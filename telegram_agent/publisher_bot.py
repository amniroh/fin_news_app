"""Send digest chunks via Telegram Bot API (python-telegram-bot Bot)."""
import logging
from typing import Union

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096


async def send_long_message(
    bot,
    chat_id: Union[int, str],
    text: str,
    *,
    delay_s: float = 0.35,
) -> bool:
    """Split at 4096 and send sequentially. `bot` is telegram.Bot."""
    if not text or not str(text).strip():
        logger.warning("send_long_message: empty text")
        return False
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        logger.error("Invalid chat_id %r", chat_id)
        return False
    chunks: list[str] = []
    rest = text.strip()
    while rest:
        chunk = rest[:MAX_MESSAGE_LENGTH]
        last_break = chunk.rfind("\n")
        if last_break > MAX_MESSAGE_LENGTH // 2:
            chunk = chunk[: last_break + 1]
            rest = rest[last_break + 1 :].lstrip()
        else:
            rest = rest[MAX_MESSAGE_LENGTH:].lstrip()
        chunks.append(chunk)
    import asyncio

    for i, chunk in enumerate(chunks):
        try:
            await bot.send_message(chat_id=chat_id, text=chunk)
            if i + 1 < len(chunks):
                await asyncio.sleep(delay_s)
        except Exception as e:
            logger.error("Bot send_message failed (chunk %s): %s", i + 1, e)
            return False
    return True


async def publish_digest_bot(bot, chat_id: Union[int, str], trend_text: str, market_text: str) -> bool:
    """Post Trend first, then Market (same order as Telethon path)."""
    ok1 = await send_long_message(bot, chat_id, trend_text)
    import asyncio

    await asyncio.sleep(0.4)
    ok2 = await send_long_message(bot, chat_id, market_text)
    return ok1 and ok2
