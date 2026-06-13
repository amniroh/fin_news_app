#!/usr/bin/env python3
"""
Daily backfill for interesting stocks: detect ~2y coverage gaps and fill them.

Runs global news ingest + universe preprocess, then per-gap jobs:
  - prices  → telegram_agent prices (yfinance → agent.sqlite)
  - fundamentals / daily metrics → value_metrics_daily_backfill
  - news    → Finnhub per symbol (if FINNHUB_API_KEY set; after global ingest)
  - analyst_ratings → yfinance snapshots (priority-0 and any symbol with a gap)

Usage (from repo root):

  .venv/bin/python backend/interesting_stocks_daily_backfill.py
  .venv/bin/python backend/interesting_stocks_daily_backfill.py --dry-run
  .venv/bin/python backend/interesting_stocks_daily_backfill.py --max-symbols 10

Schedule daily (cron example, 6:00 UTC):

  0 6 * * * cd /path/to/market_analysis && .venv/bin/python backend/interesting_stocks_daily_backfill.py >> logs/interesting_stocks_backfill.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (_REPO_ROOT, _BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("interesting_stocks_daily_backfill")


def _load_env() -> None:
    for p in (_REPO_ROOT / ".env", _BACKEND / ".env", _REPO_ROOT / "telegram_agent" / ".env"):
        if p.is_file():
            load_dotenv(p, override=False)


def main() -> int:
    _load_env()

    from interesting_stocks_service import (
        run_daily_backfill_pipeline,
        seed_interesting_stocks_from_universe,
        summarize_coverage_gaps,
    )

    ap = argparse.ArgumentParser(description="Daily gap backfill for interesting stocks")
    ap.add_argument(
        "--db",
        default=os.getenv("VALUE_METRICS_DB_PATH", str(_BACKEND / "data" / "value_metrics.sqlite")),
        help="SQLite path for value metrics + interesting stocks",
    )
    ap.add_argument("--years", type=float, default=2.0, help="Coverage lookback window in years")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print gap summary; do not run ingest or backfills",
    )
    ap.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Skip global telegram_agent ingest + universe preprocess",
    )
    ap.add_argument(
        "--symbols",
        default="",
        help="Optional comma-separated symbols (default: all active interesting stocks)",
    )
    ap.add_argument(
        "--max-symbols",
        type=int,
        default=0,
        help="Cap number of symbols processed (0 = no cap; useful for testing)",
    )
    ap.add_argument(
        "--seed-only",
        action="store_true",
        help="Only seed interesting stocks from universe JSON, then exit",
    )
    ap.add_argument(
        "--json-out",
        default="",
        help="Optional path to write full result JSON",
    )
    args = ap.parse_args()

    vm_db = Path(args.db).expanduser()
    if not vm_db.is_absolute():
        vm_db = _REPO_ROOT / vm_db

    seed_interesting_stocks_from_universe(vm_db)
    if args.seed_only:
        logger.info("Seeded interesting stocks in %s", vm_db)
        return 0

    symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    if int(args.max_symbols) > 0 and symbols:
        symbols = symbols[: int(args.max_symbols)]
    elif int(args.max_symbols) > 0 and not symbols:
        from value_metrics_store import connect, init_db, list_interesting_stocks

        con = connect(vm_db)
        init_db(con)
        try:
            all_syms = [str(r["symbol"]) for r in list_interesting_stocks(con)]
        finally:
            con.close()
        symbols = all_syms[: int(args.max_symbols)]

    if args.dry_run:
        summary = summarize_coverage_gaps(vm_db)
        print(json.dumps(
            {
                "dry_run": True,
                "db": str(vm_db),
                "n_stocks": summary["n_stocks"],
                "n_with_gaps": summary["n_with_gaps"],
                "gaps_by_type": summary["gaps_by_type"],
            },
            indent=2,
        ))
        return 0

    result = run_daily_backfill_pipeline(
        vm_db,
        only_gaps=True,
        symbols=symbols or None,
        run_ingest=not bool(args.skip_ingest),
        years=float(args.years),
    )

    if args.json_out:
        out_path = Path(args.json_out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        logger.info("Wrote %s", out_path)

    print(json.dumps(
        {
            "finished_ts_utc": result.get("finished_ts_utc"),
            "coverage_before": result.get("coverage_before"),
            "coverage_after": result.get("coverage_after"),
            "jobs": list((result.get("backfill") or {}).get("jobs", {}).keys()),
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
