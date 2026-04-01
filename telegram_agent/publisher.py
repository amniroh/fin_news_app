"""Publish digest to Telegram (channel or chat)."""
import logging
from typing import Optional

from telethon import TelegramClient

logger = logging.getLogger(__name__)

# Telegram message length limit
MAX_MESSAGE_LENGTH = 4096

# Private invite hashes contain hyphens (e.g. BJ-W7gOP-xkNTkz from t.me/+BJ-W7gOP-xkNTkz)
# Usernames are typically alphanumeric without hyphens


def _normalize_target(target: str) -> str:
    """Convert t.me/SOME_ID or +HASH formats to a form Telethon can resolve."""
    t = target.strip()
    if not t:
        return t
    # Already a full URL
    if t.startswith("https://t.me/") or t.startswith("http://t.me/"):
        return t
    # t.me/username or t.me/+invite_hash (no protocol)
    if t.startswith("t.me/"):
        return "https://" + t
    # Invite hash: starts with + or contains hyphen (e.g. +BJ-W7gOP-xkNTkz or BJ-W7gOP-xkNTkz)
    if t.startswith("+") or "-" in t:
        hash_part = t.lstrip("+")
        if hash_part and hash_part[0].isalnum():
            return f"https://t.me/+{hash_part}"
    # Numeric channel/chat id
    if t.lstrip("-").isdigit():
        return t
    # Plain username (e.g. VahidOnline): ensure @ for consistency
    if t and not t.startswith("@"):
        return "@" + t
    return t


async def publish(client: TelegramClient, target: str, text: str) -> bool:
    """Send digest to target (channel username, @channel, invite link, or chat id)."""
    if not target or not text.strip():
        logger.warning("No target or empty digest, skip publish.")
        return False
    target = _normalize_target(target)
    # Split if over limit
    if len(text) > MAX_MESSAGE_LENGTH:
        chunks = []
        rest = text
        while rest:
            chunk = rest[:MAX_MESSAGE_LENGTH]
            last_break = chunk.rfind("\n")
            if last_break > MAX_MESSAGE_LENGTH // 2:
                chunk = chunk[: last_break + 1]
                rest = rest[last_break + 1 :].lstrip()
            else:
                rest = rest[MAX_MESSAGE_LENGTH :].lstrip()
            chunks.append(chunk)
        for i, chunk in enumerate(chunks):
            try:
                await client.send_message(target, chunk)
            except Exception as e:
                logger.error("Failed to send chunk %s: %s", i + 1, e)
                return False
        return True
    try:
        await client.send_message(target, text)
        return True
    except Exception as e:
        err_str = str(e).lower()
        if "expired" in err_str or "checkchatinvite" in err_str:
            logger.error(
                "Failed to publish: the invite link has expired. "
                "For posting, use the channel's @username (public) or numeric ID (private) instead. "
                "Run: python -m telegram_agent.list_chats to get your channel IDs."
            )
        else:
            logger.error("Failed to publish to %s: %s", target, e)
        return False
