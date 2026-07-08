#!/usr/bin/env python3
"""
Validation-period transaction log for regression_based_on_technicals.

Writes a human-readable log of daily rebalance buys/sells/holds and flags
significant missed opportunities (stocks not bought that rallied, or sold
too early before further gains, or held through large drawdowns).

Usage:
  python backend/regression_technicals_val_log.py
  python backend/regression_technicals_val_log.py --allow-partial-universe
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
_BACKEND = _REPO / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from regression_based_on_technicals import (  # noqa: E402
    REGRESSION_DIR,
    STRATEGY_ID,
    RegressionTechnicalsConfig,
    TrendV0Config,
    _build_technicals_estimator,
    _fit_technicals_model,
    _predict_panel,
    _search_backtest_params,
    build_technicals_panel,
    load_agent_close_prices,
    metrics_path,
    validate_backtest_data,
)
from sp500_return_model import (  # noqa: E402
    _compute_basket_weights,
    _inverse_vol_weights,
    _smooth_panel_predictions,
    _spy_trend_mask,
    _yahoo_symbol,
    load_prices,
    split_indices_by_time_fractions,
)
from trend_v0_partial_position_exit import _agent_db_path, _vm_db_path  # noqa: E402

import sqlite3

logger = logging.getLogger("regression_technicals_val_log")

MISSED_FWD_DAYS = 20
MISSED_RETURN_THRESHOLD = 0.08  # 8% over forward window
NEAR_MISS_RANK_BUFFER = 8  # ranks top_n+1 .. top_n+8


def _forward_return(
    close_df: pd.DataFrame,
    symbol: str,
    dt: pd.Timestamp,
    horizon_days: int,
) -> Optional[float]:
    if symbol not in close_df.columns:
        return None
    series = close_df[symbol].dropna()
    if dt not in series.index:
        return None
    pos = series.index.get_loc(dt)
    if isinstance(pos, slice):
        return None
    end = pos + horizon_days
    if end >= len(series):
        return None
    c0 = float(series.iloc[pos])
    c1 = float(series.iloc[end])
    if c0 <= 0 or not math.isfinite(c0) or not math.isfinite(c1):
        return None
    return c1 / c0 - 1.0


def _pct(x: Optional[float]) -> str:
    if x is None or not math.isfinite(x):
        return "n/a"
    return f"{100 * x:+.1f}%"


def _basket_for_date(
    panel_scored: pd.DataFrame,
    prices: Dict[str, pd.DataFrame],
    dt: pd.Timestamp,
    chosen_params: Dict[str, Any],
    score_col: str = "pred",
) -> Tuple[List[str], Dict[str, float], bool, pd.DataFrame]:
    """Return (symbols, weights, risk_on, ranked_block_for_day)."""
    trend_filter_enabled = bool(chosen_params["trend_filter_enabled"])
    top_n = int(chosen_params["top_n"])
    inverse_vol_blend = float(chosen_params.get("inverse_vol_blend", 0.0))

    day_panel = panel_scored[panel_scored["date"] == dt].copy()
    if day_panel.empty:
        return [], {}, True, day_panel

    bench_sym = _yahoo_symbol("SPY")
    risk_on = True
    if trend_filter_enabled and bench_sym in prices:
        spy_close = pd.to_numeric(prices[bench_sym]["Close"], errors="coerce")
        mask = _spy_trend_mask(spy_close, pd.DatetimeIndex([dt]), 200)
        risk_on = bool(mask.iloc[0]) if len(mask) else True

    closes = {s: pd.to_numeric(prices[s]["Close"], errors="coerce") for s in prices}
    close_df = pd.concat(closes, axis=1).sort_index()

    if not risk_on and bench_sym in close_df.columns:
        return [bench_sym], {bench_sym: 1.0}, False, day_panel

    block = day_panel.sort_values(score_col, ascending=False)
    block = block[block["symbol"].isin(close_df.columns)]
    head = block.head(top_n)
    basket = head["symbol"].astype(str).tolist()
    if not basket:
        return [], {}, risk_on, block
    weights_arr = _compute_basket_weights(head[score_col].values.astype(float), weighting="equal")
    if inverse_vol_blend > 0.0 and len(basket) > 1:
        iv = _inverse_vol_weights(basket, dt, close_df, lookback=63)
        if iv is not None and len(iv) == len(weights_arr):
            weights_arr = (1.0 - inverse_vol_blend) * weights_arr + inverse_vol_blend * iv
            s = float(weights_arr.sum())
            if s > 0:
                weights_arr = weights_arr / s
    weights = {basket[i]: float(weights_arr[i]) for i in range(len(basket))}
    return basket, weights, risk_on, block


def generate_validation_transaction_log(
    cfg: RegressionTechnicalsConfig,
    *,
    missed_fwd_days: int = MISSED_FWD_DAYS,
    missed_return_threshold: float = MISSED_RETURN_THRESHOLD,
) -> Path:
    vcfg = TrendV0Config(
        years=cfg.years,
        split_train_frac=cfg.split_train_frac,
        split_val_frac=cfg.split_val_frac,
        split_test_frac=cfg.split_test_frac,
        allow_partial_universe=cfg.allow_partial_universe,
    )
    validation = validate_backtest_data(vcfg)
    symbols: List[str] = validation["symbols"]

    vm_con = sqlite3.connect(str(_vm_db_path()))
    vm_con.row_factory = sqlite3.Row
    agent_con = sqlite3.connect(str(_agent_db_path()))
    agent_con.row_factory = sqlite3.Row
    try:
        panel = build_technicals_panel(
            symbols,
            validation["start_date"],
            validation["end_date"],
            vm_con=vm_con,
            agent_con=agent_con,
            cadence=cfg.cadence,
            provider=cfg.provider,
        )
        prices = load_agent_close_prices(agent_con, symbols)
    finally:
        vm_con.close()
        agent_con.close()

    spy_prices = load_prices([cfg.benchmark], years=max(cfg.years + 1, 2.0), refresh=False)
    if cfg.benchmark in spy_prices:
        prices[cfg.benchmark] = spy_prices[cfg.benchmark]

    tr_idx, va_idx, _ = split_indices_by_time_fractions(
        pd.DatetimeIndex(panel["date"].values),
        train_frac=cfg.split_train_frac,
        val_frac=cfg.split_val_frac,
        test_frac=cfg.split_test_frac,
    )
    train_panel = panel.iloc[tr_idx].reset_index(drop=True)
    val_panel = panel.iloc[va_idx].reset_index(drop=True)

    mp = metrics_path(cfg.cadence)
    chosen_params: Optional[Dict[str, Any]] = None
    if mp.is_file():
        try:
            chosen_params = json.loads(mp.read_text(encoding="utf-8")).get("chosen_params")
        except Exception:
            chosen_params = None

    if chosen_params:
        logger.info("Using chosen_params from %s", mp)
        model = _fit_technicals_model(_build_technicals_estimator(seed=cfg.seed), train_panel, val_panel)
    else:
        chosen_params, _, model = _search_backtest_params(train_panel, val_panel, prices, cfg.cadence, cfg.seed)
        model = _fit_technicals_model(_build_technicals_estimator(seed=cfg.seed), train_panel, val_panel)

    scored_all = panel.copy()
    scored_all["pred"] = _predict_panel(model, panel)
    if int(chosen_params["pred_smoothing_days"]) > 1:
        scored_all = _smooth_panel_predictions(
            scored_all, k=int(chosen_params["pred_smoothing_days"]), score_col="pred"
        )
    val_scored = scored_all[scored_all["date"].isin(val_panel["date"].unique())].copy()

    val_dates = pd.DatetimeIndex(sorted(val_scored["date"].unique()))
    top_n = int(chosen_params["top_n"])

    closes = {s: pd.to_numeric(prices[s]["Close"], errors="coerce") for s in prices}
    close_df = pd.concat(closes, axis=1).sort_index()

    lines: List[str] = []
    events: List[Dict[str, Any]] = []
    missed_summary: Dict[str, int] = {"missed_buy": 0, "premature_sell": 0, "held_loser": 0, "near_miss_buy": 0}

    lines.append(f"# Validation transaction log — {STRATEGY_ID}")
    lines.append(f"# Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"# Val period: {val_dates.min().date()} → {val_dates.max().date()} ({len(val_dates)} rebalance days)")
    lines.append(f"# Params: {json.dumps(chosen_params)}")
    lines.append(
        f"# Missed-opportunity rules: forward {missed_fwd_days}d return threshold {missed_return_threshold:.0%}"
    )
    lines.append("")

    prev_basket: Set[str] = set()
    for dt in val_dates:
        basket, weights, risk_on, block = _basket_for_date(val_scored, prices, dt, chosen_params)
        basket_set = set(basket)
        bought = sorted(basket_set - prev_basket)
        sold = sorted(prev_basket - basket_set)
        held = sorted(basket_set & prev_basket)

        lines.append(f"## {dt.strftime('%Y-%m-%d')}  risk_on={risk_on}  basket_size={len(basket)}")
        day_event: Dict[str, Any] = {
            "date": dt.strftime("%Y-%m-%d"),
            "risk_on": risk_on,
            "bought": [],
            "sold": [],
            "held": held,
            "missed_opportunities": [],
        }

        for sym in bought:
            row = block[block["symbol"] == sym].iloc[0] if sym in block["symbol"].values else None
            rank = int(block["symbol"].tolist().index(sym) + 1) if sym in block["symbol"].values else None
            pred = float(row["pred"]) if row is not None else None
            wt = weights.get(sym, 0.0)
            fwd = _forward_return(close_df, sym, dt, missed_fwd_days)
            lines.append(
                f"  BUY  {sym:6s}  rank={rank}  pred={pred:.6f}  weight={wt:.1%}  fwd_{missed_fwd_days}d={_pct(fwd)}"
            )
            day_event["bought"].append({"symbol": sym, "rank": rank, "pred": pred, "weight": wt, "fwd": fwd})

        for sym in sold:
            row = val_scored[(val_scored["date"] == dt) & (val_scored["symbol"] == sym)]
            pred = float(row["pred"].iloc[0]) if not row.empty else None
            fwd = _forward_return(close_df, sym, dt, missed_fwd_days)
            tag = ""
            if fwd is not None and fwd >= missed_return_threshold:
                tag = f"  *** MISSED SELL (premature exit): fwd_{missed_fwd_days}d={_pct(fwd)}"
                missed_summary["premature_sell"] += 1
                day_event["missed_opportunities"].append(
                    {"type": "premature_sell", "symbol": sym, "fwd": fwd, "pred": pred}
                )
            lines.append(f"  SELL {sym:6s}  pred={pred if pred is not None else 'n/a'}  fwd_{missed_fwd_days}d={_pct(fwd)}{tag}")
            day_event["sold"].append({"symbol": sym, "pred": pred, "fwd": fwd, "premature": bool(tag)})

        if held and len(held) <= 12:
            hold_parts = []
            for sym in held[:12]:
                fwd = _forward_return(close_df, sym, dt, missed_fwd_days)
                if fwd is not None and fwd <= -missed_return_threshold:
                    missed_summary["held_loser"] += 1
                    day_event["missed_opportunities"].append(
                        {"type": "held_loser", "symbol": sym, "fwd": fwd}
                    )
                    hold_parts.append(f"{sym}(fwd={_pct(fwd)} **held loser**)")
                else:
                    hold_parts.append(f"{sym}(fwd={_pct(fwd)})")
            lines.append(f"  HOLD {', '.join(hold_parts)}")
        elif held:
            lines.append(f"  HOLD {len(held)} names (unchanged)")

        # Missed buys: not in basket but strong forward return
        if risk_on and not block.empty:
            for _, row in block.iterrows():
                sym = str(row["symbol"])
                if sym in basket_set:
                    continue
                rank = int(block["symbol"].tolist().index(sym) + 1)
                fwd = _forward_return(close_df, sym, dt, missed_fwd_days)
                if fwd is None or fwd < missed_return_threshold:
                    continue
                pred = float(row["pred"])
                if rank <= top_n + NEAR_MISS_RANK_BUFFER:
                    missed_summary["near_miss_buy"] += 1
                    kind = "near_miss_buy"
                else:
                    missed_summary["missed_buy"] += 1
                    kind = "missed_buy"
                lines.append(
                    f"  *** MISSED BUY ({kind}): {sym:6s}  rank={rank}/{len(block)}  pred={pred:.6f}  "
                    f"fwd_{missed_fwd_days}d={_pct(fwd)}"
                )
                day_event["missed_opportunities"].append(
                    {"type": kind, "symbol": sym, "rank": rank, "pred": pred, "fwd": fwd}
                )

        lines.append("")
        events.append(day_event)
        prev_basket = basket_set

    lines.append("# Summary")
    lines.append(f"  premature_sells (sold then rallied >= {missed_return_threshold:.0%}): {missed_summary['premature_sell']}")
    lines.append(f"  missed_buys (not held, fwd >= {missed_return_threshold:.0%}): {missed_summary['missed_buy']}")
    lines.append(f"  near_miss_buys (rank just outside top {top_n}): {missed_summary['near_miss_buy']}")
    lines.append(f"  held_losers (kept, fwd <= -{missed_return_threshold:.0%}): {missed_summary['held_loser']}")

    out_txt = REGRESSION_DIR / "validation_transaction_log.txt"
    out_json = REGRESSION_DIR / "validation_transaction_log.json"
    REGRESSION_DIR.mkdir(parents=True, exist_ok=True)
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out_json.write_text(
        json.dumps(
            {
                "strategy": STRATEGY_ID,
                "segment": "validation",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "chosen_params": chosen_params,
                "val_start": val_dates.min().strftime("%Y-%m-%d"),
                "val_end": val_dates.max().strftime("%Y-%m-%d"),
                "missed_fwd_days": missed_fwd_days,
                "missed_return_threshold": missed_return_threshold,
                "summary": missed_summary,
                "days": events,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("Wrote %s (%d lines)", out_txt, len(lines))
    logger.info("Wrote %s", out_json)
    return out_txt


def main() -> int:
    ap = argparse.ArgumentParser(description="Validation transaction log for regression on technicals")
    ap.add_argument("--years", type=float, default=1.0)
    ap.add_argument("--allow-partial-universe", action="store_true")
    ap.add_argument("--fwd-days", type=int, default=MISSED_FWD_DAYS)
    ap.add_argument("--threshold", type=float, default=MISSED_RETURN_THRESHOLD, help="Min abs forward return to flag")
    args = ap.parse_args()

    cfg = RegressionTechnicalsConfig(years=float(args.years), allow_partial_universe=bool(args.allow_partial_universe))
    try:
        path = generate_validation_transaction_log(
            cfg,
            missed_fwd_days=int(args.fwd_days),
            missed_return_threshold=float(args.threshold),
        )
    except Exception as exc:
        logger.exception("Failed: %s", exc)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    raise SystemExit(main())
