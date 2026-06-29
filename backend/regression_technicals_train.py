#!/usr/bin/env python3
"""Train regression_based_on_technicals strategy."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO / "backend"))

from regression_based_on_technicals import (  # noqa: E402
    RegressionTechnicalsConfig,
    evaluate_regression_technicals,
    save_artifacts,
)
from trend_v0_partial_position_exit import DataValidationError, TrendV0Config, validate_backtest_data  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("regression_technicals_train")


def main() -> int:
    ap = argparse.ArgumentParser(description="Train regression_based_on_technicals")
    ap.add_argument("--years", type=float, default=1.0)
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--allow-partial-universe", action="store_true")
    ap.add_argument("--split", default="0.5,0.25,0.25")
    args = ap.parse_args()

    parts = [float(x.strip()) for x in args.split.split(",")]
    if len(parts) != 3 or abs(sum(parts) - 1.0) > 1e-6:
        ap.error("--split must be three fractions summing to 1")

    cfg = RegressionTechnicalsConfig(
        years=float(args.years),
        split_train_frac=parts[0],
        split_val_frac=parts[1],
        split_test_frac=parts[2],
        allow_partial_universe=bool(args.allow_partial_universe),
    )
    vcfg = TrendV0Config(
        years=cfg.years,
        split_train_frac=cfg.split_train_frac,
        split_val_frac=cfg.split_val_frac,
        split_test_frac=cfg.split_test_frac,
        allow_partial_universe=cfg.allow_partial_universe,
    )

    logger.info("Validating data…")
    try:
        summary = validate_backtest_data(vcfg)
    except DataValidationError as e:
        logger.error("Validation failed:\n%s", e)
        return 1
    logger.info("Data OK: %d symbols", summary["n_symbols"])

    if args.validate_only:
        return 0

    logger.info("Training + optimizing (Sharpe>1, max DD<=20%%)…")
    result = evaluate_regression_technicals(cfg)
    path = save_artifacts(result)
    tm = result.test_metrics
    logger.info("Saved %s", path)
    logger.info(
        "Test: return=%.2f%% sharpe=%.2f max_dd=%.2f%% constraints_on_val=%s",
        100 * float(tm.get("total_return", 0)),
        float(tm.get("sharpe", float("nan"))),
        100 * float(tm.get("max_drawdown", 0)),
        result.optimization.get("constraints_met_on_val"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
