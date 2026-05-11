"""
Mean-reversion strategy that ranks SP500 symbols by their *mean* daily RSI(14).

There is no model fit here — at every cadence rebalance date we score each symbol
by the mean of its daily RSI over the last ``rsi_window_days`` (default 30) and
buy the bottom-N (most oversold) names equal-weighted. This is the classical
"RSI mean-reversion" sleeve: low mean RSI → recent weakness → mean-revert higher.

The metrics file produced has the same shape as the ML model's so the
``/strategy/snapshot`` endpoint and the website's Strategies page render it
without special-casing.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from sp500_return_model import (
    CADENCE_HORIZON_DAYS,
    DATA_DIR,
    _equity_curve_payload,
    _json_default,
    _rsi,
    _run_strategy_backtest,
    _sample_dates,
    _spearman_ic,
    _spy_baseline_returns,
    _summary_metrics,
    _yahoo_symbol,
    load_prices,
    split_indices_by_time_fractions,
)

logger = logging.getLogger(__name__)

RSI_DIR = DATA_DIR / "rsi_mean_models"
RSI_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class RsiBacktestConfig:
    cadence: str
    symbols: List[str]
    years: float = 30.0
    split_train_frac: float = 0.5
    split_val_frac: float = 0.25
    split_test_frac: float = 0.25
    top_n: int = 50
    benchmark: str = "SPY"
    rsi_window_days: int = 30  # rolling window we average daily RSI(14) over
    rsi_period: int = 14
    refresh_prices: bool = False


@dataclass
class RsiBacktestResult:
    cadence: str
    strategy_id: str
    weighting: str
    split_train_frac: float
    split_val_frac: float
    split_test_frac: float
    n_train: int
    n_val: int
    n_test: int
    rsi_window_days: int
    rsi_period: int
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


def _build_rsi_panel(
    prices: Dict[str, pd.DataFrame],
    *,
    cadence: str,
    benchmark: str,
    rsi_window_days: int,
    rsi_period: int,
    min_history_days: int = 260,
) -> pd.DataFrame:
    """Return a panel with columns [date, symbol, rsi_mean, score, y]."""
    horizon = CADENCE_HORIZON_DAYS[cadence]
    bench_sym = _yahoo_symbol(benchmark)
    panels: List[pd.DataFrame] = []
    for sym, df in prices.items():
        if sym == bench_sym or df is None or df.empty or len(df) < min_history_days:
            continue
        close = pd.to_numeric(df["Close"], errors="coerce")
        rsi_d = _rsi(close, rsi_period)
        rsi_mean = rsi_d.rolling(rsi_window_days, min_periods=max(5, rsi_window_days // 3)).mean()
        fwd = np.log(close.shift(-horizon) / close)
        sample = _sample_dates(rsi_mean.index, cadence)
        sample = sample.intersection(rsi_mean.dropna().index).intersection(fwd.dropna().index)
        if len(sample) == 0:
            continue
        sub = pd.DataFrame(
            {
                "date": sample,
                "symbol": sym,
                "rsi_mean": rsi_mean.loc[sample].values,
                "y": fwd.loc[sample].values,
            }
        )
        panels.append(sub)
    if not panels:
        raise RuntimeError("no RSI panel rows produced")
    panel = pd.concat(panels, axis=0).reset_index(drop=True)
    # Higher score = more attractive. Mean-reversion → buy oversold, so score = -rsi.
    panel["score"] = -panel["rsi_mean"].astype(float)
    return panel


def evaluate_rsi_mean(cfg: RsiBacktestConfig, prices: Optional[Dict[str, pd.DataFrame]] = None) -> RsiBacktestResult:
    if cfg.cadence not in CADENCE_HORIZON_DAYS:
        raise ValueError(f"unknown cadence {cfg.cadence!r}")
    if prices is None:
        prices = load_prices(
            list(set(cfg.symbols) | {cfg.benchmark}),
            years=cfg.years,
            refresh=cfg.refresh_prices,
        )
    if cfg.benchmark not in prices:
        raise RuntimeError(f"benchmark {cfg.benchmark!r} not loaded")

    panel = _build_rsi_panel(
        prices,
        cadence=cfg.cadence,
        benchmark=cfg.benchmark,
        rsi_window_days=cfg.rsi_window_days,
        rsi_period=cfg.rsi_period,
    )

    tr_idx, va_idx, te_idx = split_indices_by_time_fractions(
        pd.DatetimeIndex(panel["date"].values),
        train_frac=cfg.split_train_frac,
        val_frac=cfg.split_val_frac,
        test_frac=cfg.split_test_frac,
    )
    train_panel = panel.iloc[tr_idx].reset_index(drop=True)
    val_panel = panel.iloc[va_idx].reset_index(drop=True)
    test_panel = panel.iloc[te_idx].reset_index(drop=True)

    if train_panel.empty or val_panel.empty or test_panel.empty:
        raise RuntimeError(
            f"split too small: train={len(train_panel)} val={len(val_panel)} test={len(test_panel)}; need more history"
        )

    train_ic = _spearman_ic(train_panel["score"], train_panel["y"], pd.DatetimeIndex(train_panel["date"]))
    val_ic = _spearman_ic(val_panel["score"], val_panel["y"], pd.DatetimeIndex(val_panel["date"]))
    test_ic = _spearman_ic(test_panel["score"], test_panel["y"], pd.DatetimeIndex(test_panel["date"]))
    train_bt = _run_strategy_backtest(
        train_panel.rename(columns={"score": "pred"}),
        prices,
        cadence=cfg.cadence,
        top_n=cfg.top_n,
        weighting="equal",
        score_col="pred",
    )
    val_bt = _run_strategy_backtest(
        val_panel.rename(columns={"score": "pred"}),
        prices,
        cadence=cfg.cadence,
        top_n=cfg.top_n,
        weighting="equal",
        score_col="pred",
    )
    test_bt = _run_strategy_backtest(
        test_panel.rename(columns={"score": "pred"}),
        prices,
        cadence=cfg.cadence,
        top_n=cfg.top_n,
        weighting="equal",
        score_col="pred",
    )
    train_metrics = _summary_metrics(train_bt["returns"])
    train_metrics["ic"] = train_ic
    train_metrics["turnover_avg"] = train_bt["turnover_avg"]
    val_metrics = _summary_metrics(val_bt["returns"])
    val_metrics["ic"] = val_ic
    val_metrics["turnover_avg"] = val_bt["turnover_avg"]
    test_metrics = _summary_metrics(test_bt["returns"])
    test_metrics["ic"] = test_ic
    test_metrics["turnover_avg"] = test_bt["turnover_avg"]

    base_train = _spy_baseline_returns(prices, train_bt["returns"].index[0], train_bt["returns"].index[-1])
    base_val = _spy_baseline_returns(prices, val_bt["returns"].index[0], val_bt["returns"].index[-1])
    base_test = _spy_baseline_returns(prices, test_bt["returns"].index[0], test_bt["returns"].index[-1])

    # Current top: lowest mean-RSI right now.
    latest_dt = panel["date"].max()
    head = (
        panel[panel["date"] == latest_dt]
        .sort_values("rsi_mean", ascending=True)
        .head(cfg.top_n)
        .reset_index(drop=True)
    )
    current_top = [
        {
            "symbol": str(r.symbol),
            "rsi_mean": float(r.rsi_mean),
            "rank": int(i + 1),
            "weight": 1.0 / max(1, len(head)),
        }
        for i, r in enumerate(head.itertuples(index=False))
    ]

    universe_size = int(len([s for s in prices.keys() if s != cfg.benchmark]))
    return RsiBacktestResult(
        cadence=cfg.cadence,
        strategy_id="rsi_mean",
        weighting="equal",
        split_train_frac=float(cfg.split_train_frac),
        split_val_frac=float(cfg.split_val_frac),
        split_test_frac=float(cfg.split_test_frac),
        n_train=int(len(train_panel)),
        n_val=int(len(val_panel)),
        n_test=int(len(test_panel)),
        rsi_window_days=int(cfg.rsi_window_days),
        rsi_period=int(cfg.rsi_period),
        train_metrics=train_metrics,
        val_metrics=val_metrics,
        test_metrics=test_metrics,
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
        trained_at=datetime.now(timezone.utc).isoformat(),
        universe_size=universe_size,
        history_years=float(cfg.years),
    )


def metrics_path(cadence: str) -> Path:
    return RSI_DIR / f"rsi_mean_model_{cadence}_metrics.json"


def save_artifacts(result: RsiBacktestResult) -> Path:
    p = metrics_path(result.cadence)
    p.write_text(json.dumps(asdict(result), indent=2, default=_json_default), encoding="utf-8")
    return p


def predict_top_n(
    cadence: str,
    n: int = 50,
    *,
    rsi_window_days: int = 30,
    rsi_period: int = 14,
    prices: Optional[Dict[str, pd.DataFrame]] = None,
) -> List[Dict[str, Any]]:
    """Lowest mean-RSI symbols right now; weight is 1/N (equal-weight)."""
    from sp500_return_model import PRICE_CACHE_DIR

    symbols = [p.stem for p in PRICE_CACHE_DIR.glob("*.parquet")]
    if not symbols:
        raise RuntimeError("no cached prices; train ML or download prices first")
    if prices is None:
        prices = load_prices(symbols + ["SPY"], refresh=False)

    rows = []
    for sym, df in prices.items():
        if sym == "SPY" or df is None or df.empty:
            continue
        close = pd.to_numeric(df["Close"], errors="coerce")
        rsi_d = _rsi(close, rsi_period)
        rsi_mean = rsi_d.rolling(rsi_window_days, min_periods=max(5, rsi_window_days // 3)).mean()
        rsi_mean = rsi_mean.dropna()
        if rsi_mean.empty:
            continue
        rows.append({"symbol": sym, "rsi_mean": float(rsi_mean.iloc[-1])})
    if not rows:
        raise RuntimeError("no RSI rows to score")
    df = pd.DataFrame(rows).sort_values("rsi_mean", ascending=True).head(int(n)).reset_index(drop=True)
    w = 1.0 / max(1, len(df))
    return [
        {
            "symbol": str(r.symbol),
            "rsi_mean": float(r.rsi_mean),
            "rank": int(i + 1),
            "weight": float(w),
        }
        for i, r in enumerate(df.itertuples(index=False))
    ]
