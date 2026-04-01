"""Collect messages from Telegram channels using Telethon (user client)."""
import logging
from datetime import datetime, timezone
from typing import List

from telethon import TelegramClient
from telethon.tl.types import Message

from ..models import NewsItem

logger = logging.getLogger(__name__)


def _msg_ts(msg: Message, fallback: datetime) -> datetime:
    if not msg.date:
        return fallback
    d = msg.date
    if d.tzinfo is None:
        return d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


async def collect_telegram(
    client: TelegramClient,
    channel_ids: List[str],
    since: datetime,
    max_messages_per_channel: int = 50,
) -> List[NewsItem]:
    """
    Fetch messages from Telegram channels with timestamp >= since (newest-first scan, stop when older).
    """
    return await collect_telegram_backfill(
        client,
        channel_ids,
        since=since,
        until=None,
        max_messages_per_channel=max_messages_per_channel,
    )


async def collect_telegram_backfill(
    client: TelegramClient,
    channel_ids: List[str],
    *,
    since: datetime,
    until: datetime | None = None,
    max_messages_per_channel: int = 3000,
) -> List[NewsItem]:
    """
    Backfill / incremental: walk messages from newest to oldest; keep those with since <= ts <= until (if set).
    """
    if not channel_ids:
        return []
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    else:
        since = since.astimezone(timezone.utc)
    if until is not None:
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        else:
            until = until.astimezone(timezone.utc)

    items: List[NewsItem] = []
    for ch in channel_ids:
        ch = ch.strip()
        if not ch:
            continue
        prev = len(items)
        logger.info("Collecting from channel: %s (since=%s, max=%s)", ch, since, max_messages_per_channel)
        try:
            n = 0
            async for msg in client.iter_messages(ch, reverse=False):
                if n >= max_messages_per_channel:
                    break
                if not isinstance(msg, Message) or not getattr(msg, "message", None):
                    continue
                text = (msg.message or "").strip()
                if not text or len(text) < 10:
                    continue
                ts = _msg_ts(msg, since)
                if ts < since:
                    break
                if until is not None and ts > until:
                    continue
                title = text.split("\n")[0][:200] if text else ""
                item_id = f"tg:{ch}:{msg.id}"
                items.append(
                    NewsItem(
                        id=item_id,
                        source_type="telegram",
                        source_name=ch,
                        title=title,
                        content=text[:4000],
                        url=None,
                        timestamp=ts,
                    )
                )
                n += 1
            logger.info("Items from channel %s: %s", ch, len(items) - prev)
        except Exception as e:
            logger.warning("Failed to fetch channel %s: %s", ch, e)
    return items
