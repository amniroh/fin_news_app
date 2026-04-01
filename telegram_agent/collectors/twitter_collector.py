"""Ingest tweets via X API v2 (Bearer token). Requires developer app access."""
import logging
from datetime import datetime, timezone
from typing import List, Optional

import httpx

from ..models import NewsItem

logger = logging.getLogger(__name__)

TWITTER_API = "https://api.twitter.com/2"


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_twitter_time(s: str) -> datetime:
    # 2024-01-15T12:00:00.000Z
    s = s.replace("Z", "+00:00")
    if "." in s:
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass
    return datetime.fromisoformat(s.split(".")[0] + "+00:00")


def collect_twitter(
    usernames: List[str],
    list_ids: List[str],
    since: datetime,
    bearer_token: str,
    max_tweets_per_source: int = 10,
) -> List[NewsItem]:
    """Fetch recent tweets from user timelines and list timelines."""
    if not bearer_token or not bearer_token.strip():
        return []

    usernames = [u.strip().lstrip("@") for u in usernames if u and u.strip()]
    list_ids = [x.strip() for x in list_ids if x and str(x).strip()]

    if not usernames and not list_ids:
        return []

    headers = {"Authorization": f"Bearer {bearer_token.strip()}"}
    items: List[NewsItem] = []
    start_time = _iso_utc(since)

    try:
        with httpx.Client(timeout=45.0, headers=headers) as client:
            for username in usernames:
                try:
                    items.extend(
                        _fetch_user_timeline(
                            client, username, start_time, max_tweets_per_source
                        )
                    )
                except Exception as e:
                    logger.warning("Twitter user @%s: %s", username, e)

            for lid in list_ids:
                try:
                    items.extend(
                        _fetch_list_timeline(client, lid, start_time, max_tweets_per_source)
                    )
                except Exception as e:
                    logger.warning("Twitter list %s: %s", lid, e)
    except Exception as e:
        logger.error("Twitter collector HTTP error: %s", e)

    return items


def _fetch_user_timeline(
    client: httpx.Client,
    username: str,
    start_time: str,
    max_results: int,
) -> List[NewsItem]:
    r = client.get(f"{TWITTER_API}/users/by/username/{username}")
    if r.status_code != 200:
        logger.warning("Twitter lookup @%s: HTTP %s", username, r.status_code)
        return []
    data = r.json().get("data") or {}
    uid = data.get("id")
    if not uid:
        return []

    params = {
        "tweet.fields": "created_at,author_id",
        "expansions": "author_id",
        "user.fields": "username",
        "max_results": min(100, max(5, max_results)),
        "start_time": start_time,
    }
    r = client.get(f"{TWITTER_API}/users/{uid}/tweets", params=params)
    if r.status_code != 200:
        logger.warning("Twitter tweets @%s: HTTP %s", username, r.status_code)
        return []

    return _tweets_to_items(
        r.json(),
        source_label=f"@{username}",
        url_username=username,
    )


def _fetch_list_timeline(
    client: httpx.Client,
    list_id: str,
    start_time: str,
    max_results: int,
) -> List[NewsItem]:
    params = {
        "tweet.fields": "created_at,author_id",
        "expansions": "author_id",
        "user.fields": "username",
        "max_results": min(100, max(5, max_results)),
        "start_time": start_time,
    }
    r = client.get(f"{TWITTER_API}/lists/{list_id}/tweets", params=params)
    if r.status_code != 200:
        logger.warning("Twitter list %s: HTTP %s", list_id, r.status_code)
        return []

    return _tweets_to_items(
        r.json(),
        source_label=f"Twitter list {list_id}",
        url_username=None,
    )


def _tweets_to_items(
    payload: dict,
    source_label: str,
    url_username: Optional[str],
) -> List[NewsItem]:
    out: List[NewsItem] = []
    users_by_id = {}
    for u in (payload.get("includes") or {}).get("users") or []:
        users_by_id[u.get("id")] = u.get("username", "user")

    for tw in payload.get("data") or []:
        tid = tw.get("id")
        text = (tw.get("text") or "").strip()
        if not tid or not text:
            continue
        created = tw.get("created_at")
        if created:
            try:
                ts = _parse_twitter_time(created)
            except Exception:
                ts = datetime.now(timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        author_id = tw.get("author_id")
        uname = users_by_id.get(author_id) if author_id else url_username
        link_user = uname or url_username or "i"
        url = f"https://twitter.com/{link_user}/status/{tid}"

        title = text.split("\n")[0][:220] if text else "(tweet)"
        item_id = f"tw:{tid}"
        if uname:
            src = f"@{uname}"
        elif url_username:
            src = f"@{url_username}"
        else:
            src = source_label

        out.append(
            NewsItem(
                id=item_id,
                source_type="twitter",
                source_name=src,
                title=title,
                content=text[:4000],
                url=url,
                timestamp=ts,
            )
        )
    return out
