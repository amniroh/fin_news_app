#!/usr/bin/env python3
"""
Run the news digest: collect (Telegram + RSS + X), dedupe, optional micro-summarize,
one consolidated digest LLM call (Market + Trend sections), publish two messages.
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from telegram_agent.config import load_config, SESSION_DIR
from telegram_agent.digest_pipeline import run_digest_pipeline
from telegram_agent.publisher import publish

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("telegram_agent")


async def run_once(
    config: dict,
    dry_run: bool = False,
    source_mode: Optional[str] = None,
) -> None:
    config = {**config}

    result = await run_digest_pipeline(
        config,
        dry_run=dry_run,
        source_mode=source_mode,
    )

    if result.kind == "no_items":
        return

    if result.kind == "dry_run":
        print(result.dry_run_block or "")
        return

    trend_text = result.trend_text
    market_text = result.market_text

    target = config.get("target_channel", "").strip()
    if not target:
        logger.info(
            "No TARGET_CHANNEL set.\n--- Trend ---\n%s\n--- Market ---\n%s",
            trend_text[:1200],
            market_text[:1200],
        )
        return

    api_id = config.get("telegram_api_id")
    api_hash = config.get("telegram_api_hash")
    session_name = config.get("telegram_session_name", "news_agent")
    session_path = str(SESSION_DIR / session_name)

    if api_id and api_hash:
        from telethon import TelegramClient

        client = TelegramClient(session_path, int(api_id), api_hash)
        await client.start()
        try:
            ok1 = await publish(client, target, trend_text)
            await asyncio.sleep(0.4)
            ok2 = await publish(client, target, market_text)
            if ok1 and ok2:
                logger.info("Two digests published to %s", target)
            else:
                logger.error("Publish incomplete (trend=%s market=%s).", ok1, ok2)
        finally:
            await client.disconnect()
    else:
        logger.info(
            "Cannot publish without Telegram client.\n--- Trend ---\n%s\n--- Market ---\n%s",
            trend_text[:1500],
            market_text[:1500],
        )


def run_schedule(config: dict) -> None:
    import time

    hours = max(0.5, config.get("hours_back", 6))
    interval_seconds = int(hours * 3600)
    logger.info("Scheduler: digest every %s hours (Ctrl+C to stop)", hours)
    try:
        while True:
            asyncio.run(run_once(config))
            time.sleep(interval_seconds)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopping.")


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")

    parser = argparse.ArgumentParser(description="Telegram News Digest Agent")
    parser.add_argument("--once", action="store_true", help="Run once and exit (default)")
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Run on a schedule (every HOURS_BACK)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="No LLM or publish: print prompts, estimate costs only (no OpenRouter/Gemini charges)",
    )
    parser.add_argument(
        "--sources",
        choices=["all", "rss", "telegram"],
        default=None,
        help="Override SOURCE_MODE: all (default), rss only, or telegram only",
    )
    args = parser.parse_args()

    config = load_config()

    if args.schedule and not args.dry_run:
        run_schedule(config)
    else:
        asyncio.run(
            run_once(
                config,
                dry_run=args.dry_run,
                source_mode=args.sources,
            )
        )


if __name__ == "__main__":
    main()
