#!/usr/bin/env python3
"""Fetch corporate split history from Yahoo Finance and upsert into ``stock_splits``."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from telegram_agent.agent_db import connect, init_db
from telegram_agent.config import load_config
from telegram_agent.derive_hourly_daily_from_5m import _load_symbols_by_max_priority
from telegram_agent.stock_splits import default_symbol_list, fetch_and_store_splits_for_symbols

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
    p = argparse.ArgumentParser(
        description=(
            "Populate stock_splits from yfinance. "
            "Symbol source: --symbols, or --from-db (all tickers with price rows), "
            "or universe + --max-priority (default when neither --symbols nor --from-db)."
        )
    )
    p.add_argument("--symbols", type=str, default="", help="Comma-separated canonical tickers")
    p.add_argument(
        "--from-db",
        action="store_true",
        help="Use all symbols that appear in prices / prices_hourly / prices_minute",
    )
    p.add_argument(
        "--max-priority",
        type=int,
        default=1,
        help=(
            "With universe (default when neither --symbols nor --from-db): "
            "include symbols with priority <= N (default 1). Ignored if --symbols or --from-db."
        ),
    )
    p.add_argument(
        "--universe-path",
        type=Path,
        default=None,
        help="Optional JSON universe file (overrides SYMBOL_UNIVERSE_PATH for this run).",
    )
    p.add_argument(
        "--include-crypto",
        action="store_true",
        help="Also query yfinance for symbols marked kind=crypto (usually empty)",
    )
    p.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.35,
        help="Pause between Yahoo requests",
    )
    args = p.parse_args(list(argv) if argv is not None else None)
    _load_dotenv_files()
    cfg = load_config()
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    try:
        if args.symbols.strip():
            syms = sorted({x.strip().upper() for x in args.symbols.split(",") if x.strip()})
        elif args.from_db:
            syms = default_symbol_list(con)
        else:
            syms = _load_symbols_by_max_priority(
                cfg,
                max_priority=int(args.max_priority),
                universe_path=args.universe_path,
            )
            if syms:
                logger.info(
                    "Universe max_priority=%s -> %s tickers",
                    int(args.max_priority),
                    len(syms),
                )
        if not syms:
            print(
                "No symbols: pass --symbols, --from-db, or set SYMBOL_UNIVERSE_PATH / --universe-path "
                "(universe works even if SYMBOL_UNIVERSE_ENABLED=false).",
                file=sys.stderr,
            )
            return 2
        stats = fetch_and_store_splits_for_symbols(
            con,
            syms,
            sleep_seconds=float(args.sleep_seconds),
            skip_crypto=not bool(args.include_crypto),
        )
        n_with = sum(1 for v in stats.values() if v > 0)
        n_splits = sum(stats.values())
        logger.info("Done: symbols=%s with_splits=%s split_rows_written=%s", len(syms), n_with, n_splits)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
