"""Shared collect → dedupe → micro → summarize pipeline (CLI and Bot)."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Literal, Optional

from telegram_agent.config import DEFAULT_LLM_MODEL, SESSION_DIR
from telegram_agent.store import SeenStore
from telegram_agent.models import NewsItem
from telegram_agent.collectors.telegram_collector import collect_telegram
from telegram_agent.collectors.rss_collector import collect_rss
from telegram_agent.collectors.twitter_collector import collect_twitter
from telegram_agent.summarizer import summarize_digest, build_digest_prompts
from telegram_agent.micro_summarize import micro_summarize_items, estimate_micro_batches_plan
from telegram_agent.cost_estimate import estimate_digest_cost, log_run_cost_summary

logger = logging.getLogger(__name__)


def normalize_source_mode(mode: Optional[str]) -> str:
    """all = telegram + rss + twitter (if configured); rss | telegram = that source only."""
    m = (mode or "all").strip().lower()
    if m in ("both", "all", ""):
        return "all"
    if m in ("rss", "telegram"):
        return m
    logger.warning("Unknown SOURCE_MODE %r; using all", mode)
    return "all"


def _sort_items(items: List[NewsItem]) -> List[NewsItem]:
    return sorted(items, key=lambda x: x.timestamp, reverse=True)


def _log_costs(
    config: dict,
    all_items: List[NewsItem],
    micro_batches: List[dict],
    *,
    dry_run: bool,
    micro_estimated_only: bool,
) -> None:
    out_tokens = int(config.get("digest_assumed_output_tokens", 1800))
    model = config.get("llm_model", DEFAULT_LLM_MODEL)
    sys_p, usr_p = build_digest_prompts(all_items, config)
    digest_estimates = [
        (
            "Consolidated digest (Market + Trend)",
            estimate_digest_cost(sys_p, usr_p, model, out_tokens),
        ),
    ]
    log_run_cost_summary(
        micro_batches or None,
        digest_estimates,
        dry_run=dry_run,
        micro_estimated_only=micro_estimated_only,
        micro_disabled=not config.get("micro_summarize"),
    )


@dataclass
class DigestPipelineResult:
    kind: Literal["no_items", "dry_run", "ok"]
    hours_back: float
    dry_run_block: Optional[str] = None
    market_text: str = ""
    trend_text: str = ""


async def run_digest_pipeline(
    config: dict,
    *,
    dry_run: bool = False,
    source_mode: Optional[str] = None,
) -> DigestPipelineResult:
    """
    Collect sources, dedupe unseen, optional micro, consolidated LLM digest.
    Updates seen store when not dry_run and items exist.
    """
    config = {**config}

    hours_back = float(config.get("hours_back", 6))
    max_items = int(config.get("max_items_per_run", 80))
    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    store = SeenStore(config["seen_ids_file"])

    api_id = config.get("telegram_api_id")
    api_hash = config.get("telegram_api_hash")
    session_name = config.get("telegram_session_name", "news_agent")
    session_path = str(SESSION_DIR / session_name)

    mode = normalize_source_mode(
        source_mode if source_mode is not None else config.get("source_mode")
    )
    include_telegram = mode in ("all", "telegram")
    include_rss = mode in ("all", "rss")
    include_twitter = mode == "all"
    logger.info(
        "SOURCE_MODE=%s → collect telegram=%s rss=%s twitter=%s",
        mode,
        include_telegram,
        include_rss,
        include_twitter,
    )

    all_items: List[NewsItem] = []

    if include_telegram:
        if api_id and api_hash:
            from telethon import TelegramClient

            client = TelegramClient(session_path, int(api_id), api_hash)
            await client.start()
            try:
                tg_items = await collect_telegram(
                    client,
                    config.get("telegram_channels", []),
                    since=since,
                )
                all_items.extend(tg_items)
            finally:
                await client.disconnect()
        else:
            logger.warning(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH not set; skipping Telegram collection."
            )
    else:
        logger.info("Skipping Telegram channel collection (SOURCE_MODE=%s).", mode)

    if include_rss:
        rss_items = collect_rss(
            config.get("rss_feeds", []),
            since=since,
        )
        all_items.extend(rss_items)
    else:
        logger.info("Skipping RSS collection (SOURCE_MODE=%s).", mode)

    def _count_by_type(items):
        c = Counter(i.source_type for i in items)
        return c.get("telegram", 0), c.get("rss", 0), c.get("twitter", 0)

    bearer = config.get("twitter_bearer_token") or ""
    if include_twitter:
        if bearer:
            loop = asyncio.get_event_loop()
            tw_items = await loop.run_in_executor(
                None,
                lambda: collect_twitter(
                    config.get("twitter_usernames", []),
                    config.get("twitter_list_ids", []),
                    since,
                    bearer,
                    int(config.get("twitter_max_tweets_per_source", 10)),
                ),
            )
            all_items.extend(tw_items)
            logger.info("Twitter/X: collected %d tweets", len(tw_items))
        else:
            logger.info("TWITTER_BEARER_TOKEN not set; skipping X/Twitter collection.")
    else:
        logger.info("Skipping Twitter/X collection (SOURCE_MODE=%s).", mode)

    tg_n, rss_n, tw_n = _count_by_type(all_items)
    logger.info(
        "Collected before dedupe: total=%d (telegram=%d rss=%d twitter=%d)",
        len(all_items),
        tg_n,
        rss_n,
        tw_n,
    )

    unseen_ids = store.filter_unseen([i.id for i in all_items])
    all_items = [i for i in all_items if i.id in unseen_ids]
    tg_n, rss_n, tw_n = _count_by_type(all_items)
    logger.info(
        "After dedupe (unseen only): total=%d (telegram=%d rss=%d twitter=%d)",
        len(all_items),
        tg_n,
        rss_n,
        tw_n,
    )
    all_items = _sort_items(all_items)[:max_items]
    tg_n, rss_n, tw_n = _count_by_type(all_items)
    logger.info(
        "After sort + MAX_ITEMS_PER_RUN=%s: total=%d (telegram=%d rss=%d twitter=%d)",
        max_items,
        len(all_items),
        tg_n,
        rss_n,
        tw_n,
    )

    if not all_items:
        logger.info("No new items in the last %s hours. Skipping digest.", hours_back)
        if dry_run:
            logger.info("Dry-run: no cost estimate (nothing collected after dedupe).")
        return DigestPipelineResult(kind="no_items", hours_back=hours_back)

    if not dry_run:
        store.add_many([i.id for i in all_items])

    micro_batches: List[dict] = []
    micro_estimated_only = False

    if config.get("micro_summarize"):
        if dry_run:
            micro_batches = estimate_micro_batches_plan(all_items, config)
            micro_estimated_only = True
        else:
            all_items, micro_batches, _ = await micro_summarize_items(all_items, config)
            micro_estimated_only = False
    else:
        micro_batches = []
        micro_estimated_only = False

    _log_costs(
        config,
        all_items,
        micro_batches,
        dry_run=dry_run,
        micro_estimated_only=micro_estimated_only,
    )

    if dry_run:
        logger.info(
            "Dry run: consolidated digest prompts only (no digest LLM). Micro: %s.",
            "estimated only"
            if config.get("micro_summarize")
            else "disabled (MICRO_SUMMARIZE=false)",
        )
        dry_block, _ = await summarize_digest(all_items, config, dry_run=True)
        return DigestPipelineResult(
            kind="dry_run",
            hours_back=hours_back,
            dry_run_block=dry_block,
        )

    logger.info("Summarizing %d items (consolidated Market + Trend)...", len(all_items))
    market_text, trend_text = await summarize_digest(all_items, config, dry_run=False)
    return DigestPipelineResult(
        kind="ok",
        hours_back=hours_back,
        market_text=market_text or "",
        trend_text=trend_text or "",
    )
