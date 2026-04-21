#!/usr/bin/env python3
"""
Re-scale OHLCV in ``prices``, ``prices_hourly``, and ``prices_minute`` using ``stock_splits``.

**Idempotency:** do not run twice on the same DB without restoring raw prices; factors
compound. Use ``--dry-run`` first.

If ``stock_splits`` has no rows for a symbol, this tool **loads split history from Yahoo
Finance** by default (same as ``fetch_stock_splits``) and stores it in ``stock_splits``;
only OHLCV updates are skipped in ``--dry-run``. Use ``--no-fetch-missing-splits`` to
require pre-populated ``stock_splits``.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from telegram_agent.agent_db import connect, get_stock_splits_chronological, init_db
from telegram_agent.config import load_config
from telegram_agent.stock_splits import (
    default_symbol_list,
    fetch_and_store_splits_for_symbols,
    normalize_symbol_for_stored_splits,
    splits_touching_stored_bars,
    symbol_price_ts_bounds,
    unique_bar_update_counts,
)

logger = logging.getLogger(__name__)


def _load_dotenv_files() -> None:
    root = Path(__file__).resolve().parent.parent
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="Normalize stored OHLCV using stock_splits.")
    p.add_argument("--symbols", type=str, default="", help="Comma-separated tickers")
    p.add_argument(
        "--from-db",
        action="store_true",
        help="Process every symbol that has price rows (can be slow)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="No DB writes: print stored ts range, splits that touch stored bars, and unique bar counts",
    )
    p.add_argument(
        "--no-fetch-missing-splits",
        action="store_true",
        help="If set, do not query Yahoo when stock_splits has no rows for a symbol (use fetch_stock_splits first)",
    )
    args = p.parse_args(list(argv) if argv is not None else None)
    _load_dotenv_files()
    cfg = load_config()
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    try:
        if args.symbols.strip():
            syms: List[str] = sorted({x.strip().upper() for x in args.symbols.split(",") if x.strip()})
        elif args.from_db:
            syms = default_symbol_list(con)
        else:
            print("Pass --symbols TICKERS or --from-db", file=sys.stderr)
            return 2
        if not syms:
            print("No symbols", file=sys.stderr)
            return 2
        fetch_missing = not bool(args.no_fetch_missing_splits)
        for sym in syms:
            splits = get_stock_splits_chronological(con, sym)
            if not splits and fetch_missing:
                logger.info("%s: stock_splits empty — fetching split history from Yahoo Finance", sym)
                fetch_and_store_splits_for_symbols(con, [sym], sleep_seconds=0.2)
                splits = get_stock_splits_chronological(con, sym)
            if not splits:
                logger.info(
                    "%s: no rows in stock_splits%s, skip",
                    sym,
                    " (use fetch_stock_splits or drop --no-fetch-missing-splits)" if not fetch_missing else "",
                )
                continue
            if args.dry_run:
                bounds = symbol_price_ts_bounds(con, sym)
                if bounds:
                    logger.info("%s: stored price ts_utc range (union of tables): %s .. %s", sym, bounds[0], bounds[1])
                else:
                    logger.info("%s: no price rows in DB", sym)
                touching = splits_touching_stored_bars(con, sym, splits)
                if not touching:
                    logger.info(
                        "%s: no splits overlap stored bars (no rows match split predicates)",
                        sym,
                    )
                else:
                    logger.info(
                        "%s: splits that overlap stored data (%s event(s); NY ex-date, UTC effective, mult):",
                        sym,
                        len(touching),
                    )
                    for eff, ex_ny, mult in touching:
                        logger.info("  ex_date_ny=%s  effective_ts_utc=%s  share_multiplier=%s", ex_ny, eff, mult)
                uniq, uniq_total = unique_bar_update_counts(con, sym, splits)
                logger.info(
                    "%s: unique bars that would be updated if run without --dry-run: total=%s by_table=%s",
                    sym,
                    uniq_total,
                    uniq,
                )
                continue
            st = normalize_symbol_for_stored_splits(con, sym, dry_run=False)
            logger.info(
                "%s: splits_applied=%s rowcounts=%s total_row_updates=%s",
                sym,
                st.splits_applied,
                st.rowcounts,
                st.total_rows(),
            )
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
