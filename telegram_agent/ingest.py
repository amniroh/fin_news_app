"""Ingest news into agent DB: incremental vs backfill."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Literal, Optional, Sequence

from pathlib import Path

from telegram_agent.config import SESSION_DIR
from telegram_agent.models import NewsItem
from telegram_agent.agent_db import (
    connect,
    init_db,
    kv_get,
    kv_set,
    upsert_news_items,
    get_latest_news_ts_for_source_type,
    has_news_for_source_day_utc,
)
from telegram_agent.collectors.telegram_collector import collect_telegram_backfill
from telegram_agent.collectors.rss_collector import collect_rss
from telegram_agent.collectors.twitter_collector import collect_twitter
logger = logging.getLogger(__name__)

Mode = Literal["incremental", "backfill"]

def _fmt_spans(spans: Sequence[tuple[datetime, datetime]]) -> str:
    if not spans:
        return "(none)"
    parts: List[str] = []
    for a, b in spans:
        parts.append(f"{a.date().isoformat()}..{b.date().isoformat()}")
    return ", ".join(parts)


def _sample_list(xs: Sequence[str], n: int = 10) -> str:
    ys = [str(x) for x in xs if str(x).strip()]
    if not ys:
        return "(empty)"
    head = ys[: max(1, n)]
    more = len(ys) - len(head)
    if more > 0:
        return ", ".join(head) + f", ... (+{more})"
    return ", ".join(head)


def _normalize_ingest_source_mode(mode: Optional[str]) -> str:
    """
    all = telegram + rss + twitter (if configured) + api
    rss | telegram | api = that source only.
    """
    m = (mode or "all").strip().lower()
    if m in ("both", "all", ""):
        return "all"
    if m in ("rss", "telegram", "api"):
        return m
    logger.warning("Unknown SOURCE_MODE %r; using all", mode)
    return "all"


def _db_path(cfg: dict) -> Path:
    return Path(cfg.get("agent_db_path") or (SESSION_DIR.parent / "data" / "agent.sqlite"))


async def run_ingest(
    cfg: dict,
    *,
    mode: Mode,
    source_mode: Optional[str] = None,
    backfill_days: Optional[int] = None,
    force: bool = False,
    dry_run: bool = False,
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
        if not dry_run:
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

    mode_norm = _normalize_ingest_source_mode(
        source_mode if source_mode is not None else cfg.get("source_mode")
    )
    include_telegram = mode_norm in ("all", "telegram")
    include_rss = mode_norm in ("all", "rss")
    include_api = mode_norm in ("all", "api")
    include_twitter = mode_norm == "all"

    all_items: List[NewsItem] = []
    api_id = cfg.get("telegram_api_id")
    api_hash = cfg.get("telegram_api_hash")
    session_name = cfg.get("telegram_session_name", "news_agent")
    session_path = str(SESSION_DIR / session_name)
    max_tg = int(cfg.get("agent_max_telegram_backfill", 3000)) if mode == "backfill" else int(cfg.get("max_items_per_run", 80))

    # Incremental duplication skip: if latest row already covers since, skip source_type (unless force).
    if not force and mode == "incremental":
        def _skip_inc(st: str) -> bool:
            latest_ts = get_latest_news_ts_for_source_type(con, st)
            return bool(latest_ts and latest_ts >= since)

        if include_telegram and _skip_inc("telegram"):
            include_telegram = False
        if include_rss and _skip_inc("rss"):
            include_rss = False
        if include_api and _skip_inc("api"):
            include_api = False
        if include_twitter and _skip_inc("twitter"):
            include_twitter = False

    if dry_run and mode == "incremental":
        logger.info("INGEST DRY-RUN (incremental): since=%s sources=%s force=%s", since.isoformat(), mode_norm, force)
        if include_telegram:
            logger.info("  telegram: WOULD FETCH (channels=%s)", len(cfg.get("telegram_channels", []) or []))
        else:
            logger.info("  telegram: SKIP (already covered or not selected)")
        if include_rss:
            logger.info("  rss: WOULD FETCH (feeds=%s)", len(cfg.get("rss_feeds", []) or []))
        else:
            logger.info("  rss: SKIP (already covered or not selected)")
        if include_twitter:
            logger.info(
                "  twitter: WOULD FETCH (usernames=%s list_ids=%s)",
                len(cfg.get("twitter_usernames", []) or []),
                len(cfg.get("twitter_list_ids", []) or []),
            )
        else:
            logger.info("  twitter: SKIP (already covered or not selected)")
        if include_api:
            logger.info("  api: WOULD FETCH")
            logger.info("    finnhub symbols=%s sample=[%s]", len(cfg.get("finnhub_symbols", []) or []), _sample_list(cfg.get("finnhub_symbols", []) or [], 12))
            logger.info("    alphavantage tickers=%s sample=[%s] topics=%s", len(cfg.get("alphavantage_tickers", []) or []), _sample_list(cfg.get("alphavantage_tickers", []) or [], 12), _sample_list(cfg.get("alphavantage_topics", []) or [], 12))
            logger.info("    stocknewsapi tickers=%s sample=[%s]", len(cfg.get("stocknewsapi_tickers", []) or []), _sample_list(cfg.get("stocknewsapi_tickers", []) or [], 12))
        else:
            logger.info("  api: SKIP (already covered or not selected)")
        con.close()
        return 0

    # Backfill daily-level skip: fetch per UTC day only if missing for that source_type.
    if mode == "backfill":
        from_d = since.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        to_d = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        def _missing_days_and_ranges_for_source(
            st: str,
        ) -> tuple[set[datetime], List[tuple[datetime, datetime]]]:
            if force:
                # forced mode ignores DB coverage; treat full window as missing
                all_days: List[datetime] = []
                d0 = from_d
                while d0 <= to_d:
                    all_days.append(d0)
                    d0 = d0 + timedelta(days=1)
                if not all_days:
                    return set(), []
                return set(all_days), [(all_days[0], all_days[-1])]
            missing_days: List[datetime] = []
            d0 = from_d
            while d0 <= to_d:
                if not has_news_for_source_day_utc(con, source_type=st, day_utc=d0):
                    missing_days.append(d0)
                d0 = d0 + timedelta(days=1)
            if not missing_days:
                return set(), []
            spans: List[tuple[datetime, datetime]] = []
            s0 = missing_days[0]
            prev = missing_days[0]
            for cur in missing_days[1:]:
                if (cur - prev).days == 1:
                    prev = cur
                    continue
                spans.append((s0, prev))
                s0 = cur
                prev = cur
            spans.append((s0, prev))
            return set(missing_days), spans

        # Pre-compute missing day ranges first, then fetch only those intervals.
        tg_missing_days, tg_spans = _missing_days_and_ranges_for_source("telegram") if include_telegram else (set(), [])
        rss_missing_days, rss_spans = _missing_days_and_ranges_for_source("rss") if include_rss else (set(), [])
        tw_missing_days, tw_spans = _missing_days_and_ranges_for_source("twitter") if include_twitter else (set(), [])
        api_missing_days, api_spans = _missing_days_and_ranges_for_source("api") if include_api else (set(), [])

        if dry_run:
            logger.info(
                "INGEST DRY-RUN (backfill): window=%s..%s (%s day(s)) sources=%s force=%s",
                from_d.date().isoformat(),
                to_d.date().isoformat(),
                (to_d - from_d).days + 1,
                mode_norm,
                force,
            )
            if include_telegram:
                logger.info("  telegram: missing_days=%s spans=[%s]", len(tg_missing_days), _fmt_spans(tg_spans))
                logger.info("    channels=%s", len(cfg.get("telegram_channels", []) or []))
            if include_rss:
                logger.info("  rss: missing_days=%s spans=[%s]", len(rss_missing_days), _fmt_spans(rss_spans))
                logger.info("    feeds=%s", len(cfg.get("rss_feeds", []) or []))
            if include_twitter:
                logger.info("  twitter: missing_days=%s spans=[%s]", len(tw_missing_days), _fmt_spans(tw_spans))
                logger.info(
                    "    usernames=%s list_ids=%s",
                    len(cfg.get("twitter_usernames", []) or []),
                    len(cfg.get("twitter_list_ids", []) or []),
                )
            if include_api:
                logger.info("  api: missing_days=%s spans=[%s]", len(api_missing_days), _fmt_spans(api_spans))
                logger.info("    finnhub symbols=%s sample=[%s]", len(cfg.get("finnhub_symbols", []) or []), _sample_list(cfg.get("finnhub_symbols", []) or [], 12))
                logger.info("    alphavantage tickers=%s sample=[%s] topics=%s", len(cfg.get("alphavantage_tickers", []) or []), _sample_list(cfg.get("alphavantage_tickers", []) or [], 12), _sample_list(cfg.get("alphavantage_topics", []) or [], 12))
                logger.info("    stocknewsapi tickers=%s sample=[%s]", len(cfg.get("stocknewsapi_tickers", []) or []), _sample_list(cfg.get("stocknewsapi_tickers", []) or [], 12))
            con.close()
            return 0

        # Lazy imports for API collectors (avoid requiring their deps unless needed)
        if include_api and api_spans:
            from telegram_agent.collectors.stocknewsapi_collector import collect_stocknewsapi
            from telegram_agent.collectors.finnhub_collector import collect_finnhub
            from telegram_agent.collectors.alphavantage_collector import collect_alphavantage_news

        # Telegram: range-based backfill (collector supports since/until).
        if include_telegram and api_id and api_hash and tg_spans:
            from telethon import TelegramClient

            client = TelegramClient(session_path, int(api_id), api_hash)
            await client.start()
            try:
                for a, b in tg_spans:
                    end_excl = b + timedelta(days=1)
                    end = end_excl - timedelta(seconds=1)
                    tg_items = await collect_telegram_backfill(
                        client,
                        cfg.get("telegram_channels", []),
                        since=a,
                        until=end,
                        max_messages_per_channel=max_tg,
                    )
                    all_items.extend(tg_items)
            finally:
                await client.disconnect()

        # RSS: no until param → fetch from each missing day start and filter to the day.
        if include_rss and rss_spans:
            per_feed = 500
            for a, b in rss_spans:
                end_excl = b + timedelta(days=1)
                rss_items = collect_rss(
                    cfg.get("rss_feeds", []),
                    since=a,
                    max_entries_per_feed=per_feed,
                )
                for it in rss_items:
                    ts = it.timestamp
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    ts = ts.astimezone(timezone.utc)
                    day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
                    if a <= ts < end_excl and day in rss_missing_days:
                        all_items.append(it)

        # Twitter: no until param in collector → fetch from each missing day start and filter to the day.
        if include_twitter and tw_spans:
            bearer = cfg.get("twitter_bearer_token") or ""
            if bearer:
                for a, b in tw_spans:
                    end_excl = b + timedelta(days=1)

                    def _tw():
                        return collect_twitter(
                            cfg.get("twitter_usernames", []),
                            cfg.get("twitter_list_ids", []),
                            a,
                            bearer,
                            int(cfg.get("twitter_max_tweets_per_source", 10)),
                        )

                    tw_items = await asyncio.get_event_loop().run_in_executor(None, _tw)
                    for it in tw_items:
                        ts = it.timestamp
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        ts = ts.astimezone(timezone.utc)
                        day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
                        if a <= ts < end_excl and day in tw_missing_days:
                            all_items.append(it)

        # API providers: fetch per missing contiguous range (collectors support since/until).
        if include_api and api_spans:
            for a, b in api_spans:
                end_excl = b + timedelta(days=1)
                end = end_excl - timedelta(seconds=1)
                try:
                    all_items.extend(collect_stocknewsapi(cfg, since=a, until=end, mode=mode))
                except Exception as e:
                    logger.warning("StockNewsAPI ingest failed for %s..%s: %s", a.date().isoformat(), b.date().isoformat(), e)
                try:
                    all_items.extend(collect_finnhub(cfg, since=a, until=end, mode=mode))
                except Exception as e:
                    logger.warning("Finnhub ingest failed for %s..%s: %s", a.date().isoformat(), b.date().isoformat(), e)
                try:
                    all_items.extend(collect_alphavantage_news(cfg, since=a, until=end, mode=mode))
                except Exception as e:
                    logger.warning("AlphaVantage ingest failed for %s..%s: %s", a.date().isoformat(), b.date().isoformat(), e)

        stats = upsert_news_items(con, all_items)
        kv_set(con, "ingest:last_run_ts", now.isoformat())
        con.close()
        return stats.total

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

    if include_api:
        # API providers: optional; each collector self-skips if missing API key or config.
        # Keep this after RSS/Telegram so local sources still run even if API rate-limited.
        from telegram_agent.collectors.stocknewsapi_collector import collect_stocknewsapi
        from telegram_agent.collectors.finnhub_collector import collect_finnhub
        from telegram_agent.collectors.alphavantage_collector import (
            collect_alphavantage_news,
        )

        try:
            all_items.extend(collect_stocknewsapi(cfg, since=since, until=now, mode=mode))
        except Exception as e:
            logger.warning("StockNewsAPI ingest failed: %s", e)
        try:
            all_items.extend(collect_finnhub(cfg, since=since, until=now, mode=mode))
        except Exception as e:
            logger.warning("Finnhub ingest failed: %s", e)
        try:
            all_items.extend(collect_alphavantage_news(cfg, since=since, until=now, mode=mode))
        except Exception as e:
            logger.warning("AlphaVantage ingest failed: %s", e)

    stats = upsert_news_items(con, all_items)
    if not dry_run:
        kv_set(con, "ingest:last_run_ts", now.isoformat())
    logger.info(
        "Ingest upserted %s news rows (%s new ids, %s ids already in DB / updated); mode=%s, since=%s",
        stats.total,
        stats.new_count,
        stats.duplicate_count,
        mode,
        since.isoformat(),
    )
    for src, dup_n in sorted(
        stats.duplicates_by_source.items(),
        key=lambda x: (-x[1], x[0]),
    ):
        if dup_n > 0:
            logger.info(
                "Already ingested before (same id): %s rows from source %r",
                dup_n,
                src,
            )
    con.close()
    return stats.total
