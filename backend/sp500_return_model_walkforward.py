#!/usr/bin/env python3
"""
Yearly walk-forward evaluation for ML return models.

Each fold uses one calendar year as **test**, the prior calendar year as **validation**,
and all rows strictly before that validation year as **training**. Up to 20 most recent
eligible folds are run (same estimator pipeline as :func:`train_one_cadence`).

Outputs JSON under ``backend/data/sp500_return_models/walkforward_{cadence}.json`` for the
website (see ``/strategy/walkforward/{cadence}``).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO / "backend"))

from sp500_return_model import (  # noqa: E402
    MODEL_DIR,
    TrainConfig,
    _build_estimator,
    _compute_basket_weights,
    _run_strategy_backtest,
    _spearman_ic,
    _spy_baseline_returns,
    _summary_metrics,
    build_dataset,
    load_prices,
    resolve_transaction_cost_rates,
)

logger = logging.getLogger(__name__)

_METRIC_KEYS = (
    "total_return",
    "cagr",
    "ann_vol",
    "sharpe",
    "max_drawdown",
    "rolling_1y_median_return",
    "rolling_1y_hit_rate",
    "ic",
    "turnover_avg",
)


def _student_t_ci(vals: List[float], *, alpha: float = 0.05) -> Dict[str, float]:
    x = np.asarray([float(v) for v in vals if v == v and np.isfinite(float(v))], dtype=float)
    n = int(len(x))
    if n == 0:
        return {"mean": float("nan"), "ci_low": float("nan"), "ci_high": float("nan"), "n": 0}
    mean = float(np.mean(x))
    if n == 1:
        return {"mean": mean, "ci_low": mean, "ci_high": mean, "n": 1}
    s = float(np.std(x, ddof=1))
    se = s / math.sqrt(n)
    try:
        from scipy import stats  # type: ignore

        t_crit = float(stats.t.ppf(1.0 - alpha / 2.0, n - 1))
    except Exception:
        t_crit = 1.96 if n >= 30 else 2.05
    h = t_crit * se
    return {"mean": mean, "ci_low": mean - h, "ci_high": mean + h, "n": n}


def _yearly_folds(
    dates: pd.DatetimeIndex,
    *,
    min_train_rows: int,
    max_folds: int,
    train_lookback_years: int = 0,
) -> List[Dict[str, Any]]:
    """Build per-test-year folds. When ``train_lookback_years > 0`` the training
    window is restricted to the most recent K years strictly before the validation
    year (sliding window) instead of using all available history (expanding window).

    The sliding window has two advantages: (1) the model adapts to recent regimes
    (the 1990s feature distribution looks little like the 2020s after vol-targeting
    and high-frequency liquidity arrived) and (2) training is dramatically faster
    on the daily cadence (3M rows → ~500k rows for a 5y window).
    """
    ts = pd.DatetimeIndex(dates)
    if ts.tz is not None:
        ts = ts.tz_localize(None)
    years_sorted = sorted({int(y) for y in ts.year})
    folds: List[Dict[str, Any]] = []
    lookback = int(train_lookback_years) if train_lookback_years and int(train_lookback_years) > 0 else 0
    for test_year in years_sorted:
        val_year = test_year - 1
        val_start = pd.Timestamp(year=val_year, month=1, day=1)
        test_start = pd.Timestamp(year=test_year, month=1, day=1)
        test_end_excl = pd.Timestamp(year=test_year + 1, month=1, day=1)
        if lookback > 0:
            train_start = pd.Timestamp(year=val_year - lookback, month=1, day=1)
            is_train = (ts >= train_start) & (ts < val_start)
        else:
            train_start = None
            is_train = ts < val_start
        is_val = (ts >= val_start) & (ts < test_start)
        is_test = (ts >= test_start) & (ts < test_end_excl)
        n_tr, n_va, n_te = int(is_train.sum()), int(is_val.sum()), int(is_test.sum())
        if n_tr < min_train_rows or n_va < 1 or n_te < 1:
            continue
        folds.append(
            {
                "test_year": test_year,
                "val_year": val_year,
                "tr_idx": np.where(is_train)[0],
                "va_idx": np.where(is_val)[0],
                "te_idx": np.where(is_test)[0],
                "n_train": n_tr,
                "n_val": n_va,
                "n_test": n_te,
                "train_start": train_start.isoformat() if train_start is not None else None,
            }
        )
    folds.sort(key=lambda f: int(f["test_year"]))
    return folds[-max_folds:]


def _fit_fold_ml(
    ds: Any,
    prices: Dict[str, pd.DataFrame],
    cfg: TrainConfig,
    tr_idx: np.ndarray,
    va_idx: np.ndarray,
    te_idx: np.ndarray,
    slip: float,
    comm: float,
) -> Dict[str, Any]:
    """One fold: train / refit train+val / test backtest per weighting."""
    Xtr, ytr = ds.X.iloc[tr_idx], ds.y.iloc[tr_idx]
    Xva, yva = ds.X.iloc[va_idx], ds.y.iloc[va_idx]
    Xte, yte = ds.X.iloc[te_idx], ds.y.iloc[te_idx]

    rw = ds.spy_risk_sample_weight
    if rw is None:
        fit_kw_tr: Dict[str, Any] = {}
        fit_kw_tv: Dict[str, Any] = {}
    else:
        sw_tr = rw[tr_idx].astype(float)
        sw_tr = sw_tr / max(float(np.mean(sw_tr)), 1e-12)
        idx_tv = np.concatenate([tr_idx, va_idx])
        sw_tv = rw[idx_tv].astype(float)
        sw_tv = sw_tv / max(float(np.mean(sw_tv)), 1e-12)
        fit_kw_tr = {"sample_weight": sw_tr}
        fit_kw_tv = {"sample_weight": sw_tv}

    model = _build_estimator(seed=cfg.seed)
    model.fit(Xtr.values, ytr.values, **fit_kw_tr)
    pva = model.predict(Xva.values)
    val_panel = pd.DataFrame(
        {
            "date": ds.dates[va_idx],
            "symbol": ds.symbols[va_idx],
            "pred": pva,
            "y": yva.values,
        }
    )
    val_ic = _spearman_ic(val_panel["pred"], val_panel["y"], pd.DatetimeIndex(val_panel["date"]))

    Xtv = pd.concat([Xtr, Xva], axis=0)
    ytv = pd.concat([ytr, yva], axis=0)
    model_full = _build_estimator(seed=cfg.seed)
    model_full.fit(Xtv.values, ytv.values, **fit_kw_tv)
    pte = model_full.predict(Xte.values)
    test_panel = pd.DataFrame(
        {
            "date": ds.dates[te_idx],
            "symbol": ds.symbols[te_idx],
            "pred": pte,
            "y": yte.values,
        }
    )
    test_ic = _spearman_ic(test_panel["pred"], test_panel["y"], pd.DatetimeIndex(test_panel["date"]))

    bt_kwargs: Dict[str, Any] = {
        "cadence": cfg.cadence,
        "top_n": cfg.top_n,
        "tc_slippage_one_way": slip,
        "tc_commission_one_way_rate": comm,
        "pred_smoothing_days": int(getattr(cfg, "pred_smoothing_days", 0) or 0),
        "trend_filter_enabled": bool(getattr(cfg, "trend_filter_enabled", False)),
        "trend_filter_ma_days": int(getattr(cfg, "trend_filter_ma_days", 200)),
        "trend_filter_fallback_symbol": cfg.benchmark,
        "inverse_vol_blend": float(getattr(cfg, "inverse_vol_blend", 0.0)),
        "inverse_vol_lookback_days": int(getattr(cfg, "inverse_vol_lookback_days", 63)),
        "score_weighted_scheme": str(getattr(cfg, "score_weighted_scheme", "rank_decay")),
    }

    out: Dict[str, Any] = {}
    for weighting in cfg.weightings:
        sid = "ml_equal" if weighting == "equal" else "ml_pred_weighted"
        try:
            test_bt = _run_strategy_backtest(test_panel, prices, weighting=weighting, **bt_kwargs)
        except Exception as e:
            logger.warning("fold backtest failed weighting=%s: %s", weighting, e)
            continue
        spy_test = _spy_baseline_returns(
            prices,
            test_bt["returns"].index[0],
            test_bt["returns"].index[-1],
        )
        strat_m = _summary_metrics(test_bt["returns"])
        strat_m["ic"] = float(test_ic) if test_ic == test_ic else float("nan")
        strat_m["turnover_avg"] = test_bt["turnover_avg"]
        strat_m["transaction_cost_model"] = {"slippage_one_way": slip, "commission_one_way_rate": comm}
        strat_m["risk_controls"] = {
            "trend_filter_enabled": bool(getattr(cfg, "trend_filter_enabled", False)),
            "trend_filter_ma_days": int(getattr(cfg, "trend_filter_ma_days", 200)) if getattr(cfg, "trend_filter_enabled", False) else 0,
            "pred_smoothing_days": int(getattr(cfg, "pred_smoothing_days", 0) or 0),
            "trend_filter_risk_off_rebalances": int(test_bt.get("trend_filter_risk_off_rebalances", 0)),
        }
        base_m = _summary_metrics(spy_test)
        out[sid] = {"test_metrics": strat_m, "baseline_test_metrics": base_m}
    return out


def _aggregate_folds(
    fold_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Mean + 95% CI (Student t) per metric across folds, per strategy id."""
    by_strat: Dict[str, Dict[str, List[float]]] = {}
    for row in fold_rows:
        for sid, block in (row.get("strategies") or {}).items():
            if sid not in by_strat:
                by_strat[sid] = {}
            tm = block.get("test_metrics") or {}
            bm = block.get("baseline_test_metrics") or {}
            for prefix, src in (("strategy", tm), ("baseline", bm)):
                for k in _METRIC_KEYS:
                    key = f"{prefix}_{k}"
                    v = src.get(k)
                    if v is None:
                        continue
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(fv):
                        continue
                    by_strat.setdefault(sid, {}).setdefault(key, []).append(fv)

    agg: Dict[str, Any] = {}
    for sid, metrics_map in by_strat.items():
        agg[sid] = {}
        for mk, series in metrics_map.items():
            agg[sid][mk] = _student_t_ci(series, alpha=0.05)
    return agg


def run_walk_forward(
    cfg: TrainConfig,
    *,
    prices: Optional[Dict[str, pd.DataFrame]] = None,
    max_folds: int = 20,
    min_train_rows: int = 2000,
    train_lookback_years: int = 0,
) -> Dict[str, Any]:
    if prices is None:
        prices = load_prices(
            list(set(cfg.symbols) | {cfg.benchmark}),
            years=cfg.years,
            refresh=cfg.refresh_prices,
        )
    if cfg.benchmark not in prices:
        raise RuntimeError(f"benchmark {cfg.benchmark!r} not loaded")

    slip, comm = resolve_transaction_cost_rates(cfg)
    ds = build_dataset(
        prices,
        cadence=cfg.cadence,
        benchmark=cfg.benchmark,
        spy_risk_align_kappa_vol=cfg.spy_risk_align_kappa_vol,
        spy_risk_align_kappa_beta=cfg.spy_risk_align_kappa_beta,
    )
    folds = _yearly_folds(
        ds.dates,
        min_train_rows=min_train_rows,
        max_folds=max_folds,
        train_lookback_years=int(train_lookback_years),
    )
    fold_payload: List[Dict[str, Any]] = []
    for f in folds:
        tr_idx = f["tr_idx"]
        va_idx = f["va_idx"]
        te_idx = f["te_idx"]
        try:
            strategies = _fit_fold_ml(ds, prices, cfg, tr_idx, va_idx, te_idx, slip, comm)
        except Exception as e:
            logger.warning("walk-forward fold %s failed: %s", f.get("test_year"), e)
            continue
        fold_payload.append(
            {
                "test_year": f["test_year"],
                "val_year": f["val_year"],
                "n_train": f["n_train"],
                "n_val": f["n_val"],
                "n_test": f["n_test"],
                "strategies": strategies,
            }
        )

    agg = _aggregate_folds(fold_payload)
    return {
        "cadence": cfg.cadence,
        "top_n": cfg.top_n,
        "benchmark": cfg.benchmark,
        "years_history": float(cfg.years),
        "n_folds_requested": int(max_folds),
        "n_folds_completed": int(len(fold_payload)),
        "min_train_rows": int(min_train_rows),
        "train_lookback_years": int(train_lookback_years),
        "transaction_cost_model": {"slippage_one_way": slip, "commission_one_way_rate": comm},
        "risk_controls_defaults": {
            "trend_filter_enabled": bool(getattr(cfg, "trend_filter_enabled", False)),
            "trend_filter_ma_days": int(getattr(cfg, "trend_filter_ma_days", 200)) if getattr(cfg, "trend_filter_enabled", False) else 0,
            "pred_smoothing_days": int(getattr(cfg, "pred_smoothing_days", 0) or 0),
            "inverse_vol_blend": float(getattr(cfg, "inverse_vol_blend", 0.0)),
            "inverse_vol_lookback_days": int(getattr(cfg, "inverse_vol_lookback_days", 63)),
            "spy_risk_align_kappa_vol": float(cfg.spy_risk_align_kappa_vol),
            "spy_risk_align_kappa_beta": float(cfg.spy_risk_align_kappa_beta),
        },
        "folds": fold_payload,
        "aggregate": agg,
    }


def save_walk_forward_json(payload: Dict[str, Any]) -> Path:
    cad = str(payload.get("cadence", "daily"))
    p = MODEL_DIR / f"walkforward_{cad}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Yearly walk-forward ML evaluation (test = one calendar year).")
    ap.add_argument("--cadence", default="daily", choices=("daily", "weekly", "monthly", "all"))
    ap.add_argument("--years", type=float, default=30.0)
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--max-folds", type=int, default=20)
    ap.add_argument("--min-train-rows", type=int, default=2000)
    ap.add_argument(
        "--train-lookback-years",
        type=int,
        default=0,
        help="Sliding-window training: only use the last K calendar years (per fold) of training data. 0=expanding window (default).",
    )
    ap.add_argument("--symbols", default="", help="Comma-separated tickers (else SP500 via env SP500_SYMBOLS)")
    ap.add_argument("--refresh-prices", action="store_true")
    ap.add_argument("--no-tc", action="store_true", help="disable transaction costs for ablation")
    ap.add_argument("--spy-risk-kappa-vol", type=float, default=0.0)
    ap.add_argument("--spy-risk-kappa-beta", type=float, default=0.5)
    ap.add_argument(
        "--pred-smoothing-days",
        type=int,
        default=-1,
        help="Causal smoothing of predictions across last K rebalance dates. -1=cadence default (daily=5, else 0).",
    )
    ap.add_argument(
        "--no-trend-filter",
        action="store_true",
        help="Disable the SPY-200d-MA trend filter (default: ENABLED — switch to SPY in bear regimes).",
    )
    ap.add_argument("--trend-filter-ma-days", type=int, default=200)
    ap.add_argument(
        "--inverse-vol-blend",
        type=float,
        default=0.0,
        help="Blend factor (0..1) between predicted-return weights and inverse-vol weights. 0=pure pred (default), 1=pure inverse-vol.",
    )
    ap.add_argument("--inverse-vol-lookback-days", type=int, default=63)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(list(argv) if argv is not None else None)

    from sp500_quality_evaluator import load_sp500_symbols  # noqa: WPS433

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols.strip() else load_sp500_symbols()
    cadences = ("daily", "weekly", "monthly") if str(args.cadence) == "all" else (str(args.cadence),)

    # Load prices once; dataset construction differs per cadence but uses same inputs.
    universe = list(set(syms) | {"SPY"})
    prices = load_prices(universe, years=float(args.years), refresh=bool(args.refresh_prices))

    wrote: List[Path] = []
    for cad in cadences:
        if int(args.pred_smoothing_days) >= 0:
            smoothing = int(args.pred_smoothing_days)
        else:
            smoothing = 5 if str(cad) == "daily" else 0
        cfg = TrainConfig(
            cadence=str(cad),
            symbols=syms,
            years=float(args.years),
            split_train_frac=0.5,
            split_val_frac=0.25,
            split_test_frac=0.25,
            spy_risk_align_kappa_vol=float(args.spy_risk_kappa_vol),
            spy_risk_align_kappa_beta=float(args.spy_risk_kappa_beta),
            top_n=int(args.top_n),
            seed=int(args.seed),
            refresh_prices=False,
            weightings=("equal", "score_weighted"),
            tc_enabled=not bool(args.no_tc),
            pred_smoothing_days=smoothing,
            trend_filter_enabled=not bool(args.no_trend_filter),
            trend_filter_ma_days=int(args.trend_filter_ma_days),
            inverse_vol_blend=float(args.inverse_vol_blend),
            inverse_vol_lookback_days=int(args.inverse_vol_lookback_days),
        )
        payload = run_walk_forward(
            cfg,
            prices=prices,
            max_folds=int(args.max_folds),
            min_train_rows=int(args.min_train_rows),
            train_lookback_years=int(args.train_lookback_years),
        )
        out = save_walk_forward_json(payload)
        wrote.append(out)
        print(f"wrote {out}", file=sys.stderr)

    if len(wrote) > 1:
        joined = ", ".join(p.name for p in wrote)
        print(f"walk-forward complete ({len(wrote)} cadences): {joined}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
