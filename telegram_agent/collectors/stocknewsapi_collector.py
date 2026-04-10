"""Collect news from Stock News API (stocknewsapi.com)."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from telegram_agent.models import NewsItem
from telegram_agent.api_throttle import RateLimiter, chunked, retry_with_backoff

logger = logging.getLogger(__name__)


def _parse_et_ts(s: str) -> Optional[datetime]:
    """
    StockNewsAPI docs say timestamps are ET (GMT-0400). Responses typically include a date string.
    We treat unknown formats as UTC now.
    """
    if not s:
        return None
    t = str(s).strip()
    # Common format seen: "Wed, 08 Apr 2026 11:02:00 -0400"
    try:
        # Python can parse RFC 2822 via email.utils
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _mk_id(url: str, title: str, published: str) -> str:
    raw = (url or "") + "|" + (published or "") + "|" + (title or "")
    return "stocknewsapi:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _get_json(url: str, params: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
    import httpx

    with httpx.Client(timeout=timeout_s) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def collect_stocknewsapi(cfg: dict, *, since: datetime, until: datetime, mode: str) -> List[NewsItem]:
    """
    Incremental: uses section=general (broad macro headlines), plus optional tickers query if configured.
    Backfill: uses StockNewsAPI `date` parameter (MMDDYYYY-MMDDYYYY) when possible.
    """
    token = (cfg.get("stocknewsapi_token") or cfg.get("stocknews_api_key") or "").strip()
    if not token:
        logger.info("StockNewsAPI skipped: no STOCKNEWSAPI_TOKEN / STOCKNEWS_API_KEY")
        return []

    base = (cfg.get("stocknewsapi_base_url") or "https://stocknewsapi.com/api/v1").strip().rstrip("/")
    timeout_s = float(cfg.get("stocknewsapi_timeout_seconds", 20.0))
    items = int(cfg.get("stocknewsapi_items", 50))
    items = max(1, min(100, items))
    pages = int(cfg.get("stocknewsapi_pages", 1 if mode != "backfill" else 3))
    pages = max(1, min(20, pages))
    max_tickers = int(cfg.get("stocknewsapi_max_tickers_per_request", 50))
    max_tickers = max(1, min(200, max_tickers))
    max_requests = int(cfg.get("stocknewsapi_max_requests_per_run", 10 if mode != "backfill" else 50))
    sleep_s = float(cfg.get("stocknewsapi_sleep_seconds", 0.8))
    jitter_s = float(cfg.get("stocknewsapi_jitter_seconds", 0.25))
    max_retries = int(cfg.get("stocknewsapi_max_retries", 4))
    backoff_s = float(cfg.get("stocknewsapi_backoff_seconds", 1.0))

    tickers = cfg.get("stocknewsapi_tickers") or []
    if isinstance(tickers, str):
        tickers = [x.strip() for x in tickers.split(",") if x.strip()]

    out: List[NewsItem] = []
    limiter = RateLimiter(min_interval_seconds=max(0.0, sleep_s), jitter_seconds=max(0.0, jitter_s))
    since_utc = since.astimezone(timezone.utc)

    def _date_range_param() -> Optional[str]:
        if mode != "backfill":
            return None
        # Docs expect MMDDYYYY-MMDDYYYY in ET; we approximate with UTC dates.
        s = since.astimezone(timezone.utc).strftime("%m%d%Y")
        e = until.astimezone(timezone.utc).strftime("%m%d%Y")
        return f"{s}-{e}"

    date_param = _date_range_param()

    def _should_retry(exc: Exception) -> bool:
        try:
            import httpx

            if isinstance(exc, httpx.HTTPStatusError):
                code = int(exc.response.status_code)
                return code in (429, 500, 502, 503, 504)
            if isinstance(exc, httpx.TimeoutException):
                return True
            if isinstance(exc, httpx.TransportError):
                return True
        except Exception:
            pass
        return False

    def _get_json_throttled(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        def _one():
            limiter.wait()
            return _get_json(url, params=params, timeout_s=timeout_s)

        return retry_with_backoff(
            _one,
            max_retries=max_retries,
            base_sleep_seconds=backoff_s,
            should_retry=_should_retry,
        )

    # 1) General market news
    reqs_done = 0
    for page in range(1, pages + 1):
        if reqs_done >= max_requests:
            break
        params: Dict[str, Any] = {
            "section": "general",
            "items": items,
            "page": page,
            "token": token,
        }
        if date_param:
            params["date"] = date_param
        try:
            j = _get_json_throttled(base + "/category", params)
        except Exception as e:
            logger.warning("StockNewsAPI general page %s failed: %s", page, e)
            reqs_done += 1
            continue
        reqs_done += 1
        data = j.get("data") or j.get("news") or []
        if not isinstance(data, list) or not data:
            break
        for it in data:
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or it.get("headline") or "").strip() or "(No title)"
            desc = (it.get("text") or it.get("description") or it.get("summary") or "").strip()
            url = (it.get("news_url") or it.get("url") or "").strip() or None
            published = (it.get("date") or it.get("published") or it.get("time_published") or "").strip()
            ts = _parse_et_ts(published)
            if ts is None:
                # Avoid poisoning backfills with "now" when provider timestamp is missing/unparseable.
                ts = since_utc if mode == "backfill" else datetime.now(timezone.utc)
            if ts < since.astimezone(timezone.utc) or ts > until.astimezone(timezone.utc):
                # still keep during backfill date ranges (provider is ET-based); for incremental filter tightly
                if mode != "backfill":
                    continue
            nid = str(it.get("news_id") or it.get("id") or "")
            item_id = "stocknewsapi:" + nid if nid else _mk_id(url or "", title, published)
            out.append(
                NewsItem(
                    id=item_id,
                    source_type="api",
                    source_name="stocknewsapi:general",
                    title=title[:240],
                    content=(desc or title)[:4000],
                    url=url,
                    timestamp=ts,
                )
            )

    # 2) Optional: ticker news
    if tickers:
        tickers_norm = [str(t).strip().upper() for t in tickers if str(t).strip()]
        for ch in chunked(tickers_norm, max_tickers):
            if reqs_done >= max_requests:
                break
            tickers_q = ",".join(ch)
            for page in range(1, pages + 1):
                if reqs_done >= max_requests:
                    break
                params = {"tickers": tickers_q, "items": items, "page": page, "token": token}
                if date_param:
                    params["date"] = date_param
                try:
                    j = _get_json_throttled(base, params)
                except Exception as e:
                    logger.warning("StockNewsAPI tickers page %s failed: %s", page, e)
                    reqs_done += 1
                    continue
                reqs_done += 1
                data = j.get("data") or j.get("news") or []
                if not isinstance(data, list) or not data:
                    break
                for it in data:
                    if not isinstance(it, dict):
                        continue
                    title = (it.get("title") or it.get("headline") or "").strip() or "(No title)"
                    desc = (it.get("text") or it.get("description") or it.get("summary") or "").strip()
                    url = (it.get("news_url") or it.get("url") or "").strip() or None
                    published = (it.get("date") or it.get("published") or it.get("time_published") or "").strip()
                    ts = _parse_et_ts(published)
                    if ts is None:
                        ts = since_utc if mode == "backfill" else datetime.now(timezone.utc)
                    if ts < since.astimezone(timezone.utc) or ts > until.astimezone(timezone.utc):
                        if mode != "backfill":
                            continue
                    nid = str(it.get("news_id") or it.get("id") or "")
                    item_id = "stocknewsapi:" + nid if nid else _mk_id(url or "", title, published)
                    out.append(
                        NewsItem(
                            id=item_id,
                            source_type="api",
                            source_name=f"stocknewsapi:tickers:{tickers_q[:80]}",
                            title=title[:240],
                            content=(desc or title)[:4000],
                            url=url,
                            timestamp=ts,
                        )
                    )

    logger.info("StockNewsAPI collected %s item(s) [%s]", len(out), mode)
    return out

