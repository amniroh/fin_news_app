"""Collect news from RSS/Atom feeds."""
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

import feedparser

from ..models import NewsItem

logger = logging.getLogger(__name__)

# SEC and others may block generic bots; set RSS_USER_AGENT in .env if needed.
DEFAULT_RSS_UA = os.getenv(
    "RSS_USER_AGENT",
    "MarketAnalysisBot/1.0 (RSS; contact via project maintainer)",
)


def _parse_date(entry) -> Optional[datetime]:
    """Parse published/updated date from feed entry."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, key, None)
        if parsed and len(parsed) >= 6:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    return None


def collect_rss(
    feed_urls: List[str],
    since: datetime,
    max_entries_per_feed: int = 25,
) -> List[NewsItem]:
    """Fetch entries from RSS/Atom feeds since `since`."""
    if not feed_urls:
        return []

    items: List[NewsItem] = []
    for url in feed_urls:
        url = url.strip()
        if not url:
            continue
        try:
            feed = feedparser.parse(
                url,
                request_headers={
                    "User-Agent": DEFAULT_RSS_UA,
                    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                },
            )
            if feed.bozo and not getattr(feed, "entries", None):
                logger.warning("Feed parse issue for %s: %s", url, getattr(feed, "bozo_exception", ""))
            feed_title = getattr(feed.feed, "title", None) or urlparse(url).netloc or url
            raw_entries = list(getattr(feed, "entries", []) or [])
            skipped_old = 0
            count = 0
            for e in raw_entries:
                if count >= max_entries_per_feed:
                    break
                pub = _parse_date(e)
                if pub and pub < since:
                    skipped_old += 1
                    continue
                title = (getattr(e, "title", None) or "").strip() or "(No title)"
                content = (getattr(e, "summary", None) or getattr(e, "description", None) or "").strip()
                link = getattr(e, "link", None) or ""
                raw_id = getattr(e, "id", None) or link or title
                item_id = "rss:" + hashlib.sha256(f"{url}:{raw_id}".encode()).hexdigest()[:24]
                items.append(
                    NewsItem(
                        id=item_id,
                        source_type="rss",
                        source_name=feed_title,
                        title=title,
                        content=(content or title)[:4000],
                        url=link or None,
                        timestamp=pub or datetime.now(timezone.utc),
                    )
                )
                count += 1
            logger.info(
                "RSS %s: %d entries in feed, %d kept (in time window), %d skipped (older than cutoff), per-feed cap=%d",
                urlparse(url).netloc or url,
                len(raw_entries),
                count,
                skipped_old,
                max_entries_per_feed,
            )
            if len(raw_entries) > 0 and count == 0:
                logger.warning(
                    "RSS %s: all %d entries are older than your time window (HOURS_BACK). "
                    "Increase HOURS_BACK (e.g. 24 or 48) for slow feeds like SEC.",
                    url,
                    len(raw_entries),
                )
        except Exception as e:
            logger.warning("Failed to fetch feed %s: %s", url, e)

    return items
