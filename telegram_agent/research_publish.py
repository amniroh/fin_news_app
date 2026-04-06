"""Format and publish research outputs to TARGET_CHANNEL (Telethon)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from telegram_agent.memory_structured import format_memory_for_prompt

logger = logging.getLogger(__name__)


def format_research_telegram_message(
    *,
    thinking: str,
    merged_memory: Dict[str, Any],
    new_suggestions: List[Dict[str, Any]],
    run_ts: Optional[datetime] = None,
) -> str:
    """Single message or first chunk; caller may split further if needed."""
    ts = run_ts or datetime.now(timezone.utc)
    parts: List[str] = []
    parts.append(f"📊 Research run — {ts.strftime('%Y-%m-%d %H:%M UTC')}")
    parts.append("")
    parts.append("🧠 Thinking (news + memory)")
    parts.append((thinking or "").strip() or "(none)")
    parts.append("")
    parts.append("🆕 New concrete suggestions (this run)")
    if not new_suggestions:
        parts.append("(none)")
    else:
        for i, s in enumerate(new_suggestions[:30], 1):
            sym = s.get("symbol", "?")
            w = s.get("what_to_acquire", "")
            parts.append(f"{i}. {sym}: {w}")
            for k in (
                "suggestion_ts_utc",
                "entry_window_start_utc",
                "entry_window_end_utc",
                "execute_review_utc",
            ):
                if s.get(k):
                    parts.append(f"   {k}: {s[k]}")
    parts.append("")
    parts.append("📝 Memory state (structured, after merge)")
    mem_txt = format_memory_for_prompt(merged_memory, max_chars=2800)
    parts.append(mem_txt)
    return "\n".join(parts)


async def _publish_text(cfg: dict, text: str) -> bool:
    from telegram_agent.config import SESSION_DIR
    from telegram_agent.publisher import publish

    target = (cfg.get("target_channel") or "").strip()
    if not target:
        logger.warning("TARGET_CHANNEL not set; skipping publish.")
        return False
    api_id = cfg.get("telegram_api_id")
    api_hash = cfg.get("telegram_api_hash")
    session_name = cfg.get("telegram_session_name", "news_agent")
    session_path = str(SESSION_DIR / session_name)
    if not api_id or not api_hash:
        logger.warning("Telegram API credentials missing; cannot publish research.")
        return False
    from telethon import TelegramClient

    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.start()
    try:
        return await publish(client, target, text)
    finally:
        await client.disconnect()


def publish_research_to_target(cfg: dict, text: str) -> bool:
    """Sync wrapper: publish to TARGET_CHANNEL when enabled."""
    if not cfg.get("agent_research_publish", True):
        logger.info("AGENT_RESEARCH_PUBLISH is off; skip.")
        return False
    try:
        return asyncio.run(_publish_text(cfg, text))
    except RuntimeError as e:
        if "asyncio.run() cannot be called from a running event loop" in str(e):
            logger.error("Cannot publish research from inside running event loop: %s", e)
            return False
        raise
