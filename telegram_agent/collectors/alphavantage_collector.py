"""Collect news from Alpha Vantage NEWS_SENTIMENT endpoint."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from telegram_agent.models import NewsItem
from telegram_agent.api_throttle import RateLimiter, chunked, retry_with_backoff

logger = logging.getLogger(__name__)


def _mk_id(url: str, published: str, title: str) -> str:
    raw = (url or "") + "|" + (published or "") + "|" + (title or "")
    return "alphavantage:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _parse_time_published(s: str) -> Optional[datetime]:
    # Example: 20260408T033658
    if not s:
        return None
    t = str(s).strip()
    try:
        dt = datetime.strptime(t, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _fmt_time(dt: datetime) -> str:
    dtu = dt.astimezone(timezone.utc)
    return dtu.strftime("%Y%m%dT%H%M")


def collect_alphavantage_news(cfg: dict, *, since: datetime, until: datetime, mode: str) -> List[NewsItem]:
    """
    Alpha Vantage `NEWS_SENTIMENT` returns a feed of items (title/url/summary/time_published/etc).
    We support:
    - tickers: comma list via ALPHAVANTAGE_TICKERS (recommended)
    - topics: comma list via ALPHAVANTAGE_TOPICS (optional)
    """
    key = (cfg.get("alphavantage_api_key") or "").strip()
    if not key:
        logger.info("AlphaVantage skipped: no ALPHAVANTAGE_API_KEY")
        return []

    tickers = cfg.get("alphavantage_tickers") or []
    if isinstance(tickers, str):
        tickers = [x.strip() for x in tickers.split(",") if x.strip()]
    tickers = [str(x).strip().upper() for x in tickers if str(x).strip()]

    topics = cfg.get("alphavantage_topics") or []
    if isinstance(topics, str):
        topics = [x.strip() for x in topics.split(",") if x.strip()]
    topics = [str(x).strip().lower() for x in topics if str(x).strip()]

    if not tickers and not topics:
        logger.info("AlphaVantage skipped: set ALPHAVANTAGE_TICKERS and/or ALPHAVANTAGE_TOPICS")
        return []

    base = (cfg.get("alphavantage_base_url") or "https://www.alphavantage.co/query").strip()
    timeout_s = float(cfg.get("alphavantage_timeout_seconds", 25.0))
    limit = int(cfg.get("alphavantage_items", 100 if mode == "backfill" else 50))
    limit = max(1, min(1000, limit))
    max_tickers = int(cfg.get("alphavantage_max_tickers_per_request", 50))
    max_tickers = max(1, min(200, max_tickers))
    max_requests = int(cfg.get("alphavantage_max_requests_per_run", 5 if mode != "backfill" else 20))
    sleep_s = float(cfg.get("alphavantage_sleep_seconds", 12.5))  # AV free tier is often ~5/min
    jitter_s = float(cfg.get("alphavantage_jitter_seconds", 1.5))
    max_retries = int(cfg.get("alphavantage_max_retries", 4))
    backoff_s = float(cfg.get("alphavantage_backoff_seconds", 2.0))

    out: List[NewsItem] = []
    import httpx

    limiter = RateLimiter(min_interval_seconds=max(0.0, sleep_s), jitter_seconds=max(0.0, jitter_s))

    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            code = int(exc.response.status_code)
            return code in (429, 500, 502, 503, 504)
        if isinstance(exc, httpx.TimeoutException):
            return True
        if isinstance(exc, httpx.TransportError):
            return True
        return False

    with httpx.Client(timeout=timeout_s) as client:
        ticker_chunks: List[List[str]] = [[]]
        if tickers:
            ticker_chunks = chunked(list(tickers), max_tickers)

        reqs_done = 0
        for ch in ticker_chunks:
            if reqs_done >= max_requests:
                break

            params: Dict[str, Any] = {
                "function": "NEWS_SENTIMENT",
                "apikey": key,
                "limit": limit,
                "sort": "LATEST",
                "time_from": _fmt_time(since),
                "time_to": _fmt_time(until),
            }
            if ch:
                params["tickers"] = ",".join(ch)
            if topics:
                params["topics"] = ",".join(topics[:20])

            def _one():
                limiter.wait()
                r = client.get(base, params=params)
                r.raise_for_status()
                return r.json()

            try:
                j = retry_with_backoff(
                    _one,
                    max_retries=max_retries,
                    base_sleep_seconds=backoff_s,
                    should_retry=_is_retryable,
                )
            except Exception as e:
                logger.warning("AlphaVantage NEWS_SENTIMENT failed (chunk %s/%s): %s", reqs_done + 1, len(ticker_chunks), e)
                reqs_done += 1
                continue
            reqs_done += 1

            feed = j.get("feed") or []
            if not isinstance(feed, list):
                continue

            for it in feed:
                if not isinstance(it, dict):
                    continue
                title = (it.get("title") or "").strip() or "(No title)"
                url = (it.get("url") or "").strip() or None
                summary = (it.get("summary") or "").strip()
                tp = str(it.get("time_published") or "").strip()
                ts = _parse_time_published(tp) or datetime.now(timezone.utc)
                if mode != "backfill":
                    if ts < since.astimezone(timezone.utc) or ts > until.astimezone(timezone.utc):
                        continue
                item_id = _mk_id(url or "", tp, title)
                src = (it.get("source") or it.get("source_domain") or "alphavantage").strip()
                out.append(
                    NewsItem(
                        id=item_id,
                        source_type="api",
                        source_name=f"alphavantage:{src}",
                        title=title[:240],
                        content=(summary or title)[:4000],
                        url=url,
                        timestamp=ts,
                    )
                )

    logger.info("AlphaVantage collected %s item(s) [%s]", len(out), mode)
    return out

