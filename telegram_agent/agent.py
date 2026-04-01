#!/usr/bin/env python3
"""
Agent CLI: ingest → extract → prices → memory → research → backtest.

Examples:
  python -m telegram_agent.agent ingest --mode incremental
  python -m telegram_agent.agent ingest --mode backfill --days 365
  python -m telegram_agent.agent extract
  python -m telegram_agent.agent prices --mode backfill
  python -m telegram_agent.agent memory
  python -m telegram_agent.agent research
  python -m telegram_agent.agent backtest
  python -m telegram_agent.agent run-all --mode incremental
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("telegram_agent.agent")


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")

    from telegram_agent.config import load_config
    from telegram_agent.ingest import run_ingest
    from telegram_agent.extract_pipeline import run_extract
    from telegram_agent.prices import backfill_all_mentioned, incremental_prices
    from telegram_agent.agent_memory import run_memory_update
    from telegram_agent.agent_research import run_research
    from telegram_agent.backtest import print_backtest_report

    p = argparse.ArgumentParser(description="Market analysis agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="Fetch news into agent DB")
    pi.add_argument("--mode", choices=["incremental", "backfill"], default="incremental")
    pi.add_argument("--days", type=int, default=None, help="Backfill window in days (default from config)")
    pi.add_argument("--sources", choices=["all", "rss", "telegram"], default=None)

    sub.add_parser("extract", help="Extract tickers from news into news_mentions")

    pp = sub.add_parser("prices", help="Fetch yfinance prices for mentioned symbols")
    pp.add_argument("--mode", choices=["incremental", "backfill"], default="incremental")
    pp.add_argument("--days", type=int, default=400, help="History days for backfill")

    sub.add_parser("memory", help="Update rolling macro/micro memory (LLM)")

    sub.add_parser("research", help="Run opportunity research (LLM) and store recommendations")

    sub.add_parser("backtest", help="Print backtest JSON for stored recommendations")

    pa = sub.add_parser("run-all", help="ingest → extract → prices → memory → research")
    pa.add_argument("--mode", choices=["incremental", "backfill"], default="incremental")
    pa.add_argument("--days", type=int, default=None)
    pa.add_argument("--sources", choices=["all", "rss", "telegram"], default=None)
    pa.add_argument("--skip-memory", action="store_true")
    pa.add_argument("--skip-research", action="store_true")

    args = p.parse_args()
    cfg = load_config()

    if args.cmd == "ingest":
        n = asyncio.run(
            run_ingest(
                cfg,
                mode=args.mode,
                source_mode=args.sources,
                backfill_days=args.days,
            )
        )
        logger.info("Ingest done: %s rows", n)
        return

    if args.cmd == "extract":
        n = run_extract(cfg)
        logger.info("Extract done: %s mention rows", n)
        return

    if args.cmd == "prices":
        if args.mode == "backfill":
            backfill_all_mentioned(cfg, days=args.days)
        else:
            incremental_prices(cfg)
        return

    if args.cmd == "memory":
        run_memory_update(cfg)
        return

    if args.cmd == "research":
        run_research(cfg)
        return

    if args.cmd == "backtest":
        print_backtest_report(cfg)
        return

    if args.cmd == "run-all":
        asyncio.run(
            run_ingest(
                cfg,
                mode=args.mode,
                source_mode=args.sources,
                backfill_days=args.days,
            )
        )
        run_extract(cfg)
        if args.mode == "backfill":
            backfill_all_mentioned(cfg, days=args.days or int(cfg.get("agent_backfill_days", 365)) + 30)
        else:
            incremental_prices(cfg)
        if not args.skip_memory:
            run_memory_update(cfg)
        if not args.skip_research:
            run_research(cfg)
        logger.info("run-all finished")


if __name__ == "__main__":
    main()
