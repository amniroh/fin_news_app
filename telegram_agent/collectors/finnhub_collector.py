"""Collect company news from Finnhub."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from telegram_agent.models import NewsItem
from telegram_agent.api_throttle import RateLimiter, retry_with_backoff

logger = logging.getLogger(__name__)


def _mk_id(symbol: str, url: str, dt: str, headline: str) -> str:
    raw = f"{symbol}|{url}|{dt}|{headline}"
    return "finnhub:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _unix_to_dt(ts: Any) -> Optional[datetime]:
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except Exception:
        return None


def collect_finnhub(cfg: dict, *, since: datetime, until: datetime, mode: str) -> List[NewsItem]:
    """
    Finnhub supports `company-news` with from/to per symbol (best for backfill).
    We only call Finnhub when `FINNHUB_API_KEY` is set AND `FINNHUB_SYMBOLS` is provided.
    """
    token = (cfg.get("finnhub_api_key") or "").strip()
    if not token:
        logger.info("Finnhub skipped: no FINNHUB_API_KEY")
        return []

    symbols = cfg.get("finnhub_symbols") or []
    if isinstance(symbols, str):
        symbols = [x.strip() for x in symbols.split(",") if x.strip()]
    symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not symbols:
        logger.info("Finnhub skipped: no FINNHUB_SYMBOLS configured (company-news requires symbols)")
        return []

    base = (cfg.get("finnhub_base_url") or "https://finnhub.io/api/v1").strip().rstrip("/")
    timeout_s = float(cfg.get("finnhub_timeout_seconds", 20.0))
    max_per_symbol = int(cfg.get("finnhub_max_items_per_symbol", 200 if mode == "backfill" else 50))
    max_per_symbol = max(1, min(500, max_per_symbol))
    max_symbols = int(cfg.get("finnhub_max_symbols_per_run", 0))
    sleep_s = float(cfg.get("finnhub_sleep_seconds", 0.25))
    jitter_s = float(cfg.get("finnhub_jitter_seconds", 0.10))
    max_retries = int(cfg.get("finnhub_max_retries", 4))
    backoff_s = float(cfg.get("finnhub_backoff_seconds", 1.2))

    from_d = since.astimezone(timezone.utc).date().isoformat()
    to_d = until.astimezone(timezone.utc).date().isoformat()

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
        syms = symbols if max_symbols <= 0 else symbols[: max(1, max_symbols)]
        for i, sym in enumerate(syms):
            params = {"symbol": sym, "from": from_d, "to": to_d, "token": token}

            def _one():
                limiter.wait()
                r = client.get(base + "/company-news", params=params)
                r.raise_for_status()
                return r.json()

            try:
                data = retry_with_backoff(
                    _one,
                    max_retries=max_retries,
                    base_sleep_seconds=backoff_s,
                    should_retry=_is_retryable,
                )
            except Exception as e:
                logger.warning("Finnhub company-news failed for %s: %s", sym, e)
                continue
            if not isinstance(data, list):
                continue
            for it in data[:max_per_symbol]:
                if not isinstance(it, dict):
                    continue
                headline = (it.get("headline") or "").strip() or "(No title)"
                summary = (it.get("summary") or "").strip()
                url = (it.get("url") or "").strip() or None
                ts = _unix_to_dt(it.get("datetime")) or datetime.now(timezone.utc)
                if mode != "backfill":
                    if ts < since.astimezone(timezone.utc) or ts > until.astimezone(timezone.utc):
                        continue
                item_id = str(it.get("id") or "").strip()
                if item_id:
                    nid = f"finnhub:{sym}:{item_id}"
                else:
                    nid = _mk_id(sym, url or "", str(it.get("datetime") or ""), headline)
                out.append(
                    NewsItem(
                        id=nid,
                        source_type="api",
                        source_name=f"finnhub:{sym}",
                        title=headline[:240],
                        content=(summary or headline)[:4000],
                        url=url,
                        timestamp=ts,
                    )
                )

    logger.info("Finnhub collected %s item(s) across %s symbol(s) [%s]", len(out), len(syms), mode)
    return out

