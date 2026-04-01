"""Ingest news into agent DB: incremental vs backfill."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Literal, Optional

from pathlib import Path

from telegram_agent.config import SESSION_DIR
from telegram_agent.models import NewsItem
from telegram_agent.agent_db import connect, init_db, kv_get, kv_set, upsert_news_items
from telegram_agent.digest_pipeline import normalize_source_mode
from telegram_agent.collectors.telegram_collector import collect_telegram_backfill
from telegram_agent.collectors.rss_collector import collect_rss
from telegram_agent.collectors.twitter_collector import collect_twitter

logger = logging.getLogger(__name__)

Mode = Literal["incremental", "backfill"]


def _db_path(cfg: dict) -> Path:
    return Path(cfg.get("agent_db_path") or (SESSION_DIR.parent / "data" / "agent.sqlite"))


async def run_ingest(
    cfg: dict,
    *,
    mode: Mode,
    source_mode: Optional[str] = None,
    backfill_days: Optional[int] = None,
) -> int:
    """
    Collect from configured sources and upsert into agent.sqlite.
    Returns number of news rows upserted this run.
    """
    cfg = {**cfg}
    con = connect(_db_path(cfg))
    init_db(con)

    days = int(backfill_days if backfill_days is not None else cfg.get("agent_backfill_days", 365))
    now = datetime.now(timezone.utc)
    if mode == "backfill":
        since = now - timedelta(days=min(days, 365 * 2))
        kv_set(con, "ingest:last_backfill_ts", now.isoformat())
    else:
        raw = kv_get(con, "ingest:last_run_ts")
        if raw:
            try:
                since = datetime.fromisoformat(raw)
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
                since = since.astimezone(timezone.utc) - timedelta(hours=1)
            except Exception:
                since = now - timedelta(hours=float(cfg.get("hours_back", 6)))
        else:
            since = now - timedelta(hours=float(cfg.get("hours_back", 6)))

    mode_norm = normalize_source_mode(source_mode if source_mode is not None else cfg.get("source_mode"))
    include_telegram = mode_norm in ("all", "telegram")
    include_rss = mode_norm in ("all", "rss")
    include_twitter = mode_norm == "all"

    all_items: List[NewsItem] = []
    api_id = cfg.get("telegram_api_id")
    api_hash = cfg.get("telegram_api_hash")
    session_name = cfg.get("telegram_session_name", "news_agent")
    session_path = str(SESSION_DIR / session_name)
    max_tg = int(cfg.get("agent_max_telegram_backfill", 3000)) if mode == "backfill" else int(cfg.get("max_items_per_run", 80))

    if include_telegram and api_id and api_hash:
        from telethon import TelegramClient

        client = TelegramClient(session_path, int(api_id), api_hash)
        await client.start()
        try:
            tg_items = await collect_telegram_backfill(
                client,
                cfg.get("telegram_channels", []),
                since=since,
                until=now,
                max_messages_per_channel=max_tg,
            )
            all_items.extend(tg_items)
        finally:
            await client.disconnect()
    elif include_telegram:
        logger.warning("Telegram ingest skipped: TELEGRAM_API_ID / TELEGRAM_API_HASH not set.")

    if include_rss:
        # RSS: fetch many entries per feed; collector filters by item date vs since
        per_feed = 500 if mode == "backfill" else 25
        rss_items = collect_rss(
            cfg.get("rss_feeds", []),
            since=since,
            max_entries_per_feed=per_feed,
        )
        all_items.extend(rss_items)

    if include_twitter:
        bearer = cfg.get("twitter_bearer_token") or ""
        if bearer:
            since_arg = since

            def _tw():
                return collect_twitter(
                    cfg.get("twitter_usernames", []),
                    cfg.get("twitter_list_ids", []),
                    since_arg,
                    bearer,
                    int(cfg.get("twitter_max_tweets_per_source", 10)),
                )

            tw_items = await asyncio.get_event_loop().run_in_executor(None, _tw)
            all_items.extend(tw_items)
        else:
            logger.info("Twitter skipped: no TWITTER_BEARER_TOKEN")

    n = upsert_news_items(con, all_items)
    kv_set(con, "ingest:last_run_ts", now.isoformat())
    logger.info("Ingest upserted %s news rows (mode=%s, since=%s)", n, mode, since.isoformat())
    con.close()
    return n
