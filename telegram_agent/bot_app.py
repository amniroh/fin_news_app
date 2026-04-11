#!/usr/bin/env python3
"""
Telegram Bot: /digest to run unseen digest, /schedule <hours> for periodic runs.
Requires TELEGRAM_BOT_TOKEN. Add bot to channel as admin (can post messages).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env")

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from telegram_agent.config import load_config
from telegram_agent.digest_pipeline import run_digest_pipeline
from telegram_agent.publisher_bot import publish_digest_bot
from telegram_agent.schedule_store import load_schedules, save_schedules

logger = logging.getLogger("telegram_agent.bot")

JOB_PREFIX = "digest_"


def job_name(chat_id: int) -> str:
    return f"{JOB_PREFIX}{chat_id}"


def chat_allowed(cfg: dict, chat_id: int) -> bool:
    allowed = cfg.get("bot_allowed_chat_ids") or []
    if not allowed:
        return True
    return str(chat_id) in allowed


def parse_channel_command(text: str, bot_username: str) -> tuple[str | None, list[str]]:
    """
    First line of a channel post, e.g. '/digest@MyBot args...'
    If the command includes @name, it must match this bot (Telegram sends /cmd@bot in channels).
    """
    if not text or not text.strip():
        return None, []
    line = text.strip().split("\n", 1)[0].strip()
    parts = line.split()
    if not parts:
        return None, []
    first = parts[0]
    if not first.startswith("/"):
        return None, []
    rest = first[1:]
    if "@" in rest:
        cmd_part, name_part = rest.split("@", 1)
        if bot_username and name_part.lower() != bot_username.lower():
            return None, []
        cmd = cmd_part.lower()
    else:
        cmd = rest.lower()
    return cmd, parts[1:]


async def execute_digest(application: Application, chat_id: int, cfg: dict) -> None:
    result = await run_digest_pipeline(
        cfg,
        dry_run=False,
        source_mode=cfg.get("source_mode"),
    )
    bot = application.bot
    if result.kind == "no_items":
        await bot.send_message(
            chat_id=chat_id,
            text=f"No new unseen items in the last {result.hours_back:g} hours.",
        )
        return
    if result.kind != "ok":
        return
    ok = await publish_digest_bot(bot, chat_id, result.trend_text, result.market_text)
    if not ok:
        await bot.send_message(
            chat_id=chat_id,
            text="Digest generated but sending one or more message parts failed (check server logs).",
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    if not chat_allowed(cfg, update.effective_chat.id):
        await update.effective_message.reply_text("This bot is not enabled for this chat.")
        return
    await update.effective_message.reply_text(
        "Market digest bot\n\n"
        "/digest — Run now: summarize unseen news and post Trend + Market in this chat.\n"
        "/schedule <hours> — Auto-repeat every N hours (e.g. /schedule 6).\n"
        "/schedule off — Stop auto digests for this chat.\n"
        "/status — Show the saved schedule.\n"
        "/competitive — Run competitive systematic bots (P0/P1), test, store, publish summary.\n"
        "/competitive_backtest — Walk-forward backtest on all price intervals in DB; publish summary.\n\n"
        "In channels: post commands as a new channel message, usually "
        "/digest@BotName (replace with this bot's username from @BotFather).\n"
        "The bot must be an admin with Post messages."
    )


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    cid = update.effective_chat.id
    if not chat_allowed(cfg, cid):
        await update.effective_message.reply_text("This bot is not enabled for this chat.")
        return
    status = await update.effective_message.reply_text("Running digest…")
    try:
        await execute_digest(context.application, cid, cfg)
    except Exception as e:
        logger.exception("cmd_digest failed")
        try:
            await context.bot.send_message(chat_id=cid, text=f"Digest error: {e}")
        except Exception:
            pass
    finally:
        try:
            await status.delete()
        except Exception:
            pass


def remove_digest_jobs(job_queue, chat_id: int) -> None:
    name = job_name(chat_id)
    for j in list(job_queue.jobs()):
        if j.name == name:
            j.schedule_removal()


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_schedule_with_args(update, context, list(context.args or []))


async def cmd_schedule_with_args(
    update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]
) -> None:
    cfg = load_config()
    cid = update.effective_chat.id
    if not chat_allowed(cfg, cid):
        await update.effective_message.reply_text("This bot is not enabled for this chat.")
        return
    jq = context.job_queue
    if jq is None:
        await update.effective_message.reply_text(
            "Job queue unavailable. Install: pip install 'python-telegram-bot[job-queue]'"
        )
        return

    path = Path(cfg["bot_schedule_file"])
    if not args:
        await update.effective_message.reply_text(
            "Usage: /schedule <hours> — e.g. /schedule 6\nOr: /schedule off"
        )
        return
    if args[0].lower() in ("off", "stop", "none"):
        remove_digest_jobs(jq, cid)
        schedules = load_schedules(path)
        schedules.pop(str(cid), None)
        save_schedules(path, schedules)
        await update.effective_message.reply_text("Scheduled digests stopped for this chat.")
        return
    try:
        hours = float(args[0].replace(",", "."))
    except ValueError:
        await update.effective_message.reply_text("Invalid number. Example: /schedule 6")
        return

    min_h = float(cfg.get("bot_min_schedule_hours", 0.25))
    if hours < min_h:
        await update.effective_message.reply_text(f"Minimum interval is {min_h} hours.")
        return
    if hours > 336:
        await update.effective_message.reply_text("Maximum interval is 336 hours (14 days).")
        return

    remove_digest_jobs(jq, cid)

    def build_callback(target_chat: int):
        async def _cb(ctx: ContextTypes.DEFAULT_TYPE) -> None:
            c = load_config()
            await execute_digest(ctx.application, target_chat, c)

        return _cb

    jq.run_repeating(
        build_callback(cid),
        interval=timedelta(hours=hours),
        first=timedelta(hours=hours),
        chat_id=cid,
        name=job_name(cid),
    )
    schedules = load_schedules(path)
    schedules[str(cid)] = hours
    save_schedules(path, schedules)
    await update.effective_message.reply_text(
        f"Scheduled: digest every {hours:g} hours (first automatic run in {hours:g} h)."
    )


async def cmd_competitive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run competitive bot cycle (sync pipeline in executor)."""
    cfg = load_config()
    cid = update.effective_chat.id
    if not chat_allowed(cfg, cid):
        await update.effective_message.reply_text("This bot is not enabled for this chat.")
        return
    from telegram_agent.competitive_bots import run_competitive_cycle
    from telegram_agent.research_publish import (
        format_competitive_telegram_message,
        publish_plain_to_target,
    )

    status = await update.effective_message.reply_text("Running competitive bots…")
    loop = asyncio.get_running_loop()

    def _run() -> dict:
        return run_competitive_cycle(cfg, cadence_label="telegram")

    try:
        out = await loop.run_in_executor(None, _run)
    except Exception as e:
        logger.exception("competitive bots failed")
        await context.bot.send_message(chat_id=cid, text=f"Competitive bots error: {e}")
        try:
            await status.delete()
        except Exception:
            pass
        return
    try:
        await status.delete()
    except Exception:
        pass
    if not out.get("ok"):
        await context.bot.send_message(
            chat_id=cid,
            text=f"Competitive bots: {out.get('error', 'failed')}",
        )
        return
    txt = format_competitive_telegram_message(out)
    if cfg.get("competitive_bots_publish", True):
        try:
            publish_plain_to_target(cfg, txt)
        except Exception as e:
            logger.warning("Publish competitive summary failed: %s", e)
    # Telegram message limit ~4096
    chunk = txt[:4000]
    if len(txt) > 4000:
        chunk += "\n…(truncated)"
    await context.bot.send_message(chat_id=cid, text=chunk)


async def cmd_competitive_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    cid = update.effective_chat.id
    if not chat_allowed(cfg, cid):
        await update.effective_message.reply_text("This bot is not enabled for this chat.")
        return
    from telegram_agent.competitive_backtest import run_competitive_backtest_all_intervals
    from telegram_agent.research_publish import (
        format_competitive_backtest_telegram_message,
        publish_plain_to_target,
    )

    status = await update.effective_message.reply_text("Running competitive backtest (may take a while)…")
    loop = asyncio.get_running_loop()

    def _run() -> dict:
        return run_competitive_backtest_all_intervals(cfg)

    try:
        out = await loop.run_in_executor(None, _run)
    except Exception as e:
        logger.exception("competitive backtest failed")
        await context.bot.send_message(chat_id=cid, text=f"Backtest error: {e}")
        try:
            await status.delete()
        except Exception:
            pass
        return
    try:
        await status.delete()
    except Exception:
        pass
    if not out.get("ok"):
        await context.bot.send_message(
            chat_id=cid,
            text=f"Backtest: {out.get('error', 'failed')}",
        )
        return
    txt = format_competitive_backtest_telegram_message(out)
    if cfg.get("competitive_bots_publish", True):
        try:
            publish_plain_to_target(cfg, txt)
        except Exception as e:
            logger.warning("Publish backtest summary failed: %s", e)
    chunk = txt[:4000]
    if len(txt) > 4000:
        chunk += "\n…(truncated)"
    await context.bot.send_message(chat_id=cid, text=chunk)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = load_config()
    cid = update.effective_chat.id
    if not chat_allowed(cfg, cid):
        await update.effective_message.reply_text("This bot is not enabled for this chat.")
        return
    path = Path(cfg["bot_schedule_file"])
    schedules = load_schedules(path)
    h = schedules.get(str(cid))
    jq = context.job_queue
    active = False
    if jq:
        active = any(j.name == job_name(cid) for j in jq.jobs())
    if h is not None:
        await update.effective_message.reply_text(
            f"Saved interval: every {h:g} hours.\n"
            f"Active timer job: {'yes' if active else 'no (restart bot restores from file)'}"
        )
    else:
        await update.effective_message.reply_text("No schedule. Use /schedule <hours>.")


async def channel_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    CommandHandler does not receive channel_post updates — only `message`.
    Channel commands are delivered as channel_post; we parse and dispatch here.
    """
    if not update.channel_post or not update.channel_post.text:
        return
    app = context.application
    if "bot_username" not in app.bot_data:
        me = await context.bot.get_me()
        app.bot_data["bot_username"] = (me.username or "").strip()
    cmd, args = parse_channel_command(update.channel_post.text, app.bot_data["bot_username"])
    if not cmd:
        return
    if cmd in ("start", "help"):
        await cmd_start(update, context)
    elif cmd == "digest":
        await cmd_digest(update, context)
    elif cmd == "schedule":
        await cmd_schedule_with_args(update, context, args)
    elif cmd == "status":
        await cmd_status(update, context)
    elif cmd == "competitive":
        await cmd_competitive(update, context)
    elif cmd == "competitive_backtest":
        await cmd_competitive_backtest(update, context)


async def post_init(application: Application) -> None:
    cfg = load_config()
    path = Path(cfg["bot_schedule_file"])
    schedules = load_schedules(path)
    jq = application.job_queue
    if jq is None:
        logger.error(
            "Job queue missing — install: pip install 'python-telegram-bot[job-queue]'"
        )
        return

    def build_callback(target_chat: int):
        async def _cb(ctx: ContextTypes.DEFAULT_TYPE) -> None:
            c = load_config()
            await execute_digest(ctx.application, target_chat, c)

        return _cb

    for cid_str, hours in schedules.items():
        try:
            cid = int(cid_str)
        except ValueError:
            continue
        remove_digest_jobs(jq, cid)
        jq.run_repeating(
            build_callback(cid),
            interval=timedelta(hours=hours),
            first=timedelta(minutes=5),
            chat_id=cid,
            name=job_name(cid),
        )
        logger.info("Restored digest schedule for chat %s every %s h", cid, hours)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg = load_config()
    token = (cfg.get("telegram_bot_token") or "").strip()
    if not token:
        logger.error("Set TELEGRAM_BOT_TOKEN in .env (from @BotFather).")
        sys.exit(1)

    application = Application.builder().token(token).post_init(post_init).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("digest", cmd_digest))
    application.add_handler(CommandHandler("schedule", cmd_schedule))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("competitive", cmd_competitive))
    application.add_handler(CommandHandler("competitive_backtest", cmd_competitive_backtest))

    # Channels send commands as channel_post; CommandHandler ignores them (messages only).
    application.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST
            & filters.TEXT
            & filters.Regex(r"^/"),
            channel_post_command,
        )
    )

    logger.info("Bot polling (Ctrl+C to stop)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
