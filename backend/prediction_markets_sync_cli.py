#!/usr/bin/env python3
"""CLI: sync Polymarket + Kalshi signals into SQLite (optional price-history backfill)."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from prediction_markets_categories import load_category_allowlist
from prediction_markets_store import connect, init_prediction_markets_db
from prediction_markets_sync import backfill_price_history, sync_prediction_markets

logger = logging.getLogger("prediction_markets_sync_cli")


def _db_path() -> Path:
    backend = Path(__file__).resolve().parent
    p = Path(os.getenv("VALUE_METRICS_DB_PATH", str(backend / "data" / "value_metrics.sqlite"))).expanduser()
    if not p.is_absolute():
        p = (backend.parent / p).resolve()
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync prediction market signals (Polymarket + Kalshi)")
    ap.add_argument("--pool-size", type=int, default=800, help="Candidate markets to scan per source")
    ap.add_argument("--keep", type=int, default=200, help="Top tradeable open markets to keep per source")
    ap.add_argument("--settled-keep", type=int, default=80, help="Top settled markets to keep per source")
    ap.add_argument("--no-settled", action="store_true", help="Skip settled/closed markets")
    ap.add_argument(
        "--no-trades",
        action="store_true",
        help="Skip Polymarket Data API trade enrichment (faster for large syncs).",
    )
    ap.add_argument(
        "--categories",
        type=str,
        default=None,
        help=(
            "Optional comma-separated category allowlist for fetch/wipe. "
            "Default (unset or 'all') stores every category. "
            "Env: PM_CATEGORY_ALLOWLIST."
        ),
    )
    ap.add_argument(
        "--wipe-disallowed",
        dest="wipe_disallowed",
        action="store_true",
        default=False,
        help="Delete stored signals outside --categories / PM_CATEGORY_ALLOWLIST (off by default).",
    )
    ap.add_argument(
        "--keep-disallowed",
        dest="wipe_disallowed",
        action="store_false",
        help="Keep signals outside the category allowlist (default).",
    )
    ap.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="After sync, backfill YES price history for the last N days (0=skip). Use 10 or 30.",
    )
    ap.add_argument(
        "--backfill-fidelity-minutes",
        type=int,
        default=60,
        help="Candle / history spacing in minutes for backfill (default hourly).",
    )
    ap.add_argument(
        "--skip-sync",
        action="store_true",
        help="Only run history backfill on markets already in the DB (no live sync).",
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    allowlist = load_category_allowlist(args.categories)
    wipe = bool(args.wipe_disallowed) and bool(allowlist)

    db = _db_path()
    con = connect(db)
    init_prediction_markets_db(con)
    stats: dict = {}
    try:
        if not bool(args.skip_sync):
            stats = sync_prediction_markets(
                con,
                pool_size=int(args.pool_size),
                keep_per_source=int(args.keep),
                settled_keep=int(args.settled_keep),
                include_settled=not bool(args.no_settled),
                fetch_polymarket_trades=not bool(args.no_trades),
                category_allowlist=allowlist,
                wipe_disallowed=wipe,
            )
            logger.info("Sync done: %s", stats)
        elif wipe and allowlist:
            from prediction_markets_store import delete_signals_not_in_categories, repair_polymarket_event_urls

            stats["urls_repaired"] = repair_polymarket_event_urls(con)
            stats["wiped"] = delete_signals_not_in_categories(con, allowlist)
            logger.info("Wipe/repair done: %s", stats)
        if int(args.backfill_days) > 0:
            bf = backfill_price_history(
                con,
                days=int(args.backfill_days),
                fidelity_minutes=int(args.backfill_fidelity_minutes),
            )
            stats["backfill"] = bf
            logger.info("Backfill done: %s", bf)
    finally:
        con.close()
    errors = list(stats.get("errors") or [])
    if isinstance(stats.get("backfill"), dict):
        errors.extend(stats["backfill"].get("errors") or [])
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
