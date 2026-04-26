"""
Backtrader validation for ``rsi_mean`` pipeline reports.

What this does
--------------
1. Loads a saved ``training_pipeline`` JSON (e.g. ``pipeline_rsi_mean_*.json``).
2. Replays ``optimize_rsi_mean.simulate_fast_cross_sectional`` on the **test** window using
   ``tuning.final_params`` (same wiring as ``RsiMeanAdapter.evaluate``).
3. Recomputes ``strategy_metrics.compute_aggregate_metrics`` and rolling horizon stats from the
   replayed legs / equity curve.
4. Runs `Backtrader <https://www.backtrader.com/>`_ on a **normalized equity** price series so the
   broker portfolio value tracks the same curve, then compares:
   - our pipeline JSON ``test`` metrics vs replay (should match if DB/config match the original run)
   - Backtrader ``DrawDown`` max drawdown vs our equity-curve drawdown
   - a custom ``bt.Analyzer`` that calls the *same* metric helpers (parity wiring check)

Important limitations
---------------------
- **Sharpe / Calmar / alpha / significance** in our pipeline are defined on **per-leg** returns and
  our own annualization helpers. Backtrader's ``SharpeRatio`` uses different assumptions (typically
  bar returns / different annualization). Those will **not** match numerically unless reimplemented.
  This script focuses on **max drawdown alignment** on the equity curve and full parity for metrics
  we compute ourselves inside Backtrader's run loop.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from telegram_agent.agent_db import connect, init_db
from telegram_agent.config import load_config
from telegram_agent.optimize_rsi_mean import (
    _load_dotenv_like_other_modules,
    build_context,
    simulate_fast_cross_sectional,
    symbols_with_bar_in_window,
)
from telegram_agent.rolling_window_metrics import (
    rolling_horizon_returns,
    rolling_metric_key_base,
    rolling_window_to_timedelta,
)
from telegram_agent.strategy_metrics import TradeLeg, compute_aggregate_metrics, max_drawdown_from_equity, equity_curve_compound
from telegram_agent.training_pipeline import RsiMeanAdapter, _prepare_pipeline_context, _utc


def _parse_iso(s: str) -> datetime:
    raw = (s or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _cfg_for_report(cfg: dict, report: Dict[str, Any]) -> None:
    """Align optimizer keys with what the saved report used (best-effort)."""
    test_rm = (report.get("test") or {}).get("rolling_metrics") or {}
    rw = str(test_rm.get("rolling_window") or cfg.get("optimize_rolling_window") or "30d")
    suf = str(test_rm.get("rolling_metric_suffix") or cfg.get("optimize_rolling_metric_suffix") or "30d")
    cfg["optimize_rolling_window"] = rw
    cfg["optimize_rolling_metric_suffix"] = suf
    # Match pipeline evaluation stride policy.
    cfg.setdefault("optimize_dense_hourly_simulation", bool(cfg.get("pipeline_optimize_dense_hourly_simulation", True)))
    cfg.setdefault("optimize_deterministic_simulation", bool(cfg.get("pipeline_optimize_deterministic_simulation", True)))


def _replay_rsi_mean_test(cfg: dict, report: Dict[str, Any]) -> Dict[str, Any]:
    splits = report.get("splits") or {}
    tr = splits.get("train") or {}
    te = splits.get("test") or {}
    if not tr or not te:
        raise ValueError("report missing splits.train / splits.test")

    start = _parse_iso(str(tr.get("start")))
    end = _parse_iso(str(te.get("end")))
    test_start = _parse_iso(str(te.get("start")))
    test_end = _parse_iso(str(te.get("end")))

    symbols = list(report.get("symbols_requested") or [])
    if not symbols:
        raise ValueError("report missing symbols_requested")

    src = report.get("sources_filter")
    sources: Optional[List[str]] = None
    if isinstance(src, list) and src:
        sources = [str(x).strip() for x in src if str(x).strip()]

    ctx = _prepare_pipeline_context(cfg, symbols=symbols, start=start, end=end, sources=sources)
    syms = symbols_with_bar_in_window(ctx, test_start, test_end)
    if not syms:
        raise RuntimeError("No symbols_with_bar_in_window for test window; cannot replay")

    final_params = ((report.get("tuning") or {}).get("final_params")) or {}
    if not isinstance(final_params, dict) or not final_params:
        raise ValueError("report.tuning.final_params missing or empty")

    rp, top_k, min_bars, exposure, dd_stop_f, dd_resume_f = RsiMeanAdapter._params_from_dict(final_params)

    curve, legs = simulate_fast_cross_sectional(
        ctx,
        syms,
        start=test_start,
        end=test_end,
        min_bars=min_bars,
        top_k=top_k,
        params=rp,
        exposure=exposure,
        dd_stop=dd_stop_f,
        dd_resume=dd_resume_f,
        max_eval_points=int(RsiMeanAdapter._sim_max_eval_points(cfg)),
        grid_offset=0,
    )

    enabled = RsiMeanAdapter._enabled_metrics(cfg)
    bench = str(cfg.get("test_benchmark_symbol") or "SPY").strip().upper()
    rf = float(cfg.get("test_risk_free_annual", 0.04))
    oos = float(cfg.get("test_oos_split", 0.5))

    con = connect(Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite")))
    try:
        init_db(con)
        agg = compute_aggregate_metrics(
            con,
            legs,
            benchmark_symbol=bench,
            risk_free_annual=rf,
            oos_split=oos,
            enabled=enabled,
        )
    finally:
        con.close()

    rw_td = rolling_window_to_timedelta(str(cfg.get("optimize_rolling_window", "1y")))
    ts = [_utc(t) for t, _ in curve]
    eq = [float(x) for _, x in curve]
    roll = rolling_horizon_returns(ts, eq, rw_td) if curve else []
    suf = str(cfg.get("optimize_rolling_metric_suffix", "1y"))
    mk = rolling_metric_key_base(suf)
    med = hit = None
    if roll:
        sr = sorted(float(x) for x in roll)
        med = float(sr[len(sr) // 2])
        hit = float(sum(1 for x in roll if x > 0) / len(roll))

    mdd_eq = max_drawdown_from_equity(equity_curve_compound([float(l.realized_pct) / 100.0 for l in legs])) if legs else 0.0

    return {
        "replay": {
            "symbols_with_bar_in_test": list(syms),
            "n_hours": len(curve),
            "n_legs": len(legs),
            "aggregate_metrics": agg,
            "rolling_metrics": {
                "rolling_window": str(cfg.get("optimize_rolling_window", "1y")),
                "rolling_metric_suffix": suf,
                "rolling_metric_keys": dict(mk),
                mk["median_return"]: med,
                mk["hit_rate"]: hit,
                "n_roll_windows": len(roll),
            },
            "max_drawdown_from_leg_returns_equity": float(mdd_eq),
        },
        "curve": [{"t": t.isoformat(), "equity": float(e)} for t, e in zip(ts, eq)],
        "legs": [{"entry": _utc(l.entry).isoformat(), "exit": _utc(l.exit).isoformat(), "symbol": l.symbol, "realized_pct": float(l.realized_pct)} for l in legs],
    }


def _run_backtrader_on_equity_curve(
    ts: List[datetime],
    eq: List[float],
    *,
    legs: List[TradeLeg],
    cfg: dict,
) -> Dict[str, Any]:
    try:
        import backtrader as bt
    except ImportError as e:
        raise RuntimeError("Install backtrader: pip install backtrader") from e

    if not ts or not eq or len(ts) != len(eq):
        return {"error": "empty_or_mismatched_curve"}

    eq0 = float(eq[0]) if float(eq[0]) != 0 else 1.0
    norm = [float(x) / eq0 for x in eq]

    # Backtrader + pandas: use UTC-naive index for broad compatibility.
    idx = pd.DatetimeIndex(pd.to_datetime(ts, utc=True)).tz_convert("UTC").tz_localize(None)
    df = pd.DataFrame({"open": norm, "high": norm, "low": norm, "close": norm, "volume": 0.0}, index=idx)

    class BuyAndHoldEquity(bt.Strategy):
        def next(self):
            if not self.position:
                self.order_target_percent(target=1.0)

    class PipelineParityAnalyzer(bt.Analyzer):
        """Runs the same metric helpers as the pipeline, attached to the Cerebro run."""

        params = (("cfg", None), ("legs", None), ("ts", None), ("eq", None))

        def create_analysis(self) -> None:
            self.rets = {}

        def stop(self) -> None:
            cfg = self.p.cfg
            legs: List[TradeLeg] = self.p.legs
            ts2: List[datetime] = self.p.ts
            eq2: List[float] = self.p.eq
            con = connect(Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite")))
            try:
                init_db(con)
                enabled = RsiMeanAdapter._enabled_metrics(cfg)
                agg = compute_aggregate_metrics(
                    con,
                    legs,
                    benchmark_symbol=str(cfg.get("test_benchmark_symbol") or "SPY").strip().upper(),
                    risk_free_annual=float(cfg.get("test_risk_free_annual", 0.04)),
                    oos_split=float(cfg.get("test_oos_split", 0.5)),
                    enabled=enabled,
                )
            finally:
                con.close()
            rw_td = rolling_window_to_timedelta(str(cfg.get("optimize_rolling_window", "1y")))
            roll = rolling_horizon_returns(ts2, eq2, rw_td) if ts2 and eq2 else []
            suf = str(cfg.get("optimize_rolling_metric_suffix", "1y"))
            mk = rolling_metric_key_base(suf)
            med = hit = None
            if roll:
                sr = sorted(float(x) for x in roll)
                med = float(sr[len(sr) // 2])
                hit = float(sum(1 for x in roll if x > 0) / len(roll))
            self.rets["aggregate_metrics"] = agg
            self.rets["rolling_metrics"] = {
                "rolling_window": str(cfg.get("optimize_rolling_window", "1y")),
                "rolling_metric_suffix": suf,
                "rolling_metric_keys": dict(mk),
                mk["median_return"]: med,
                mk["hit_rate"]: hit,
                "n_roll_windows": len(roll),
            }

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.broker.setcash(100_000.0)
    cerebro.broker.setcommission(commission=0.0)
    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)
    cerebro.addstrategy(BuyAndHoldEquity)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
    cerebro.addanalyzer(
        PipelineParityAnalyzer,
        cfg=cfg,
        legs=legs,
        ts=ts,
        eq=eq,
        _name="parity",
    )
    strat = cerebro.run()[0]
    dd = strat.analyzers.dd.get_analysis()
    par = strat.analyzers.parity.get_analysis()
    # Backtrader's ``max.drawdown`` is expressed in **percent points** (e.g. 24.01 means ~24% peak-to-trough).
    # Our pipeline stores max drawdown as a **fraction** (e.g. 0.2401). Convert for apples-to-apples comparison.
    try:
        bt_mdd_frac = float(dd.max.drawdown) / 100.0
    except Exception:
        bt_mdd_frac = None
    return {
        "backtrader_drawdown_max_fraction": bt_mdd_frac,
        "embedded_pipeline_metrics_via_bt_analyzer": par,
    }


def _diff_metrics(a: Dict[str, Any], b: Dict[str, Any], keys: Tuple[str, ...]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in keys:
        va, vb = a.get(k), b.get(k)
        if va is None and vb is None:
            continue
        try:
            fa, fb = float(va), float(vb)
            out[k] = {"saved": va, "replay": vb, "abs_diff": abs(fa - fb)}
        except (TypeError, ValueError):
            out[k] = {"saved": va, "replay": vb, "equal": va == vb}
    return out


def main() -> None:
    _load_dotenv_like_other_modules()
    p = argparse.ArgumentParser(description="Validate pipeline rsi_mean test metrics vs Backtrader replay")
    p.add_argument(
        "--report",
        type=str,
        default="telegram_agent/data/pipeline_rsi_mean_20260420T225406Z.json",
        help="Path to pipeline JSON report",
    )
    p.add_argument("--out-json", type=str, default="", help="Optional path to write comparison JSON")
    args = p.parse_args()

    report_path = Path(args.report)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cfg = load_config()
    _cfg_for_report(cfg, report)

    replay_bundle = _replay_rsi_mean_test(cfg, report)
    replay = replay_bundle["replay"]
    curve_rows = replay_bundle["curve"]
    legs_dicts = replay_bundle["legs"]
    legs_objs = [
        TradeLeg(
            entry=_parse_iso(x["entry"]),
            exit=_parse_iso(x["exit"]),
            symbol=str(x["symbol"]),
            realized_pct=float(x["realized_pct"]),
        )
        for x in legs_dicts
    ]
    ts = [_parse_iso(x["t"]) for x in curve_rows]
    eq = [float(x["equity"]) for x in curve_rows]

    saved_test = report.get("test") or {}
    saved_agg = saved_test.get("aggregate_metrics") or {}
    saved_roll = saved_test.get("rolling_metrics") or {}

    mk = rolling_metric_key_base(str(saved_roll.get("rolling_metric_suffix") or cfg.get("optimize_rolling_metric_suffix", "30d")))
    med_k = mk["median_return"]
    hit_k = mk["hit_rate"]

    agg_keys = (
        "max_drawdown",
        "sharpe",
        "calmar",
        "mean_return_per_trade_pct",
        "alpha_vs_benchmark_mean",
        "sharpe_in_sample",
        "sharpe_out_of_sample",
    )
    agg_diff = _diff_metrics(saved_agg, replay["aggregate_metrics"], agg_keys)
    roll_diff = _diff_metrics(
        {med_k: saved_roll.get(med_k), hit_k: saved_roll.get(hit_k), "n_roll_windows": saved_roll.get("n_roll_windows")},
        {
            med_k: replay["rolling_metrics"].get(med_k),
            hit_k: replay["rolling_metrics"].get(hit_k),
            "n_roll_windows": replay["rolling_metrics"].get("n_roll_windows"),
        },
        (med_k, hit_k, "n_roll_windows"),
    )

    mdd_saved = float(saved_agg.get("max_drawdown") or 0.0)
    mdd_replay_eq = max_drawdown_from_equity(eq) if eq else 0.0

    bt_out = _run_backtrader_on_equity_curve(ts, eq, legs=legs_objs, cfg=cfg)
    bt_mdd = bt_out.get("backtrader_drawdown_max_fraction")
    mdd_bt_vs_eq = None if bt_mdd is None else abs(float(bt_mdd) - float(mdd_replay_eq))

    out: Dict[str, Any] = {
        "report_path": str(report_path),
        "notes": {
            "backtrader": "https://www.backtrader.com/ — event-driven backtesting; analyzers for drawdown/Sharpe etc.",
            "sharpe_parity": "Pipeline Sharpe uses per-trade returns + custom annualization; Backtrader SharpeRatio differs by design.",
            "drawdown_compare": "Compares Backtrader DrawDown on normalized-equity buy&hold vs max_drawdown_from_equity on the replay curve.",
        },
        "diff_aggregate_metrics": agg_diff,
        "diff_rolling_metrics": roll_diff,
        "drawdown": {
            "saved_max_drawdown": mdd_saved,
            "replay_max_drawdown_on_equity_series": float(mdd_replay_eq),
            "backtrader_max_drawdown": bt_mdd,
            "abs_diff_backtrader_vs_replay_equity": mdd_bt_vs_eq,
        },
        "counts": {
            "saved_n_legs": saved_test.get("n_legs"),
            "replay_n_legs": replay.get("n_legs"),
            "saved_n_hours": saved_test.get("n_hours"),
            "replay_n_hours": replay.get("n_hours"),
        },
        "backtrader": bt_out,
    }

    print(json.dumps(out, indent=2, default=str))

    if args.out_json:
        Path(args.out_json).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
