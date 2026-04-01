#!/usr/bin/env python3
"""List chats/channels you're in with their IDs. Use the numeric ID as TARGET_CHANNEL for private channels."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from telegram_agent.config import load_config, SESSION_DIR


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")

    config = load_config()
    api_id = config.get("telegram_api_id")
    api_hash = config.get("telegram_api_hash")
    session_name = config.get("telegram_session_name", "news_agent")

    if not api_id or not api_hash:
        print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env")
        sys.exit(1)

    from telethon import TelegramClient

    session_path = str(SESSION_DIR / session_name)
    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.start()

    print("Your channels and chats (use numeric ID for TARGET_CHANNEL):\n")
    async for d in client.iter_dialogs():
        e = d.entity
        title = getattr(e, "title", None) or (getattr(e, "first_name", "") or "") + (
            " " + (getattr(e, "last_name", "") or "")
        ).strip()
        username = getattr(e, "username", None)
        tid = e.id
        # Channel IDs need the -100 prefix for API use
        if hasattr(e, "broadcast") and e.broadcast:
            chat_id = f"-100{tid}"
        elif hasattr(e, "megagroup") and e.megagroup:
            chat_id = f"-100{tid}"
        else:
            chat_id = str(-tid) if tid > 0 else str(tid)
        un = f"  @{username}" if username else ""
        print(f"  {chat_id}  {title}{un}")

    await client.disconnect()
    print("\nSet TARGET_CHANNEL to the numeric ID (e.g. -1001234567890) for private channels.")


if __name__ == "__main__":
    asyncio.run(main())
