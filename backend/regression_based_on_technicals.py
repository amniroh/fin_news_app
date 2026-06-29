#!/usr/bin/env python3
"""
Regression on technical indicators + price history for all interesting stocks.

Features per (date, symbol): EMA distance, MACD spread/line, ADX, RVOL, short-horizon
returns and volatility from daily closes. LightGBM predicts next-day return; top-N
equal-weight basket rebalanced daily.

Hyperparameters are chosen on the validation segment to maximize Sharpe subject to:
  Sharpe > 1.0  and  max drawdown >= -20%
"""
from __future__ import annotations

import itertools
import json
import logging
import math
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
_BACKEND = _REPO / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from sp500_return_model import (  # noqa: E402
    CADENCE_HORIZON_DAYS,
    DATA_DIR,
    _equity_curve_payload,
    _json_default,
    _run_strategy_backtest,
    _spearman_ic,
    _summary_metrics,
    load_prices,
    split_indices_by_time_fractions,
)
from trend_v0_partial_position_exit import (  # noqa: E402
    TrendV0Config,
    _agent_db_path,
    _vm_db_path,
    _walkforward_folds,
    load_interesting_symbols,
    load_ohlcv_daily,
    load_technical_history,
    validate_backtest_data,
)

logger = logging.getLogger(__name__)

STRATEGY_ID = "regression_based_on_technicals"
REGRESSION_DIR = DATA_DIR / "regression_technicals_models"
REGRESSION_DIR.mkdir(parents=True, exist_ok=True)

FEATURE_NAMES = [
    "close_ema_ratio",
    "macd_spread",
    "macd_line_norm",
    "adx",
    "rvol",
    "ret_1d",
    "ret_5d",
    "ret_20d",
    "vol_20d",
]

SHARPE_MIN = 1.0
MAX_DRAWDOWN_MIN = -0.20  # i.e. drawdown no worse than 20%

SEARCH_GRID: Dict[str, Sequence[Any]] = {
    "top_n": (15, 25, 35),
    "pred_smoothing_days": (3, 5, 7, 10),
    "inverse_vol_blend": (0.25, 0.5),
    "trend_filter_enabled": (False, True),
}

EARLY_STOPPING_ROUNDS = 60


def _build_technicals_estimator(seed: int = 7) -> Any:
    """Regularized regressor — shallower trees, stronger L1/L2, subsampling."""
    try:
        import lightgbm as lgb  # type: ignore

        return lgb.LGBMRegressor(
            n_estimators=600,
            learning_rate=0.025,
            num_leaves=31,
            max_depth=6,
            min_child_samples=500,
            min_split_gain=0.02,
            feature_fraction=0.75,
            bagging_fraction=0.75,
            bagging_freq=1,
            reg_lambda=5.0,
            reg_alpha=1.0,
            random_state=seed,
            n_jobs=-1,
            verbosity=-1,
        )
    except Exception:
        from sklearn.ensemble import HistGradientBoostingRegressor

        return HistGradientBoostingRegressor(
            max_iter=400,
            learning_rate=0.03,
            max_leaf_nodes=31,
            max_depth=6,
            min_samples_leaf=500,
            l2_regularization=2.0,
            early_stopping=True,
            validation_fraction=0.1,
            n_iter_no_change=50,
            random_state=seed,
        )


def _fit_technicals_model(
    model: Any,
    train_panel: pd.DataFrame,
    val_panel: pd.DataFrame,
) -> Any:
    """Fit on train with early stopping monitored on validation (no train+val refit)."""
    Xtr = train_panel[FEATURE_NAMES]
    ytr = train_panel["y"].values
    Xva = val_panel[FEATURE_NAMES]
    yva = val_panel["y"].values

    try:
        import lightgbm as lgb  # type: ignore

        if isinstance(model, lgb.LGBMRegressor):
            model.fit(
                Xtr,
                ytr,
                eval_set=[(Xva, yva)],
                eval_metric="l2",
                callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
            )
            return model
    except Exception:
        pass

    try:
        from sklearn.ensemble import HistGradientBoostingRegressor

        if isinstance(model, HistGradientBoostingRegressor):
            model.fit(Xtr.values, ytr, X_val=Xva.values, y_val=yva)
            return model
    except Exception:
        pass

    model.fit(Xtr.values, ytr)
    return model


def _predict_panel(model: Any, panel: pd.DataFrame) -> np.ndarray:
    Xdf = panel[FEATURE_NAMES]
    try:
        import lightgbm as lgb  # type: ignore

        if isinstance(model, lgb.LGBMRegressor):
            pred = model.predict(Xdf)
        else:
            pred = model.predict(Xdf.values)
    except Exception:
        pred = model.predict(Xdf.values)
    return np.asarray(pred, dtype=float)


@dataclass
class RegressionTechnicalsConfig:
    years: float = 1.0
    split_train_frac: float = 0.5
    split_val_frac: float = 0.25
    split_test_frac: float = 0.25
    benchmark: str = "SPY"
    cadence: str = "daily"
    provider: str = "yfinance"
    allow_partial_universe: bool = False
    seed: int = 7


@dataclass
class RegressionTechnicalsResult:
    cadence: str
    strategy_id: str
    weighting: str
    split_train_frac: float
    split_val_frac: float
    split_test_frac: float
    n_train: int
    n_val: int
    n_test: int
    feature_names: List[str]
    train_metrics: Dict[str, Any]
    val_metrics: Dict[str, Any]
    test_metrics: Dict[str, Any]
    baseline_train_metrics: Dict[str, Any]
    baseline_val_metrics: Dict[str, Any]
    baseline_test_metrics: Dict[str, Any]
    train_ic: float
    val_ic: float
    test_ic: float
    train_curve: List[Dict[str, Any]]
    val_curve: List[Dict[str, Any]]
    test_curve: List[Dict[str, Any]]
    baseline_train_curve: List[Dict[str, Any]]
    baseline_val_curve: List[Dict[str, Any]]
    baseline_test_curve: List[Dict[str, Any]]
    current_top: List[Dict[str, Any]]
    turnover_test: float
    trained_at: str
    universe_size: int
    history_years: float
    chosen_params: Dict[str, Any] = field(default_factory=dict)
    optimization: Dict[str, Any] = field(default_factory=dict)
    data_validation: Dict[str, Any] = field(default_factory=dict)
    walkforward_folds: List[Dict[str, Any]] = field(default_factory=list)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_dumps(obj: Any) -> str:
    def _default(o: Any) -> Any:
        if isinstance(o, (float, np.floating)):
            v = float(o)
            if math.isnan(v) or math.isinf(v):
                return None
            return v
        return _json_default(o)

    return json.dumps(obj, indent=2, default=_default)


def load_agent_close_prices(agent_con: sqlite3.Connection, symbols: Sequence[str]) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = load_ohlcv_daily(agent_con, sym)
        if df.empty:
            continue
        df = df.sort_index()
        out[sym] = pd.DataFrame({"Close": df["close"]}, index=df.index)
    return out


def build_technicals_panel(
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    *,
    vm_con: sqlite3.Connection,
    agent_con: sqlite3.Connection,
    cadence: str,
    provider: str,
) -> pd.DataFrame:
    horizon = CADENCE_HORIZON_DAYS[cadence]
    tech = load_technical_history(vm_con, symbols, start_date, end_date, provider=provider)
    if tech.empty:
        raise RuntimeError("no technical indicator rows")

    panels: List[pd.DataFrame] = []
    for sym in symbols:
        ohlcv = load_ohlcv_daily(agent_con, sym)
        if ohlcv.empty:
            continue
        ohlcv = ohlcv.loc[(ohlcv.index >= pd.Timestamp(start_date)) & (ohlcv.index <= pd.Timestamp(end_date))]
        if len(ohlcv) < 30:
            continue
        close = ohlcv["close"].astype(float)
        sub_tech = tech[tech["symbol"] == sym].copy()
        if sub_tech.empty:
            continue
        sub_tech["date"] = pd.to_datetime(sub_tech["asof_date"])
        sub_tech = sub_tech.set_index("date").sort_index()
        merged = ohlcv.join(sub_tech[["ema", "macd_line", "macd_signal", "adx", "rvol"]], how="inner")
        merged = merged.dropna(subset=["ema", "macd_line", "macd_signal", "adx", "rvol", "close"])
        if merged.empty:
            continue
        ret_1d = close.pct_change()
        ret_5d = close.pct_change(5)
        ret_20d = close.pct_change(20)
        vol_20d = ret_1d.rolling(20, min_periods=10).std()
        fwd = np.log((close.shift(-horizon) / close).clip(lower=1e-12))
        idx = merged.index.intersection(fwd.dropna().index)
        if len(idx) < 20:
            continue
        frame = pd.DataFrame(
            {
                "date": idx,
                "symbol": sym,
                "close_ema_ratio": (merged.loc[idx, "close"] / merged.loc[idx, "ema"] - 1.0).values,
                "macd_spread": (merged.loc[idx, "macd_line"] - merged.loc[idx, "macd_signal"]).values,
                "macd_line_norm": (merged.loc[idx, "macd_line"] / merged.loc[idx, "close"]).values,
                "adx": merged.loc[idx, "adx"].values,
                "rvol": merged.loc[idx, "rvol"].values,
                "ret_1d": ret_1d.loc[idx].values,
                "ret_5d": ret_5d.loc[idx].values,
                "ret_20d": ret_20d.loc[idx].values,
                "vol_20d": vol_20d.loc[idx].values,
                "y": fwd.loc[idx].values,
            }
        ).dropna()
        if not frame.empty:
            panels.append(frame)

    if not panels:
        raise RuntimeError("no regression panel rows produced")
    panel = pd.concat(panels, axis=0, ignore_index=True)
    panel["date"] = pd.to_datetime(panel["date"])
    panel = panel.replace([np.inf, -np.inf], np.nan).dropna()
    return panel


def _meets_constraints(metrics: Dict[str, Any]) -> bool:
    sh = metrics.get("sharpe")
    dd = metrics.get("max_drawdown")
    if sh is None or dd is None:
        return False
    if not math.isfinite(float(sh)) or not math.isfinite(float(dd)):
        return False
    return float(sh) > SHARPE_MIN and float(dd) >= MAX_DRAWDOWN_MIN


def _search_backtest_params(
    train_panel: pd.DataFrame,
    val_panel: pd.DataFrame,
    prices: Dict[str, pd.DataFrame],
    cadence: str,
    seed: int,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Any]:
    model = _fit_technicals_model(_build_technicals_estimator(seed=seed), train_panel, val_panel)

    val_scored = val_panel.copy()
    val_scored["pred"] = _predict_panel(model, val_panel)

    keys = list(SEARCH_GRID.keys())
    combos = list(itertools.product(*(SEARCH_GRID[k] for k in keys)))
    trials: List[Dict[str, Any]] = []
    best: Optional[Dict[str, Any]] = None
    best_feasible: Optional[Dict[str, Any]] = None

    for combo in combos:
        params = dict(zip(keys, combo))
        try:
            bt = _run_strategy_backtest(
                val_scored,
                prices,
                cadence=cadence,
                top_n=int(params["top_n"]),
                weighting="equal",
                score_col="pred",
                pred_smoothing_days=int(params["pred_smoothing_days"]),
                trend_filter_enabled=bool(params["trend_filter_enabled"]),
                inverse_vol_blend=float(params["inverse_vol_blend"]),
            )
            metrics = _summary_metrics(bt["returns"])
            row = {**params, **metrics, "feasible": _meets_constraints(metrics)}
            trials.append(row)
            score = float(metrics.get("sharpe", float("-inf")))
            if row["feasible"]:
                if best_feasible is None or score > float(best_feasible.get("sharpe", float("-inf"))):
                    best_feasible = row
            if best is None or score > float(best.get("sharpe", float("-inf"))):
                best = row
        except Exception as exc:
            trials.append({**params, "error": str(exc), "feasible": False})

    chosen = best_feasible or best
    if chosen is None:
        raise RuntimeError("parameter search produced no valid backtests")
    chosen_params = {k: chosen[k] for k in keys}
    return chosen_params, trials, model


def evaluate_regression_technicals(cfg: RegressionTechnicalsConfig) -> RegressionTechnicalsResult:
    vcfg = TrendV0Config(
        years=cfg.years,
        split_train_frac=cfg.split_train_frac,
        split_val_frac=cfg.split_val_frac,
        split_test_frac=cfg.split_test_frac,
        benchmark=cfg.benchmark,
        provider=cfg.provider,
        allow_partial_universe=cfg.allow_partial_universe,
    )
    validation = validate_backtest_data(vcfg)
    symbols: List[str] = validation["symbols"]
    start_date = validation["start_date"]
    end_date = validation["end_date"]

    vm_con = sqlite3.connect(str(_vm_db_path()))
    vm_con.row_factory = sqlite3.Row
    agent_con = sqlite3.connect(str(_agent_db_path()))
    agent_con.row_factory = sqlite3.Row
    try:
        panel = build_technicals_panel(
            symbols,
            start_date,
            end_date,
            vm_con=vm_con,
            agent_con=agent_con,
            cadence=cfg.cadence,
            provider=cfg.provider,
        )
        prices = load_agent_close_prices(agent_con, symbols)
    finally:
        vm_con.close()
        agent_con.close()

    # SPY for baseline / trend filter
    spy_prices = load_prices([cfg.benchmark], years=max(cfg.years + 1, 2.0), refresh=False)
    if cfg.benchmark in spy_prices:
        prices[cfg.benchmark] = spy_prices[cfg.benchmark]

    tr_idx, va_idx, te_idx = split_indices_by_time_fractions(
        pd.DatetimeIndex(panel["date"].values),
        train_frac=cfg.split_train_frac,
        val_frac=cfg.split_val_frac,
        test_frac=cfg.split_test_frac,
    )
    train_panel = panel.iloc[tr_idx].reset_index(drop=True)
    val_panel = panel.iloc[va_idx].reset_index(drop=True)
    test_panel = panel.iloc[te_idx].reset_index(drop=True)

    chosen_params, trials, model = _search_backtest_params(
        train_panel, val_panel, prices, cfg.cadence, cfg.seed
    )

    # Refit once more with the same regularized recipe (train + early stop on val).
    model_full = _fit_technicals_model(_build_technicals_estimator(seed=cfg.seed), train_panel, val_panel)

    def _score_and_bt(seg: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        scored = seg.copy()
        scored["pred"] = _predict_panel(model_full, seg)
        bt = _run_strategy_backtest(
            scored,
            prices,
            cadence=cfg.cadence,
            top_n=int(chosen_params["top_n"]),
            weighting="equal",
            score_col="pred",
            pred_smoothing_days=int(chosen_params["pred_smoothing_days"]),
            trend_filter_enabled=bool(chosen_params["trend_filter_enabled"]),
            inverse_vol_blend=float(chosen_params["inverse_vol_blend"]),
        )
        return scored, bt

    _, train_bt = _score_and_bt(train_panel)
    val_scored, val_bt = _score_and_bt(val_panel)
    test_scored, test_bt = _score_and_bt(test_panel)

    train_scored = train_panel.copy()
    train_scored["pred"] = _predict_panel(model_full, train_panel)
    train_ic = _spearman_ic(train_scored["pred"], train_panel["y"], pd.DatetimeIndex(train_panel["date"]))
    val_ic = _spearman_ic(val_scored["pred"], val_panel["y"], pd.DatetimeIndex(val_panel["date"]))
    test_ic = _spearman_ic(test_scored["pred"], test_panel["y"], pd.DatetimeIndex(test_panel["date"]))

    def _baseline(seg_returns: pd.Series) -> pd.Series:
        spy = prices.get(cfg.benchmark)
        if spy is None:
            raise RuntimeError("SPY missing")
        c = pd.to_numeric(spy["Close"], errors="coerce")
        r = c.pct_change()
        return r.loc[(r.index >= seg_returns.index.min()) & (r.index <= seg_returns.index.max())].dropna()

    base_train = _baseline(train_bt["returns"])
    base_val = _baseline(val_bt["returns"])
    base_test = _baseline(test_bt["returns"])

    latest_dt = panel["date"].max()
    latest = test_scored[test_scored["date"] == latest_dt] if (test_scored["date"] == latest_dt).any() else test_scored
    if latest.empty:
        latest = test_scored.groupby("symbol").tail(1)
    head = latest.sort_values("pred", ascending=False).head(int(chosen_params["top_n"]))
    w = 1.0 / max(1, len(head))
    current_top = [
        {"symbol": str(r.symbol), "rank": i + 1, "weight": w, "pred": float(r.pred)}
        for i, r in enumerate(head.itertuples(index=False))
    ]

    calendar = pd.DatetimeIndex(sorted(panel["date"].unique()))
    wf_folds: List[Dict[str, Any]] = []
    for fold in _walkforward_folds(calendar):
        te_s = pd.Timestamp(fold["test_start"])
        te_e = pd.Timestamp(fold["test_end"])
        va_s = pd.Timestamp(fold["val_start"])
        va_e = pd.Timestamp(fold["val_end"])
        tr_end = pd.Timestamp(fold["train_end"])
        tr_panel = panel[panel["date"] <= tr_end]
        va_seg = panel[(panel["date"] >= va_s) & (panel["date"] <= va_e)]
        te_seg = panel[(panel["date"] >= te_s) & (panel["date"] <= te_e)]
        if tr_panel.empty or va_seg.empty or te_seg.empty:
            continue
        try:
            fp, _, _ = _search_backtest_params(tr_panel, va_seg, prices, cfg.cadence, cfg.seed)
        except Exception:
            continue
        fm = _fit_technicals_model(_build_technicals_estimator(seed=cfg.seed), tr_panel, va_seg)
        te_sc = te_seg.copy()
        te_sc["pred"] = _predict_panel(fm, te_seg)
        te_bt = _run_strategy_backtest(
            te_sc,
            prices,
            cadence=cfg.cadence,
            top_n=int(fp["top_n"]),
            weighting="equal",
            score_col="pred",
            pred_smoothing_days=int(fp["pred_smoothing_days"]),
            trend_filter_enabled=bool(fp["trend_filter_enabled"]),
            inverse_vol_blend=float(fp["inverse_vol_blend"]),
        )
        te_ret = te_bt["returns"]
        base_te = _baseline(te_ret) if not te_ret.empty else pd.Series(dtype=float)
        wf_folds.append(
            {
                **{k: fold[k] for k in ("test_year", "val_year", "test_month", "val_month", "n_train", "n_val", "n_test")},
                "strategies": {
                    STRATEGY_ID: {
                        "test_metrics": _summary_metrics(te_ret),
                        "baseline_test_metrics": _summary_metrics(base_te),
                    }
                },
            }
        )

    feasible_count = sum(1 for t in trials if t.get("feasible"))
    return RegressionTechnicalsResult(
        cadence=cfg.cadence,
        strategy_id=STRATEGY_ID,
        weighting="equal",
        split_train_frac=float(cfg.split_train_frac),
        split_val_frac=float(cfg.split_val_frac),
        split_test_frac=float(cfg.split_test_frac),
        n_train=int(len(train_panel)),
        n_val=int(len(val_panel)),
        n_test=int(len(test_panel)),
        feature_names=list(FEATURE_NAMES),
        train_metrics=_summary_metrics(train_bt["returns"]),
        val_metrics=_summary_metrics(val_bt["returns"]),
        test_metrics=_summary_metrics(test_bt["returns"]),
        baseline_train_metrics=_summary_metrics(base_train),
        baseline_val_metrics=_summary_metrics(base_val),
        baseline_test_metrics=_summary_metrics(base_test),
        train_ic=float(train_ic) if train_ic == train_ic else float("nan"),
        val_ic=float(val_ic) if val_ic == val_ic else float("nan"),
        test_ic=float(test_ic) if test_ic == test_ic else float("nan"),
        train_curve=_equity_curve_payload(train_bt["returns"]),
        val_curve=_equity_curve_payload(val_bt["returns"]),
        test_curve=_equity_curve_payload(test_bt["returns"]),
        baseline_train_curve=_equity_curve_payload(base_train),
        baseline_val_curve=_equity_curve_payload(base_val),
        baseline_test_curve=_equity_curve_payload(base_test),
        current_top=current_top,
        turnover_test=float(test_bt["turnover_avg"]),
        trained_at=_utcnow_iso(),
        universe_size=len(symbols),
        history_years=float(cfg.years),
        chosen_params=chosen_params,
        optimization={
            "sharpe_min": SHARPE_MIN,
            "max_drawdown_min": MAX_DRAWDOWN_MIN,
            "n_trials": len(trials),
            "n_feasible_trials": feasible_count,
            "constraints_met_on_val": _meets_constraints(_summary_metrics(val_bt["returns"])),
            "regularization": {
                "early_stopping_rounds": EARLY_STOPPING_ROUNDS,
                "fit_policy": "train_only_with_val_early_stop",
                "reg_lambda": 5.0,
                "reg_alpha": 1.0,
                "max_depth": 6,
                "num_leaves": 31,
                "min_child_samples": 500,
            },
            "top_trials": sorted(
                [t for t in trials if "sharpe" in t],
                key=lambda x: float(x.get("sharpe", float("-inf"))),
                reverse=True,
            )[:5],
        },
        data_validation=validation,
        walkforward_folds=wf_folds,
    )


def metrics_path(cadence: str = "daily") -> Path:
    return REGRESSION_DIR / f"regression_technicals_model_{cadence}_metrics.json"


def walkforward_path(cadence: str = "daily") -> Path:
    return REGRESSION_DIR / f"walkforward_{cadence}.json"


def save_artifacts(result: RegressionTechnicalsResult) -> Path:
    p = metrics_path(result.cadence)
    p.write_text(_safe_json_dumps(asdict(result)), encoding="utf-8")
    wf = {
        "cadence": result.cadence,
        "strategy": STRATEGY_ID,
        "benchmark": "SPY",
        "generated_at": result.trained_at,
        "folds": result.walkforward_folds,
    }
    walkforward_path(result.cadence).write_text(_safe_json_dumps(wf), encoding="utf-8")
    return p
