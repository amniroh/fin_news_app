"""
Generic train/validation/test pipeline for trading models/algorithms.

Design goals:
- Chronological, non-overlapping splits (train -> validation -> test)
- Tune only on validation; run exactly once on test for final reporting
- Pluggable models (strategies can be trained or purely parameter-tuned)
- Standardized evaluation output (completed TradeLegs + aggregate metrics)

Initial use case: RSI mean-reversion optimizer (`optimize_rsi_mean`).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from telegram_agent.config import DATA_DIR, load_config
from telegram_agent.agent_db import connect, init_db
from telegram_agent.cross_sectional_engine import simulate_cross_sectional_ranked
from telegram_agent.optimize_rsi_mean import (
    RsiMeanParams,
    _load_dotenv_like_other_modules,
    _parse_iso_or_date,
    _symbols_from_competitive_env,
    build_context,
    simulate_fast_cross_sectional,
    symbols_with_bar_in_window,
)
from telegram_agent.signal_strategies import RANKERS, SIGNAL_DOCS, get_ranker
from telegram_agent.rolling_window_metrics import (
    rolling_horizon_returns,
    rolling_metric_key_base,
    rolling_window_to_timedelta,
)
from telegram_agent.strategy_metrics import TradeLeg, compute_aggregate_metrics


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime

    def as_dict(self) -> Dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(frozen=True)
class DataSplits:
    train: TimeWindow
    validation: TimeWindow
    test: TimeWindow

    def as_dict(self) -> Dict[str, Any]:
        return {"train": self.train.as_dict(), "validation": self.validation.as_dict(), "test": self.test.as_dict()}


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def make_chronological_splits_from_ctx(
    ctx,
    *,
    start: datetime,
    end: datetime,
    train_frac: float = 0.50,
    val_frac: float = 0.25,
    test_frac: float = 0.25,
    min_points_per_split: int = 200,
) -> DataSplits:
    """
    Build splits from the *shared* reference timeline in ctx, requiring all selected symbols
    to have finite forward-filled closes (to keep distributions comparable).
    """
    start = _utc(start)
    end = _utc(end)
    times: List[datetime] = list(ctx.ref_times or [])
    if not times:
        raise ValueError("Context has no ref_times; cannot split")

    # Slice by requested overall window.
    i0 = 0
    while i0 < len(times) and _utc(times[i0]) < start:
        i0 += 1
    i1 = len(times) - 1
    while i1 >= 0 and _utc(times[i1]) > end:
        i1 -= 1
    if i1 <= i0:
        raise ValueError("Requested split window has no data on the reference timeline")

    syms = list(ctx.symbols_with_any_data or [])
    if not syms:
        raise ValueError("No symbols with data in context")

    # Valid indices where all symbols have a finite close at that index.
    valid_idx: List[int] = []
    for i in range(i0, i1 + 1):
        ok = True
        for s in syms:
            arr = ctx.closes_ffill.get(s)
            if arr is None:
                ok = False
                break
            v = float(arr[i])
            if v != v or v == float("inf") or v == float("-inf"):  # nan/inf checks without numpy import
                ok = False
                break
        if ok:
            valid_idx.append(i)

    if len(valid_idx) < 3 * min_points_per_split:
        raise ValueError(
            f"Not enough common timeline points for splits: {len(valid_idx)} < {3 * min_points_per_split}"
        )

    # Normalize fractions.
    tr = max(0.05, min(0.90, float(train_frac)))
    va = max(0.05, min(0.90, float(val_frac)))
    te = max(0.05, min(0.90, float(test_frac)))
    s = tr + va + te
    tr, va, te = tr / s, va / s, te / s

    n = len(valid_idx)
    n_train = max(min_points_per_split, int(round(n * tr)))
    n_val = max(min_points_per_split, int(round(n * va)))
    n_test = max(min_points_per_split, n - n_train - n_val)

    # Rebalance to keep order and minimum sizes.
    if n_train + n_val + n_test > n:
        n_test = max(min_points_per_split, n - n_train - n_val)
    if n_train + n_val + n_test > n:
        n_val = max(min_points_per_split, n - n_train - n_test)
    if n_train + n_val + n_test > n:
        n_train = max(min_points_per_split, n - n_val - n_test)
    if n_train + n_val + n_test > n:
        raise ValueError("Unable to allocate splits with requested minimum sizes")

    k1 = n_train
    k2 = n_train + n_val
    if k2 >= n:
        raise ValueError("Split boundaries invalid; adjust fractions or min_points_per_split")

    # Build non-overlapping time windows using the shared timeline timestamps.
    t0 = _utc(times[valid_idx[0]])
    t_train_end = _utc(times[valid_idx[k1 - 1]])
    t_val_start = _utc(times[valid_idx[k1]])
    t_val_end = _utc(times[valid_idx[k2 - 1]])
    t_test_start = _utc(times[valid_idx[k2]])
    t3 = _utc(times[valid_idx[-1]])

    if not (t0 < t_train_end <= t_val_start <= t_val_end <= t_test_start <= t3):
        raise ValueError("Computed splits are not chronological")

    return DataSplits(
        train=TimeWindow(start=t0, end=t_train_end),
        validation=TimeWindow(start=t_val_start, end=t_val_end),
        test=TimeWindow(start=t_test_start, end=t3),
    )


def _prepare_pipeline_context(
    cfg: dict,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    sources: Optional[Sequence[str]],
) -> Any:
    """Build ``OptimizerContext`` and apply ``PIPELINE_MIN_COVERAGE_FRAC`` filtering."""
    ctx = build_context(cfg, symbols=symbols, sources=sources)
    win_start = _utc(start)
    win_end = _utc(end)
    cov: List[Tuple[str, int]] = []
    for s in list(ctx.symbols_with_any_data or []):
        ser = ctx.cache.get(s) or []
        n_in = sum(1 for t, _ in ser if _utc(t) >= win_start and _utc(t) <= win_end)
        cov.append((s, int(n_in)))
    cov.sort(key=lambda x: x[1], reverse=True)
    densest = cov[0][1] if cov else 0
    min_frac = float(cfg.get("pipeline_min_coverage_frac", 0.85))
    min_frac = max(0.10, min(1.0, min_frac))
    min_n = int(max(1, round(densest * min_frac))) if densest > 0 else 1
    kept = [s for (s, n) in cov if n >= min_n]
    if kept:
        ctx.symbols_with_any_data = kept
    return ctx


class ModelAdapter(Protocol):
    name: str

    def tune(self, cfg: dict, *, ctx, splits: DataSplits, seed: int) -> Dict[str, Any]:
        """Return a JSON-serializable dict with finalized parameters and tuning artifacts."""

    def evaluate(self, cfg: dict, *, ctx, splits: DataSplits, finalized: Dict[str, Any]) -> Dict[str, Any]:
        """Run exactly once on test split. Return a JSON-serializable evaluation result."""


class RsiMeanAdapter:
    name = "rsi_mean"

    @staticmethod
    def _sim_max_eval_points(cfg: dict) -> int:
        """
        Match `optimize_rsi_mean.simulate_fast_cross_sectional` stride policy.

        When `optimize_dense_hourly_simulation` is enabled, use a huge max_eval_points so stride=1
        (full hourly resolution). This must stay consistent across tuning (`random_search`) and
        pipeline evaluation (`final_params_metrics` / test).
        """
        if bool(cfg.get("optimize_dense_hourly_simulation", False)):
            return 10**9
        return int(cfg.get("competitive_backtest_max_eval_points", 2000))

    @staticmethod
    def _params_from_dict(p: Dict[str, Any]) -> Tuple[RsiMeanParams, int, int, float, Optional[float], Optional[float]]:
        rp = RsiMeanParams(
            rsi_period=int(p.get("rsi_period", 14)),
            rsi_lo=float(p.get("rsi_lo", 20.0)),
            rsi_hi=float(p.get("rsi_hi", 55.0)),
            mom_lookback=int(p.get("mom_lookback", 20)),
            mom_max=float(p.get("mom_max", 2.0)),
            rsi_target=float(p.get("rsi_target", 45.0)),
            mom_scale=float(p.get("mom_scale", 5.0)),
        )
        top_k = int(p.get("top_k", 5))
        min_bars = int(p.get("min_bars", 80))
        exposure = float(p.get("exposure", 1.0))
        dd_stop = p.get("dd_stop", None)
        dd_resume = p.get("dd_resume", None)
        dd_stop_f = None if dd_stop in (None, "") else float(dd_stop)
        dd_resume_f = None if dd_resume in (None, "") else float(dd_resume)
        return rp, top_k, min_bars, exposure, dd_stop_f, dd_resume_f

    @staticmethod
    def _enabled_metrics(cfg: dict) -> set[str]:
        raw_enabled = cfg.get("test_metrics_enabled")
        if isinstance(raw_enabled, str):
            return {x.strip().lower() for x in raw_enabled.split(",") if x.strip()}
        if isinstance(raw_enabled, list):
            return {str(x).strip().lower() for x in raw_enabled if str(x).strip()}
        return {"sharpe", "alpha", "max_drawdown", "oos_sharpe", "calmar", "significance"}

    @classmethod
    def _evaluate_params_on_window(
        cls,
        cfg: dict,
        *,
        ctx,
        window: TimeWindow,
        params_dict: Dict[str, Any],
        symbols_pool: Sequence[str],
    ) -> Dict[str, Any]:
        rp, top_k, min_bars, exposure, dd_stop_f, dd_resume_f = cls._params_from_dict(params_dict)
        # Must match `optimize_rsi_mean.evaluate_config` / `random_search`: only names that printed
        # in this window. Forward-filled ref-grid prices alone are not a substitute.
        syms = symbols_with_bar_in_window(ctx, window.start, window.end)
        suf = str(cfg.get("optimize_rolling_metric_suffix", "1y"))
        mk = rolling_metric_key_base(suf)
        if not syms:
            return {
                "window": window.as_dict(),
                "evaluation_note": "no_symbols_with_observed_bar_in_window",
                "basket_definition": {
                    "symbol": "BASKET",
                    "meaning": (
                        "A single synthetic portfolio leg representing the equal-weight basket of the model’s top-K picks "
                        "held over each rebalance interval."
                    ),
                    "symbols_pool": [],
                    "symbols_after_coverage": list(symbols_pool),
                    "rebalance_interval": "1h",
                    "weighting": "equal_weight",
                    "selection": {
                        "method": "rsi_mean_score_rank",
                        "top_k": int(top_k),
                        "min_bars": int(min_bars),
                        "params": asdict(rp),
                    },
                    "execution": {
                        "exposure": float(exposure),
                        "drawdown_overlay": {"dd_stop": dd_stop_f, "dd_resume": dd_resume_f},
                    },
                },
                "final_params": dict(params_dict),
                "n_hours": 0,
                "n_legs": 0,
                "aggregate_metrics": {"n_legs": 0, "note": "no_symbols_with_observed_bar_in_window"},
                "rolling_metrics": {
                    "rolling_window": str(cfg.get("optimize_rolling_window", "1y")),
                    "rolling_metric_suffix": suf,
                    "rolling_metric_keys": dict(mk),
                    mk["median_return"]: None,
                    mk["hit_rate"]: None,
                    "n_roll_windows": 0,
                },
            }

        curve, legs = simulate_fast_cross_sectional(
            ctx,
            syms,
            start=window.start,
            end=window.end,
            min_bars=min_bars,
            top_k=top_k,
            params=rp,
            exposure=exposure,
            dd_stop=dd_stop_f,
            dd_resume=dd_resume_f,
            max_eval_points=int(cls._sim_max_eval_points(cfg)),
            grid_offset=0,
        )

        enabled = cls._enabled_metrics(cfg)
        bench = str(cfg.get("test_benchmark_symbol") or "SPY").strip().upper()
        rf = float(cfg.get("test_risk_free_annual", 0.04))
        oos = float(cfg.get("test_oos_split", 0.5))
        con = None
        try:
            db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
            con = connect(db)
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
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass

        rw = rolling_window_to_timedelta(str(cfg.get("optimize_rolling_window", "1y")))
        ts = [t for t, _ in curve]
        eq = [float(x) for _, x in curve]
        roll = rolling_horizon_returns(ts, eq, rw) if curve else []
        med = None
        hit = None
        if roll:
            sr = sorted(float(x) for x in roll)
            med = float(sr[len(sr) // 2])
            hit = float(sum(1 for x in sr if x > 0) / len(sr))

        return {
            "window": window.as_dict(),
            "basket_definition": {
                "symbol": "BASKET",
                "meaning": (
                    "A single synthetic portfolio leg representing the equal-weight basket of the model’s top-K picks "
                    "held over each rebalance interval."
                ),
                "symbols_pool": list(syms),
                "symbols_after_coverage": list(symbols_pool),
                "rebalance_interval": "1h",
                "weighting": "equal_weight",
                "selection": {
                    "method": "rsi_mean_score_rank",
                    "top_k": int(top_k),
                    "min_bars": int(min_bars),
                    "params": asdict(rp),
                },
                "execution": {
                    "exposure": float(exposure),
                    "drawdown_overlay": {"dd_stop": dd_stop_f, "dd_resume": dd_resume_f},
                },
            },
            "final_params": dict(params_dict),
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
        }

    def tune(self, cfg: dict, *, ctx, splits: DataSplits, seed: int) -> Dict[str, Any]:
        # Parameter tuning uses validation only (no leakage from test).
        from telegram_agent.optimize_rsi_mean import random_search

        trials = int(cfg.get("pipeline_tune_trials", 250))
        symbols_pool = list(ctx.symbols_with_any_data or [])
        report = random_search(
            cfg,
            symbols=symbols_pool,
            start=splits.validation.start,
            end=splits.validation.end,
            trials=trials,
            seed=int(seed),
            sources=list(ctx.sources_filter) if ctx.sources_filter else None,
        )
        best = report.get("best_feasible") or report.get("best_overall") or {}
        params = (best.get("params") or {}) if isinstance(best, dict) else {}
        final_metrics = self._evaluate_params_on_window(
            cfg,
            ctx=ctx,
            window=splits.validation,
            params_dict=params if isinstance(params, dict) else {},
            symbols_pool=symbols_pool,
        )
        return {
            "adapter": self.name,
            "tune_window": splits.validation.as_dict(),
            "symbols_pool": symbols_pool,
            "symbols_with_observed_bar_in_tune_window": symbols_with_bar_in_window(
                ctx, splits.validation.start, splits.validation.end
            ),
            "tune_trials": trials,
            "tune_report": {
                "objective_name": report.get("objective_name"),
                "constraints": report.get("constraints"),
                "rolling_window": report.get("rolling_window"),
                "rolling_metric_suffix": report.get("rolling_metric_suffix"),
                "rolling_metric_keys": report.get("rolling_metric_keys"),
                "best_feasible": report.get("best_feasible"),
                "best_overall": report.get("best_overall"),
            },
            "final_params": params,
            "final_params_metrics": final_metrics,
        }

    def evaluate(self, cfg: dict, *, ctx, splits: DataSplits, finalized: Dict[str, Any]) -> Dict[str, Any]:
        p = finalized.get("final_params") or {}
        rp, top_k, min_bars, exposure, dd_stop_f, dd_resume_f = self._params_from_dict(p)

        symbols_after_coverage = list(ctx.symbols_with_any_data)
        syms = symbols_with_bar_in_window(ctx, splits.test.start, splits.test.end)
        suf = str(cfg.get("optimize_rolling_metric_suffix", "1y"))
        mk = rolling_metric_key_base(suf)
        if not syms:
            return {
                "adapter": self.name,
                "test_window": splits.test.as_dict(),
                "evaluation_note": "no_symbols_with_observed_bar_in_window",
                "basket_definition": {
                    "symbol": "BASKET",
                    "meaning": (
                        "A single synthetic portfolio leg representing the equal-weight basket of the model’s top-K picks "
                        "held over each rebalance interval."
                    ),
                    "symbols_pool": [],
                    "symbols_after_coverage": symbols_after_coverage,
                    "rebalance_interval": "1h",
                    "weighting": "equal_weight",
                    "selection": {
                        "method": "rsi_mean_score_rank",
                        "top_k": int(top_k),
                        "min_bars": int(min_bars),
                        "params": asdict(rp),
                    },
                    "execution": {
                        "exposure": float(exposure),
                        "drawdown_overlay": {"dd_stop": dd_stop_f, "dd_resume": dd_resume_f},
                    },
                },
                "final_params": p,
                "n_hours": 0,
                "n_legs": 0,
                "legs": [],
                "aggregate_metrics": {"n_legs": 0, "note": "no_symbols_with_observed_bar_in_window"},
                "rolling_metrics": {
                    "rolling_window": str(cfg.get("optimize_rolling_window", "1y")),
                    "rolling_metric_suffix": suf,
                    "rolling_metric_keys": dict(mk),
                    mk["median_return"]: None,
                    mk["hit_rate"]: None,
                    "n_roll_windows": 0,
                },
            }

        curve, legs = simulate_fast_cross_sectional(
            ctx,
            syms,
            start=splits.test.start,
            end=splits.test.end,
            min_bars=min_bars,
            top_k=top_k,
            params=rp,
            exposure=exposure,
            dd_stop=dd_stop_f,
            dd_resume=dd_resume_f,
            max_eval_points=int(self._sim_max_eval_points(cfg)),
            grid_offset=0,
        )

        # Aggregate metrics from completed legs (generic schema).
        enabled = self._enabled_metrics(cfg)
        bench = str(cfg.get("test_benchmark_symbol") or "SPY").strip().upper()
        rf = float(cfg.get("test_risk_free_annual", 0.04))
        oos = float(cfg.get("test_oos_split", 0.5))
        # Some metrics (e.g. alpha) need DB access for benchmark prices.
        con = None
        try:
            db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
            con = connect(db)
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
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass

        # Rolling horizon metrics on the equity curve (optimization-style metrics).
        rw = rolling_window_to_timedelta(str(cfg.get("optimize_rolling_window", "1y")))
        ts = [t for t, _ in curve]
        eq = [float(x) for _, x in curve]
        roll = rolling_horizon_returns(ts, eq, rw) if curve else []
        med = None
        hit = None
        if roll:
            sr = sorted(float(x) for x in roll)
            med = float(sr[len(sr) // 2])
            hit = float(sum(1 for x in sr if x > 0) / len(sr))

        return {
            "adapter": self.name,
            "test_window": splits.test.as_dict(),
            "basket_definition": {
                "symbol": "BASKET",
                "meaning": (
                    "A single synthetic portfolio leg representing the equal-weight basket of the model’s top-K picks "
                    "held over each rebalance interval."
                ),
                "symbols_pool": list(syms),
                "symbols_after_coverage": symbols_after_coverage,
                "rebalance_interval": "1h",
                "weighting": "equal_weight",
                "selection": {
                    "method": "rsi_mean_score_rank",
                    "top_k": int(top_k),
                    "min_bars": int(min_bars),
                    "params": asdict(rp),
                },
                "execution": {
                    "exposure": float(exposure),
                    "drawdown_overlay": {"dd_stop": dd_stop_f, "dd_resume": dd_resume_f},
                },
            },
            "final_params": p,
            "n_hours": len(curve),
            "n_legs": len(legs),
            "legs": [asdict(l) for l in legs],
            "aggregate_metrics": agg,
            "rolling_metrics": {
                "rolling_window": str(cfg.get("optimize_rolling_window", "1y")),
                "rolling_metric_suffix": suf,
                "rolling_metric_keys": dict(mk),
                mk["median_return"]: med,
                mk["hit_rate"]: hit,
                "n_roll_windows": len(roll),
            },
        }


class GenericSignalAdapter:
    """Cross-sectional signal from ``signal_strategies`` (MACD, Bollinger, value PE, etc.)."""

    def __init__(self, signal_key: str) -> None:
        if signal_key not in RANKERS:
            raise ValueError(f"Unknown signal adapter {signal_key!r}. Keys: {sorted(RANKERS)}")
        self.signal_key = signal_key
        self.name = signal_key

    @staticmethod
    def _sim_max_eval_points(cfg: dict) -> int:
        return RsiMeanAdapter._sim_max_eval_points(cfg)

    @staticmethod
    def _exec_from_params(p: Dict[str, Any]) -> Tuple[Dict[str, Any], int, int, float, Optional[float], Optional[float]]:
        d = dict(p)
        top_k = int(d.get("top_k", 5))
        min_bars = int(d.get("min_bars", 50))
        exposure = float(d.get("exposure", 1.0))
        dd_stop = d.get("dd_stop", None)
        dd_resume = d.get("dd_resume", None)
        dd_stop_f = None if dd_stop in (None, "") else float(dd_stop)
        dd_resume_f = None if dd_resume in (None, "") else float(dd_resume)
        return d, top_k, min_bars, exposure, dd_stop_f, dd_resume_f

    def _evaluate_params_on_window(
        self,
        cfg: dict,
        *,
        ctx,
        window: TimeWindow,
        params_dict: Dict[str, Any],
        symbols_pool: Sequence[str],
    ) -> Dict[str, Any]:
        p, top_k, min_bars, exposure, dd_stop_f, dd_resume_f = self._exec_from_params(params_dict)
        rank_fn = get_ranker(self.signal_key)
        syms = symbols_with_bar_in_window(ctx, window.start, window.end)
        suf = str(cfg.get("optimize_rolling_metric_suffix", "1y"))
        mk = rolling_metric_key_base(suf)
        if not syms:
            return {
                "window": window.as_dict(),
                "evaluation_note": "no_symbols_with_observed_bar_in_window",
                "basket_definition": {
                    "symbol": "BASKET",
                    "meaning": SIGNAL_DOCS.get(self.signal_key, self.signal_key),
                    "symbols_pool": [],
                    "symbols_after_coverage": list(symbols_pool),
                    "rebalance_interval": "1h",
                    "weighting": "equal_weight",
                    "selection": {"method": self.signal_key, "top_k": int(top_k), "min_bars": int(min_bars), "params": p},
                    "execution": {
                        "exposure": float(exposure),
                        "drawdown_overlay": {"dd_stop": dd_stop_f, "dd_resume": dd_resume_f},
                    },
                },
                "final_params": dict(params_dict),
                "n_hours": 0,
                "n_legs": 0,
                "aggregate_metrics": {"n_legs": 0, "note": "no_symbols_with_observed_bar_in_window"},
                "rolling_metrics": {
                    "rolling_window": str(cfg.get("optimize_rolling_window", "1y")),
                    "rolling_metric_suffix": suf,
                    "rolling_metric_keys": dict(mk),
                    mk["median_return"]: None,
                    mk["hit_rate"]: None,
                    "n_roll_windows": 0,
                },
            }

        curve, legs = simulate_cross_sectional_ranked(
            ctx,
            syms,
            start=window.start,
            end=window.end,
            min_bars=min_bars,
            top_k=top_k,
            params=p,
            exposure=exposure,
            dd_stop=dd_stop_f,
            dd_resume=dd_resume_f,
            max_eval_points=int(self._sim_max_eval_points(cfg)),
            grid_offset=0,
            rank_fn=rank_fn,
            cfg=cfg,
        )

        enabled = RsiMeanAdapter._enabled_metrics(cfg)
        bench = str(cfg.get("test_benchmark_symbol") or "SPY").strip().upper()
        rf = float(cfg.get("test_risk_free_annual", 0.04))
        oos = float(cfg.get("test_oos_split", 0.5))
        con = None
        try:
            db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
            con = connect(db)
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
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass

        rw = rolling_window_to_timedelta(str(cfg.get("optimize_rolling_window", "1y")))
        ts = [t for t, _ in curve]
        eq = [float(x) for _, x in curve]
        roll = rolling_horizon_returns(ts, eq, rw) if curve else []
        med = hit = None
        if roll:
            sr = sorted(float(x) for x in roll)
            med = float(sr[len(sr) // 2])
            hit = float(sum(1 for x in sr if x > 0) / len(sr))

        return {
            "window": window.as_dict(),
            "basket_definition": {
                "symbol": "BASKET",
                "meaning": SIGNAL_DOCS.get(self.signal_key, self.signal_key),
                "symbols_pool": list(syms),
                "symbols_after_coverage": list(symbols_pool),
                "rebalance_interval": "1h",
                "weighting": "equal_weight",
                "selection": {"method": self.signal_key, "top_k": int(top_k), "min_bars": int(min_bars), "params": p},
                "execution": {
                    "exposure": float(exposure),
                    "drawdown_overlay": {"dd_stop": dd_stop_f, "dd_resume": dd_resume_f},
                },
            },
            "final_params": dict(params_dict),
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
        }

    def tune(self, cfg: dict, *, ctx, splits: DataSplits, seed: int) -> Dict[str, Any]:
        from telegram_agent.optimize_generic_signal import random_search_generic_signal

        trials = int(cfg.get("pipeline_tune_trials", 250))
        symbols_pool = list(ctx.symbols_with_any_data or [])
        report = random_search_generic_signal(
            cfg,
            ctx,
            symbols=symbols_pool,
            start=splits.validation.start,
            end=splits.validation.end,
            signal_key=self.signal_key,
            trials=trials,
            seed=int(seed),
        )
        best = report.get("best_feasible") or report.get("best_overall") or {}
        params = (best.get("params") or {}) if isinstance(best, dict) else {}
        final_metrics = self._evaluate_params_on_window(
            cfg,
            ctx=ctx,
            window=splits.validation,
            params_dict=params if isinstance(params, dict) else {},
            symbols_pool=symbols_pool,
        )
        return {
            "adapter": self.name,
            "tune_window": splits.validation.as_dict(),
            "symbols_pool": symbols_pool,
            "symbols_with_observed_bar_in_tune_window": symbols_with_bar_in_window(
                ctx, splits.validation.start, splits.validation.end
            ),
            "tune_trials": trials,
            "tune_report": {
                "objective_name": report.get("objective_name"),
                "constraints": report.get("constraints"),
                "rolling_window": report.get("rolling_window"),
                "rolling_metric_suffix": report.get("rolling_metric_suffix"),
                "rolling_metric_keys": report.get("rolling_metric_keys"),
                "signal": report.get("signal"),
                "best_feasible": report.get("best_feasible"),
                "best_overall": report.get("best_overall"),
            },
            "final_params": params,
            "final_params_metrics": final_metrics,
            "signal_doc": SIGNAL_DOCS.get(self.signal_key, ""),
        }

    def evaluate(self, cfg: dict, *, ctx, splits: DataSplits, finalized: Dict[str, Any]) -> Dict[str, Any]:
        p0 = finalized.get("final_params") or {}
        p, top_k, min_bars, exposure, dd_stop_f, dd_resume_f = self._exec_from_params(p0)
        rank_fn = get_ranker(self.signal_key)
        symbols_after_coverage = list(ctx.symbols_with_any_data)
        syms = symbols_with_bar_in_window(ctx, splits.test.start, splits.test.end)
        suf = str(cfg.get("optimize_rolling_metric_suffix", "1y"))
        mk = rolling_metric_key_base(suf)
        if not syms:
            return {
                "adapter": self.name,
                "test_window": splits.test.as_dict(),
                "evaluation_note": "no_symbols_with_observed_bar_in_window",
                "basket_definition": {
                    "symbol": "BASKET",
                    "meaning": SIGNAL_DOCS.get(self.signal_key, self.signal_key),
                    "symbols_pool": [],
                    "symbols_after_coverage": symbols_after_coverage,
                    "rebalance_interval": "1h",
                    "weighting": "equal_weight",
                    "selection": {"method": self.signal_key, "top_k": int(top_k), "min_bars": int(min_bars), "params": p},
                    "execution": {
                        "exposure": float(exposure),
                        "drawdown_overlay": {"dd_stop": dd_stop_f, "dd_resume": dd_resume_f},
                    },
                },
                "final_params": p0,
                "n_hours": 0,
                "n_legs": 0,
                "legs": [],
                "aggregate_metrics": {"n_legs": 0, "note": "no_symbols_with_observed_bar_in_window"},
                "rolling_metrics": {
                    "rolling_window": str(cfg.get("optimize_rolling_window", "1y")),
                    "rolling_metric_suffix": suf,
                    "rolling_metric_keys": dict(mk),
                    mk["median_return"]: None,
                    mk["hit_rate"]: None,
                    "n_roll_windows": 0,
                },
            }

        curve, legs = simulate_cross_sectional_ranked(
            ctx,
            syms,
            start=splits.test.start,
            end=splits.test.end,
            min_bars=min_bars,
            top_k=top_k,
            params=p,
            exposure=exposure,
            dd_stop=dd_stop_f,
            dd_resume=dd_resume_f,
            max_eval_points=int(self._sim_max_eval_points(cfg)),
            grid_offset=0,
            rank_fn=rank_fn,
            cfg=cfg,
        )

        enabled = RsiMeanAdapter._enabled_metrics(cfg)
        bench = str(cfg.get("test_benchmark_symbol") or "SPY").strip().upper()
        rf = float(cfg.get("test_risk_free_annual", 0.04))
        oos = float(cfg.get("test_oos_split", 0.5))
        con = None
        try:
            db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
            con = connect(db)
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
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass

        rw = rolling_window_to_timedelta(str(cfg.get("optimize_rolling_window", "1y")))
        ts = [t for t, _ in curve]
        eq = [float(x) for _, x in curve]
        roll = rolling_horizon_returns(ts, eq, rw) if curve else []
        med = hit = None
        if roll:
            sr = sorted(float(x) for x in roll)
            med = float(sr[len(sr) // 2])
            hit = float(sum(1 for x in sr if x > 0) / len(sr))

        return {
            "adapter": self.name,
            "test_window": splits.test.as_dict(),
            "basket_definition": {
                "symbol": "BASKET",
                "meaning": SIGNAL_DOCS.get(self.signal_key, self.signal_key),
                "symbols_pool": list(syms),
                "symbols_after_coverage": symbols_after_coverage,
                "rebalance_interval": "1h",
                "weighting": "equal_weight",
                "selection": {"method": self.signal_key, "top_k": int(top_k), "min_bars": int(min_bars), "params": p},
                "execution": {
                    "exposure": float(exposure),
                    "drawdown_overlay": {"dd_stop": dd_stop_f, "dd_resume": dd_resume_f},
                },
            },
            "final_params": p0,
            "n_hours": len(curve),
            "n_legs": len(legs),
            "legs": [asdict(l) for l in legs],
            "aggregate_metrics": agg,
            "rolling_metrics": {
                "rolling_window": str(cfg.get("optimize_rolling_window", "1y")),
                "rolling_metric_suffix": suf,
                "rolling_metric_keys": dict(mk),
                mk["median_return"]: med,
                mk["hit_rate"]: hit,
                "n_roll_windows": len(roll),
            },
        }


class RsiMeanWalkForwardAdapter(RsiMeanAdapter):
    """
    Walk-forward optimization on the test split.

    - Tune once on validation for initial params (same as RsiMeanAdapter).
    - On test: invest for ``retune_days`` using current params, then re-tune using all data
      available up to that retune timestamp (expanding window; no future leakage).
    """

    name = "rsi_mean_walk_forward"

    def evaluate(self, cfg: dict, *, ctx, splits: DataSplits, finalized: Dict[str, Any]) -> Dict[str, Any]:
        from telegram_agent.optimize_rsi_mean import random_search

        # Config knobs (kept in cfg so env can override without changing CLI).
        retune_days = int(cfg.get("pipeline_wfo_retune_days", 30))
        retune_days = max(1, min(365, retune_days))
        tune_trials = cfg.get("pipeline_wfo_tune_trials")
        tune_trials = int(tune_trials) if tune_trials not in (None, "") else int(cfg.get("pipeline_tune_trials", 250))
        tune_trials = max(10, min(5000, tune_trials))

        current_params = dict(finalized.get("final_params") or {})

        seg_start = _utc(splits.test.start)
        test_end = _utc(splits.test.end)
        symbols_pool = list(ctx.symbols_with_any_data)
        equity_mul = 1.0
        curve_all: List[Tuple[datetime, float]] = []
        legs_all: List[TradeLeg] = []
        cycles: List[Dict[str, Any]] = []

        cycle_idx = 0
        while seg_start < test_end:
            seg_end = min(test_end, seg_start + timedelta(days=retune_days))

            rp, top_k, min_bars, exposure, dd_stop_f, dd_resume_f = self._params_from_dict(current_params)
            syms_seg = symbols_with_bar_in_window(ctx, seg_start, seg_end)
            curve_seg, legs_seg = simulate_fast_cross_sectional(
                ctx,
                syms_seg,
                start=seg_start,
                end=seg_end,
                min_bars=min_bars,
                top_k=top_k,
                params=rp,
                exposure=exposure,
                dd_stop=dd_stop_f,
                dd_resume=dd_resume_f,
                max_eval_points=int(self._sim_max_eval_points(cfg)),
                grid_offset=0,
            )

            # Stitch equity curve (segment curves start at 1.0; we scale by current equity_mul).
            if curve_seg:
                for t, e in curve_seg:
                    curve_all.append((_utc(t), float(equity_mul) * float(e)))
                equity_mul = float(equity_mul) * float(curve_seg[-1][1])
            legs_all.extend(list(legs_seg or []))

            cycles.append(
                {
                    "cycle_index": cycle_idx,
                    "invest_window": {"start": seg_start.isoformat(), "end": seg_end.isoformat()},
                    "params_used": dict(current_params),
                    "symbols_with_observed_bar_in_segment": list(syms_seg),
                    "n_hours": len(curve_seg),
                    "n_legs": len(legs_seg),
                    "equity_mul_end": equity_mul,
                }
            )

            # Retune at seg_end (unless we just finished test).
            if seg_end >= test_end:
                break

            # Expanding tuning window: all data available up to seg_end (no future).
            # This includes train+val and any already-observed portion of test.
            report = random_search(
                cfg,
                symbols=symbols_pool,
                start=_utc(splits.train.start),
                end=_utc(seg_end),
                trials=tune_trials,
                seed=int(cfg.get("pipeline_seed", 0) or 0) + 1000 + cycle_idx,
                sources=list(ctx.sources_filter) if ctx.sources_filter else None,
            )
            best = report.get("best_feasible") or report.get("best_overall") or {}
            new_params = (best.get("params") or {}) if isinstance(best, dict) else {}
            cycles[-1]["retune_at"] = seg_end.isoformat()
            cycles[-1]["retune_trials"] = tune_trials
            cycles[-1]["retune_objective_name"] = report.get("objective_name")
            cycles[-1]["retune_best_feasible"] = report.get("best_feasible")
            cycles[-1]["retune_best_overall"] = report.get("best_overall")
            cycles[-1]["new_params"] = dict(new_params) if isinstance(new_params, dict) else {}

            if isinstance(new_params, dict) and new_params:
                current_params = dict(new_params)

            seg_start = seg_end
            cycle_idx += 1

        # Aggregate metrics from completed legs (generic schema).
        enabled = self._enabled_metrics(cfg)
        bench = str(cfg.get("test_benchmark_symbol") or "SPY").strip().upper()
        rf = float(cfg.get("test_risk_free_annual", 0.04))
        oos = float(cfg.get("test_oos_split", 0.5))
        con = None
        try:
            db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
            con = connect(db)
            init_db(con)
            agg = compute_aggregate_metrics(
                con,
                legs_all,
                benchmark_symbol=bench,
                risk_free_annual=rf,
                oos_split=oos,
                enabled=enabled,
            )
        finally:
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass

        # Rolling horizon metrics on the stitched equity curve.
        rw = rolling_window_to_timedelta(str(cfg.get("optimize_rolling_window", "1y")))
        suf = str(cfg.get("optimize_rolling_metric_suffix", "1y"))
        mk = rolling_metric_key_base(suf)
        ts = [t for t, _ in curve_all]
        eq = [float(x) for _, x in curve_all]
        roll = rolling_horizon_returns(ts, eq, rw) if curve_all else []
        med = None
        hit = None
        if roll:
            sr = sorted(float(x) for x in roll)
            med = float(sr[len(sr) // 2])
            hit = float(sum(1 for x in sr if x > 0) / len(sr))

        return {
            "adapter": self.name,
            "test_window": splits.test.as_dict(),
            "basket_definition": {
                "symbol": "BASKET",
                "meaning": (
                    "A single synthetic portfolio leg representing the equal-weight basket of the model’s top-K picks "
                    "held over each rebalance interval."
                ),
                "symbols_after_coverage": symbols_pool,
                "rebalance_interval": "1h",
                "weighting": "equal_weight",
                "selection": {"method": "rsi_mean_score_rank"},
                "note": "Per-segment tradable universe is `symbols_with_observed_bar_in_segment` on each walk_forward.cycles[] row.",
            },
            "walk_forward": {
                "retune_days": retune_days,
                "tune_trials_per_retune": tune_trials,
                "cycles": cycles,
            },
            "final_params_last": dict(current_params),
            "n_hours": len(curve_all),
            "n_legs": len(legs_all),
            "legs": [asdict(l) for l in legs_all],
            "aggregate_metrics": agg,
            "rolling_metrics": {
                "rolling_window": str(cfg.get("optimize_rolling_window", "1y")),
                "rolling_metric_suffix": suf,
                "rolling_metric_keys": dict(mk),
                mk["median_return"]: med,
                mk["hit_rate"]: hit,
                "n_roll_windows": len(roll),
            },
        }


def _apply_pipeline_optimizer_defaults(cfg: dict) -> None:
    if "optimize_dense_hourly_simulation" not in cfg:
        cfg["optimize_dense_hourly_simulation"] = bool(cfg.get("pipeline_optimize_dense_hourly_simulation", True))
    if "optimize_deterministic_simulation" not in cfg:
        cfg["optimize_deterministic_simulation"] = bool(cfg.get("pipeline_optimize_deterministic_simulation", True))


def run_pipeline(
    cfg: dict,
    *,
    adapter: ModelAdapter,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    train_frac: float = 0.50,
    val_frac: float = 0.25,
    test_frac: float = 0.25,
    seed: int = 7,
    sources: Optional[Sequence[str]] = None,
    ctx: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Single-adapter run. Pass ``ctx`` to reuse the same ``OptimizerContext`` (e.g. multi-adapter batch).
    """
    _apply_pipeline_optimizer_defaults(cfg)
    if ctx is None:
        ctx = _prepare_pipeline_context(cfg, symbols=symbols, start=start, end=end, sources=sources)

    splits = make_chronological_splits_from_ctx(
        ctx,
        start=start,
        end=end,
        train_frac=train_frac,
        val_frac=val_frac,
        test_frac=test_frac,
    )
    tuned = adapter.tune(cfg, ctx=ctx, splits=splits, seed=int(seed))
    tested = adapter.evaluate(cfg, ctx=ctx, splits=splits, finalized=tuned)
    return {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "symbols_requested": list(symbols),
        "symbols_with_any_data": list(ctx.symbols_with_any_data),
        "sources_filter": list(ctx.sources_filter) if ctx.sources_filter else None,
        "splits": splits.as_dict(),
        "tuning": tuned,
        "test": tested,
    }


def run_pipeline_multi(
    cfg: dict,
    *,
    adapters: Sequence[ModelAdapter],
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    train_frac: float = 0.50,
    val_frac: float = 0.25,
    test_frac: float = 0.25,
    seed: int = 7,
    sources: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Run several adapters on one shared context and identical chronological splits."""
    _apply_pipeline_optimizer_defaults(cfg)
    ctx = _prepare_pipeline_context(cfg, symbols=symbols, start=start, end=end, sources=sources)
    splits = make_chronological_splits_from_ctx(
        ctx,
        start=start,
        end=end,
        train_frac=train_frac,
        val_frac=val_frac,
        test_frac=test_frac,
    )
    runs: List[Dict[str, Any]] = []
    for adapter in adapters:
        tuned = adapter.tune(cfg, ctx=ctx, splits=splits, seed=int(seed))
        tested = adapter.evaluate(cfg, ctx=ctx, splits=splits, finalized=tuned)
        runs.append({"adapter": adapter.name, "tuning": tuned, "test": tested})
    return {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "symbols_requested": list(symbols),
        "symbols_with_any_data": list(ctx.symbols_with_any_data),
        "sources_filter": list(ctx.sources_filter) if ctx.sources_filter else None,
        "splits": splits.as_dict(),
        "runs": runs,
    }


def resolve_adapter(name: str) -> ModelAdapter:
    """Construct a pipeline adapter by CLI name (``rsi_mean``, ``signal_macd``, …)."""
    n = (name or "").strip().lower()
    if n == "rsi_mean":
        return RsiMeanAdapter()
    if n in ("rsi_mean_walk_forward", "rsi_mean_wfo", "rsi_mean_walkforward"):
        return RsiMeanWalkForwardAdapter()
    if n in RANKERS:
        return GenericSignalAdapter(n)
    raise ValueError(
        f"Unknown adapter {name!r}. Use rsi_mean, rsi_mean_walk_forward, or one of: {sorted(RANKERS)}"
    )


def resolve_adapters_csv(arg: str) -> List[ModelAdapter]:
    parts = [x.strip().lower() for x in (arg or "").split(",") if x.strip()]
    if not parts:
        raise ValueError("empty adapter list")
    return [resolve_adapter(p) for p in parts]


def _flatten_metrics_row(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Single-row summary for comparing approaches.
    """
    row: Dict[str, Any] = {
        "run_id": report.get("run_id"),
        "adapter": (report.get("test") or {}).get("adapter"),
        "train_start": (((report.get("splits") or {}).get("train") or {}).get("start")),
        "train_end": (((report.get("splits") or {}).get("train") or {}).get("end")),
        "val_start": (((report.get("splits") or {}).get("validation") or {}).get("start")),
        "val_end": (((report.get("splits") or {}).get("validation") or {}).get("end")),
        "test_start": (((report.get("splits") or {}).get("test") or {}).get("start")),
        "test_end": (((report.get("splits") or {}).get("test") or {}).get("end")),
        "symbols_requested": ",".join(report.get("symbols_requested") or []),
        "symbols_with_any_data_n": len(report.get("symbols_with_any_data") or []),
    }
    test = report.get("test") or {}
    row["n_legs"] = test.get("n_legs")
    row["n_hours"] = test.get("n_hours")

    agg = test.get("aggregate_metrics") or {}
    for k, v in agg.items():
        if k in ("benchmark_symbol", "risk_free_annual", "oos_split", "note"):
            continue
        row[f"agg_{k}"] = v

    rm = test.get("rolling_metrics") or {}
    for k, v in rm.items():
        if k in ("rolling_metric_keys",):
            continue
        row[f"roll_{k}"] = v
    return row


def _flatten_multi_run_report(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One CSV row per adapter for ``run_pipeline_multi`` output."""
    rows: List[Dict[str, Any]] = []
    base = {
        "run_id": report.get("run_id"),
        "symbols_requested": ",".join(report.get("symbols_requested") or []),
        "symbols_with_any_data_n": len(report.get("symbols_with_any_data") or []),
    }
    sp = report.get("splits") or {}
    base["train_start"] = (sp.get("train") or {}).get("start")
    base["train_end"] = (sp.get("train") or {}).get("end")
    base["val_start"] = (sp.get("validation") or {}).get("start")
    base["val_end"] = (sp.get("validation") or {}).get("end")
    base["test_start"] = (sp.get("test") or {}).get("start")
    base["test_end"] = (sp.get("test") or {}).get("end")
    for run in report.get("runs") or []:
        row = dict(base)
        row["adapter"] = run.get("adapter")
        sub = _flatten_metrics_row(
            {
                "run_id": report.get("run_id"),
                "splits": report.get("splits"),
                "symbols_requested": report.get("symbols_requested"),
                "symbols_with_any_data": report.get("symbols_with_any_data"),
                "tuning": run.get("tuning"),
                "test": run.get("test"),
            }
        )
        for k, v in sub.items():
            if k not in row or k in ("adapter",):
                row[k] = v
        rows.append(row)
    return rows


def _default_pipeline_output_stem(adapter_name: str, *, utc_now: Optional[datetime] = None) -> str:
    """Basename fragment for artifacts: ``pipeline_<approach>_<UTC>`` (filesystem-safe)."""
    now = utc_now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^0-9a-z]+", "_", (adapter_name or "run").lower()).strip("_") or "run"
    return f"pipeline_{slug}_{stamp}"


def _resolve_pipeline_output_paths(
    adapter_name: str,
    out_json: Optional[str],
    out_csv: Optional[str],
    *,
    utc_now: Optional[datetime] = None,
) -> Tuple[Path, Path]:
    """
    Default both outputs to the same stem under ``DATA_DIR`` when omitted.
    If only one path is given, derive the other by swapping ``.json`` / ``.csv``.
    """
    stem = _default_pipeline_output_stem(adapter_name, utc_now=utc_now)
    if out_json is None and out_csv is None:
        return (DATA_DIR / f"{stem}.json", DATA_DIR / f"{stem}.csv")
    if out_json is None:
        csv_p = Path(out_csv)
        return (csv_p.with_suffix(".json"), csv_p)
    if out_csv is None:
        json_p = Path(out_json)
        return (json_p, json_p.with_suffix(".csv"))
    return (Path(out_json), Path(out_csv))


def main() -> None:
    _load_dotenv_like_other_modules()
    cfg = load_config()

    p = argparse.ArgumentParser(description="Generic trading train/val/test pipeline (initial: rsi_mean)")
    p.add_argument("--start", type=str, default="2020-01-01", help="UTC start (YYYY-MM-DD or ISO)")
    p.add_argument("--end", type=str, default="2026-01-01", help="UTC end (YYYY-MM-DD or ISO)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--train-frac", type=float, default=0.50)
    p.add_argument("--val-frac", type=float, default=0.25)
    p.add_argument("--test-frac", type=float, default=0.25)
    p.add_argument(
        "--adapter",
        type=str,
        default="rsi_mean",
        help="Single adapter when --adapters is not set (rsi_mean | rsi_mean_walk_forward | signal_macd | …).",
    )
    p.add_argument(
        "--adapters",
        type=str,
        default="",
        help="Comma-separated list: run all on the same splits/context (e.g. rsi_mean,signal_macd,signal_bollinger). Overrides --adapter.",
    )
    p.add_argument(
        "--out-json",
        type=str,
        default=None,
        help="Write full JSON report here. Default: telegram_agent/data/pipeline_<adapter>_<UTC>.json",
    )
    p.add_argument(
        "--out-csv",
        type=str,
        default=None,
        help="Write flattened metrics CSV here. Default: same stem as JSON with .csv",
    )
    p.add_argument(
        "--sources",
        type=str,
        default="",
        help="Comma-separated price sources to include (e.g. yfinance,alpaca). Empty means all.",
    )
    args = p.parse_args()

    start = _parse_iso_or_date(args.start, is_end=False)
    end = _parse_iso_or_date(args.end, is_end=True)
    syms = _symbols_from_competitive_env(cfg)
    if not syms:
        raise SystemExit("COMPETITIVE_BACKTEST_SYMBOLS is empty; set it in .env")

    src = [x.strip() for x in (args.sources or "").split(",") if x and str(x).strip()]
    sources = src if src else None

    adapters_csv = (args.adapters or "").strip()
    if adapters_csv:
        try:
            adapter_list = resolve_adapters_csv(adapters_csv)
        except ValueError as e:
            raise SystemExit(str(e)) from e
        out_json, out_csv = _resolve_pipeline_output_paths("multi", args.out_json, args.out_csv)
        report = run_pipeline_multi(
            cfg,
            adapters=adapter_list,
            symbols=syms,
            start=start,
            end=end,
            train_frac=float(args.train_frac),
            val_frac=float(args.val_frac),
            test_frac=float(args.test_frac),
            seed=int(args.seed),
            sources=sources,
        )
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        rows = _flatten_multi_run_report(report)
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        if rows:
            keys: List[str] = []
            for r in rows:
                for k in r:
                    if k not in keys:
                        keys.append(k)
            with out_csv.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k) for k in keys})
        print(json.dumps({"ok": True, "out_json": str(out_json), "out_csv": str(out_csv), "n_adapters": len(adapter_list)}, indent=2))
        return

    try:
        adapter = resolve_adapter(str(args.adapter or "rsi_mean"))
    except ValueError as e:
        raise SystemExit(str(e)) from e
    out_json, out_csv = _resolve_pipeline_output_paths(adapter.name, args.out_json, args.out_csv)

    report = run_pipeline(
        cfg,
        adapter=adapter,
        symbols=syms,
        start=start,
        end=end,
        train_frac=float(args.train_frac),
        val_frac=float(args.val_frac),
        test_frac=float(args.test_frac),
        seed=int(args.seed),
        sources=sources,
    )

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    row = _flatten_metrics_row(report)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        w.writeheader()
        w.writerow(row)

    print(json.dumps({"ok": True, "out_json": str(out_json), "out_csv": str(out_csv)}, indent=2))


if __name__ == "__main__":
    main()

