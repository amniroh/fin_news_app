#!/usr/bin/env python3
"""CLI: sync Polymarket + Kalshi signals into SQLite."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from prediction_markets_store import connect, init_prediction_markets_db
from prediction_markets_sync import sync_prediction_markets

logger = logging.getLogger("prediction_markets_sync_cli")


def _db_path() -> Path:
    backend = Path(__file__).resolve().parent
    p = Path(os.getenv("VALUE_METRICS_DB_PATH", str(backend / "data" / "value_metrics.sqlite"))).expanduser()
    if not p.is_absolute():
        p = (backend.parent / p).resolve()
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync prediction market signals (Polymarket + Kalshi)")
    ap.add_argument("--polymarket-limit", type=int, default=400)
    ap.add_argument("--kalshi-limit", type=int, default=400)
    ap.add_argument("--no-settled", action="store_true", help="Skip settled/closed markets")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    db = _db_path()
    con = connect(db)
    init_prediction_markets_db(con)
    try:
        stats = sync_prediction_markets(
            con,
            polymarket_limit=int(args.polymarket_limit),
            kalshi_limit=int(args.kalshi_limit),
            include_settled=not bool(args.no_settled),
        )
    finally:
        con.close()
    logger.info("Done: %s", stats)
    return 0 if not stats.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
