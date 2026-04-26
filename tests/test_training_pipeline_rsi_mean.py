"""
Deterministic checks for `training_pipeline.run_pipeline` + `RsiMeanAdapter`.

Uses a synthetic hourly context (flat prices → RSI saturated high) and parameters that
exclude every name from the basket, so **no BASKET legs** are opened. Tuning is mocked to
return those known params; we assert tune/test wiring and the expected zero-trade outcome.

Requires: ``numpy`` (see ``telegram_agent/requirements.txt``). If missing, the test skips.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Flat prices → RSI → 100; rsi_hi 50 filters out all names → no basket trades.
KNOWN_PARAMS: Dict[str, Any] = {
    "rsi_period": 3,
    "rsi_lo": 0.0,
    "rsi_hi": 50.0,
    "mom_lookback": 2,
    "mom_max": 10.0,
    "rsi_target": 50.0,
    "mom_scale": 5.0,
    "top_k": 1,
    "min_bars": 5,
    "exposure": 1.0,
    "dd_stop": None,
    "dd_resume": None,
}


def _prefix_sums_from_diffs(closes: List[float]) -> Tuple[List[float], List[float]]:
    """Match `optimize_rsi_mean.build_context` prefix-sum layout (length len(closes))."""
    n = len(closes)
    if n < 2:
        return [0.0], [0.0]
    d = [closes[i + 1] - closes[i] for i in range(n - 1)]
    g = [max(x, 0.0) if x == x else 0.0 for x in d]
    l = [max(-x, 0.0) if x == x else 0.0 for x in d]
    gp = [0.0]
    lp = [0.0]
    sg = sl = 0.0
    for i in range(len(g)):
        sg += g[i]
        sl += l[i]
        gp.append(sg)
        lp.append(sl)
    return gp, lp


def _build_synthetic_context(n_hours: int, OptimizerContext: type) -> Any:
    """Two symbols, constant price, hourly bars — enough points for default split mins."""
    t0 = datetime(2020, 1, 1, tzinfo=timezone.utc)
    ref_times = [t0 + timedelta(hours=i) for i in range(n_hours)]
    price = 100.0
    syms = ("SYM1", "SYM2")
    cache: Dict[str, List[Tuple[datetime, float]]] = {s: [(t, price) for t in ref_times] for s in syms}

    closes_ffill: Dict[str, List[float]] = {}
    gains_ps: Dict[str, List[float]] = {}
    losses_ps: Dict[str, List[float]] = {}
    for s in syms:
        out = [price] * n_hours
        closes_ffill[s] = out
        gp, lp = _prefix_sums_from_diffs(out)
        gains_ps[s] = gp
        losses_ps[s] = lp

    return OptimizerContext(
        symbols_requested=list(syms),
        symbols_with_any_data=list(syms),
        cache=cache,
        sources_filter=None,
        ref_times=ref_times,
        closes_ffill=closes_ffill,
        gains_ps=gains_ps,
        losses_ps=losses_ps,
    )


def _fake_random_search(
    cfg: dict,
    *,
    symbols,
    start,
    end,
    trials: int,
    seed: int,
    sources=None,
) -> Dict[str, Any]:
    mk = {
        "median_return": "median_rolling_7d_return",
        "hit_rate": "rolling_7d_hit_rate",
        "floor_return": "rolling_7d_floor_return",
        "floor_pctl": "rolling_7d_floor_pctl",
    }
    return {
        "objective_name": cfg.get("optimize_objective", "median_rolling_7d_return"),
        "constraints": {},
        "rolling_window": str(cfg.get("optimize_rolling_window", "7d")),
        "rolling_metric_suffix": str(cfg.get("optimize_rolling_metric_suffix", "7d")),
        "rolling_metric_keys": mk,
        "best_feasible": {
            "ok": True,
            "objective": 0.0,
            "median_rolling_return": 0.0,
            "rolling_hit_rate": 1.0,
            "rolling_floor_pctl": 0.05,
            "rolling_floor_return": 0.0,
            "max_drawdown": 0.0,
            "calmar": None,
            "oos_sharpe": None,
            "n_hours": 1,
            "n_roll_windows": 1,
            "params": {**KNOWN_PARAMS},
        },
        "best_overall": None,
    }


class TrainingPipelineRsiMeanTest(unittest.TestCase):
    def test_tune_and_test_expected_no_basket_trades(self) -> None:
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy required (pip install -r telegram_agent/requirements.txt)")

        from telegram_agent.optimize_rsi_mean import OptimizerContext
        from telegram_agent.training_pipeline import RsiMeanAdapter, run_pipeline

        ctx = _build_synthetic_context(800, OptimizerContext)
        t0 = ctx.ref_times[0]
        t_end = ctx.ref_times[-1]

        cfg: Dict[str, Any] = {
            "agent_db_path": str(_REPO_ROOT / "telegram_agent" / "data" / "agent.sqlite"),
            "pipeline_min_coverage_frac": 0.5,
            "optimize_rolling_window": "7d",
            "optimize_rolling_metric_suffix": "7d",
            "optimize_objective": "median_rolling_7d_return",
            "pipeline_optimize_dense_hourly_simulation": True,
            "pipeline_optimize_deterministic_simulation": True,
            "optimize_dense_hourly_simulation": True,
            "optimize_deterministic_simulation": True,
            "test_risk_free_annual": 0.0,
            "test_oos_split": 0.5,
            "test_benchmark_symbol": "SPY",
            "test_metrics_enabled": ["sharpe"],
            "competitive_backtest_max_eval_points": 5000,
        }

        with patch("telegram_agent.training_pipeline.build_context", return_value=ctx):
            with patch("telegram_agent.optimize_rsi_mean.random_search", side_effect=_fake_random_search):
                report = run_pipeline(
                    cfg,
                    adapter=RsiMeanAdapter(),
                    symbols=["SYM1", "SYM2"],
                    start=t0,
                    end=t_end,
                    train_frac=0.5,
                    val_frac=0.25,
                    test_frac=0.25,
                    seed=42,
                    sources=None,
                )

        splits = report["splits"]
        tr0, tr1 = splits["train"]["start"], splits["train"]["end"]
        va0, va1 = splits["validation"]["start"], splits["validation"]["end"]
        te0, te1 = splits["test"]["start"], splits["test"]["end"]
        self.assertLess(tr0, tr1)
        self.assertLessEqual(tr1, va0)
        self.assertLess(va0, va1)
        self.assertLessEqual(va1, te0)
        self.assertLess(te0, te1)

        tuning = report["tuning"]
        self.assertEqual(tuning["adapter"], "rsi_mean")
        self.assertEqual(tuning["final_params"]["rsi_hi"], 50.0)
        self.assertEqual(tuning["final_params"]["rsi_period"], 3)

        fpm = tuning["final_params_metrics"]
        self.assertEqual(fpm["n_legs"], 0)
        self.assertNotIn("evaluation_note", fpm)

        test = report["test"]
        self.assertEqual(test["adapter"], "rsi_mean")
        self.assertEqual(test["n_legs"], 0)
        self.assertEqual(test["legs"], [])

        agg = test["aggregate_metrics"]
        self.assertEqual(agg.get("n_legs"), 0)
        self.assertIn("note", agg)

        val = report["validation_with_final_params"]
        self.assertEqual(val["adapter"], "rsi_mean")
        self.assertIn("validation_window", val)
        self.assertEqual(val["validation_window"]["start"], va0)
        self.assertEqual(val["validation_window"]["end"], va1)
        self.assertEqual(val.get("n_legs"), 0)


if __name__ == "__main__":
    unittest.main()
