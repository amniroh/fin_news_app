#!/usr/bin/env python3
"""
Train / backtest trend_v0_partial_position_exit on all interesting stocks.

Usage (repo root):
  python backend/trend_v0_train.py
  python backend/trend_v0_train.py --validate-only
  python backend/trend_v0_train.py --years 1.0
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO / "backend"))

from trend_v0_partial_position_exit import (  # noqa: E402
    DataValidationError,
    TrendV0Config,
    evaluate_trend_v0,
    save_artifacts,
    validate_backtest_data,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("trend_v0_train")


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest trend_v0_partial_position_exit")
    ap.add_argument("--years", type=float, default=1.0, help="History window in years (default 1.0)")
    ap.add_argument("--validate-only", action="store_true", help="Only run data validation, do not backtest")
    ap.add_argument("--split", default="0.5,0.25,0.25", help="Train/val/test fractions")
    ap.add_argument(
        "--allow-partial-universe",
        action="store_true",
        help="Use only symbols with complete indicator coverage (logs excluded symbols)",
    )
    args = ap.parse_args()

    parts = [float(x.strip()) for x in args.split.split(",")]
    if len(parts) != 3:
        ap.error("--split must have three comma-separated fractions")
    tf, vf, te = parts
    if abs(tf + vf + te - 1.0) > 1e-6:
        ap.error("split fractions must sum to 1")

    cfg = TrendV0Config(
        years=float(args.years),
        split_train_frac=tf,
        split_val_frac=vf,
        split_test_frac=te,
        allow_partial_universe=bool(args.allow_partial_universe),
    )

    logger.info("Validating data for %s-year window on all interesting stocks…", cfg.years)
    try:
        summary = validate_backtest_data(cfg)
    except DataValidationError as e:
        logger.error("Data validation FAILED — backtest aborted:\n%s", e)
        return 1

    logger.info(
        "Data OK: %d symbols%s, %d technical rows [%s → %s]",
        summary["n_symbols"],
        f" ({summary.get('n_excluded', 0)} excluded)" if summary.get("n_excluded") else "",
        summary["n_technical_rows"],
        summary["start_date"],
        summary["end_date"],
    )
    if summary.get("n_excluded"):
        logger.warning("Excluded symbols (sample): %s", ", ".join(summary.get("excluded_sample") or []))

    if args.validate_only:
        return 0

    logger.info("Running backtest + walk-forward folds…")
    result = evaluate_trend_v0(cfg)
    path = save_artifacts(result)
    tm = result.test_metrics
    logger.info("Saved metrics to %s", path)
    logger.info(
        "Test segment: total_return=%.2f%% sharpe=%.2f max_dd=%.2f%% n_days=%s",
        100 * float(tm.get("total_return", 0)),
        float(tm.get("sharpe", float("nan"))),
        100 * float(tm.get("max_drawdown", 0)),
        tm.get("n_days"),
    )
    logger.info("Walk-forward folds: %d", len(result.walkforward_folds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
