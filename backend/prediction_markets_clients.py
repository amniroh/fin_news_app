"""HTTP clients for Polymarket (Gamma + CLOB + Data API) and Kalshi."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import requests

logger = logging.getLogger(__name__)

POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"
POLYMARKET_DATA = "https://data-api.polymarket.com"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

DEFAULT_TIMEOUT = 30


def _parse_json_field(val: Any) -> List[Any]:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return []
    return []


def _parse_prices(outcome_prices: Any) -> tuple[Optional[float], Optional[float]]:
    prices = _parse_json_field(outcome_prices)
    if not prices:
        return None, None
    try:
        yes = float(prices[0])
        no = float(prices[1]) if len(prices) > 1 else max(0.0, 1.0 - yes)
        return yes, no
    except Exception:
        return None, None


def _iso_from_ts(ts: Optional[str]) -> Optional[str]:
    if not ts:
        return None
    s = str(ts).strip()
    if not s:
        return None
    if s.endswith("Z"):
        return s.replace("Z", "+00:00")
    return s


@dataclass
class NormalizedMarket:
    source: str
    external_id: str
    title: str
    description: Optional[str]
    category: Optional[str]
    status: str
    close_time_utc: Optional[str]
    blockchain_ref: Optional[str]
    token_ids: List[str]
    event_url: Optional[str]
    yes_price: Optional[float]
    no_price: Optional[float]
    volume: Optional[float]
    volume_24h: Optional[float]
    liquidity: Optional[float]
    transaction_volume: Optional[float]
    trade_count: int
    relevance_score: float
    settlement_result: Optional[str]  # yes | no | pending | void
    settlement_yes_value: Optional[float]
    raw: Dict[str, Any]


class PolymarketClient:
    def __init__(self, session: Optional[requests.Session] = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "market_analysis/1.0")

    def fetch_market_pool(
        self,
        *,
        pool_size: int = 800,
        active: Optional[bool] = True,
        closed: Optional[bool] = False,
        order: str = "volume24hr",
    ) -> List[NormalizedMarket]:
        """Fetch candidates ordered by API (default: 24h volume desc), then re-rank locally."""
        markets: List[NormalizedMarket] = []
        offset = 0
        page_size = 100
        while len(markets) < pool_size:
            batch = min(page_size, pool_size - len(markets))
            params: Dict[str, Any] = {
                "limit": batch,
                "offset": offset,
                "order": order,
                "ascending": "false",
            }
            if active is not None:
                params["active"] = str(active).lower()
            if closed is not None:
                params["closed"] = str(closed).lower()
            r = self.session.get(f"{POLYMARKET_GAMMA}/markets", params=params, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            for row in rows:
                markets.append(self._normalize(row))
            if len(rows) < batch:
                break
            offset += batch
            time.sleep(0.04)
        markets.sort(key=lambda m: m.relevance_score, reverse=True)
        return markets

    def iter_markets(
        self,
        *,
        limit: int = 500,
        active: Optional[bool] = None,
        closed: Optional[bool] = None,
        page_size: int = 100,
    ) -> Iterator[NormalizedMarket]:
        fetched = 0
        offset = 0
        while fetched < limit:
            batch = min(page_size, limit - fetched)
            params: Dict[str, Any] = {"limit": batch, "offset": offset}
            if active is not None:
                params["active"] = str(active).lower()
            if closed is not None:
                params["closed"] = str(closed).lower()
            r = self.session.get(f"{POLYMARKET_GAMMA}/markets", params=params, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            for row in rows:
                yield self._normalize(row)
                fetched += 1
                if fetched >= limit:
                    return
            if len(rows) < batch:
                break
            offset += batch
            time.sleep(0.05)

    def fetch_onchain_trades(self, condition_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Recent trades from Polymarket Data API (settlement layer activity)."""
        try:
            r = self.session.get(
                f"{POLYMARKET_DATA}/trades",
                params={"market": condition_id, "limit": limit},
                timeout=DEFAULT_TIMEOUT,
            )
            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("Polymarket trades fetch failed for %s: %s", condition_id, exc)
            return []

    @staticmethod
    def summarize_trades(trades: List[Dict[str, Any]]) -> tuple[float, int]:
        total = 0.0
        n = 0
        for t in trades:
            try:
                size = float(t.get("size") or 0)
                price = float(t.get("price") or 0)
                if size > 0 and price > 0:
                    total += size * price
                    n += 1
            except Exception:
                continue
        return total, n

    def fetch_price_history(self, token_id: str, *, interval: str = "1w") -> List[Dict[str, Any]]:
        try:
            r = self.session.get(
                f"{POLYMARKET_CLOB}/prices-history",
                params={"market": token_id, "interval": interval, "fidelity": 60},
                timeout=DEFAULT_TIMEOUT,
            )
            r.raise_for_status()
            return list(r.json().get("history") or [])
        except Exception as exc:
            logger.debug("Polymarket price history failed for %s: %s", token_id[:16], exc)
            return []

    def _normalize(self, row: Dict[str, Any]) -> NormalizedMarket:
        yes, no = _parse_prices(row.get("outcomePrices"))
        outcomes = _parse_json_field(row.get("outcomes"))
        token_ids = [str(t) for t in _parse_json_field(row.get("clobTokenIds"))]
        condition_id = str(row.get("conditionId") or row.get("id") or "")
        closed = bool(row.get("closed"))
        active = bool(row.get("active"))
        if closed:
            status = "settled" if yes is not None and (yes >= 0.99 or yes <= 0.01) else "closed"
        elif active:
            status = "open"
        else:
            status = "unknown"

        settlement_result = "pending"
        settlement_yes_value: Optional[float] = None
        if status == "settled" and yes is not None:
            if yes >= 0.99:
                settlement_result = "yes"
                settlement_yes_value = 1.0
            elif yes <= 0.01:
                settlement_result = "no"
                settlement_yes_value = 0.0
            else:
                settlement_result = "void"
                settlement_yes_value = yes

        slug = row.get("slug")
        event_url = f"https://polymarket.com/event/{slug}" if slug else None
        vol_total = float(row["volumeNum"]) if row.get("volumeNum") not in (None, "") else (
            float(row["volume"]) if row.get("volume") not in (None, "") else None
        )
        vol_24h = float(row["volume24hr"]) if row.get("volume24hr") not in (None, "") else (
            float(row.get("volume24hrClob") or 0) or None
        )
        liq = float(row["liquidityNum"]) if row.get("liquidityNum") not in (None, "") else (
            float(row["liquidity"]) if row.get("liquidity") not in (None, "") else None
        )
        title = str(row.get("question") or row.get("title") or condition_id)
        relevance = _trading_relevance_score(
            source="polymarket",
            title=title,
            external_id=condition_id,
            volume=vol_total,
            volume_24h=vol_24h,
            liquidity=liq,
            yes_price=yes,
            status=status,
        )
        return NormalizedMarket(
            source="polymarket",
            external_id=condition_id,
            title=title,
            description=row.get("description"),
            category=row.get("category"),
            status=status,
            close_time_utc=_iso_from_ts(row.get("endDate") or row.get("endDateIso")),
            blockchain_ref=condition_id,
            token_ids=token_ids,
            event_url=event_url,
            yes_price=yes,
            no_price=no,
            volume=vol_total,
            volume_24h=vol_24h,
            liquidity=liq,
            transaction_volume=None,
            trade_count=0,
            relevance_score=relevance,
            settlement_result=settlement_result,
            settlement_yes_value=settlement_yes_value,
            raw=row,
        )


class KalshiClient:
    def __init__(self, session: Optional[requests.Session] = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "market_analysis/1.0")

    def fetch_market_pool(
        self,
        *,
        pool_size: int = 800,
        status: Optional[str] = "open",
        page_size: int = 200,
    ) -> List[NormalizedMarket]:
        """Paginate Kalshi markets and rank by trading relevance (24h + total volume)."""
        markets: List[NormalizedMarket] = []
        cursor: Optional[str] = None
        while len(markets) < pool_size:
            batch = min(page_size, pool_size - len(markets))
            params: Dict[str, Any] = {"limit": batch, "mve_filter": "exclude"}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            r = self.session.get(f"{KALSHI_API}/markets", params=params, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            rows = data.get("markets") or []
            if not rows:
                break
            for row in rows:
                markets.append(self._normalize(row))
            cursor = data.get("cursor") or ""
            if not cursor:
                break
            time.sleep(0.04)
        markets.sort(key=lambda m: m.relevance_score, reverse=True)
        return markets

    def iter_markets(
        self,
        *,
        limit: int = 500,
        status: Optional[str] = None,
        page_size: int = 200,
    ) -> Iterator[NormalizedMarket]:
        fetched = 0
        cursor: Optional[str] = None
        while fetched < limit:
            batch = min(page_size, limit - fetched)
            params: Dict[str, Any] = {"limit": batch}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            r = self.session.get(f"{KALSHI_API}/markets", params=params, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            rows = data.get("markets") or []
            if not rows:
                break
            for row in rows:
                yield self._normalize(row)
                fetched += 1
                if fetched >= limit:
                    return
            cursor = data.get("cursor") or ""
            if not cursor:
                break
            time.sleep(0.05)

    def _normalize(self, row: Dict[str, Any]) -> NormalizedMarket:
        ticker = str(row.get("ticker") or "")
        status_raw = str(row.get("status") or "unknown").lower()
        if status_raw in ("active", "open"):
            status = "open"
        elif status_raw in ("settled", "finalized", "determined"):
            status = "settled"
        elif status_raw == "closed":
            status = "closed"
        else:
            status = status_raw

        def _f(key: str) -> Optional[float]:
            v = row.get(key)
            if v in (None, ""):
                return None
            try:
                return float(v)
            except Exception:
                return None

        yes_bid = _f("yes_bid_dollars")
        yes_ask = _f("yes_ask_dollars")
        last = _f("last_price_dollars")
        yes_price = last
        if yes_price is None and yes_bid is not None and yes_ask is not None:
            yes_price = (yes_bid + yes_ask) / 2.0
        if yes_price is None and yes_bid is not None:
            yes_price = yes_bid
        no_price = (1.0 - yes_price) if yes_price is not None else None

        result = str(row.get("result") or "").strip().lower()
        settlement_result = "pending"
        settlement_yes_value: Optional[float] = None
        if status == "settled" and result in ("yes", "no"):
            settlement_result = result
            settlement_yes_value = 1.0 if result == "yes" else 0.0
        elif status == "settled":
            settlement_result = "void"

        event_ticker = row.get("event_ticker")
        event_url = f"https://kalshi.com/markets/{event_ticker}" if event_ticker else None
        vol_total = _f("volume_fp")
        vol_24h = _f("volume_24h_fp")
        liq = _f("open_interest_fp")
        title = str(row.get("title") or ticker)
        relevance = _trading_relevance_score(
            source="kalshi",
            title=title,
            external_id=ticker,
            volume=vol_total,
            volume_24h=vol_24h,
            liquidity=liq,
            yes_price=yes_price,
            status=status,
        )
        return NormalizedMarket(
            source="kalshi",
            external_id=ticker,
            title=title,
            description=row.get("rules_primary"),
            category=row.get("market_type"),
            status=status,
            close_time_utc=_iso_from_ts(row.get("close_time") or row.get("latest_expiration_time")),
            blockchain_ref=ticker,
            token_ids=[],
            event_url=event_url,
            yes_price=yes_price,
            no_price=no_price,
            volume=vol_total,
            volume_24h=vol_24h,
            liquidity=liq,
            transaction_volume=vol_24h,
            trade_count=0,
            relevance_score=relevance,
            settlement_result=settlement_result,
            settlement_yes_value=settlement_yes_value,
            raw=row,
        )


def _trading_relevance_score(
    *,
    source: str,
    title: str,
    external_id: str,
    volume: Optional[float],
    volume_24h: Optional[float],
    liquidity: Optional[float],
    yes_price: Optional[float],
    status: str,
) -> float:
    """Higher = more useful for trading (volume, liquidity, simple markets)."""
    v24 = float(volume_24h or 0)
    vtot = float(volume or 0)
    liq = float(liquidity or 0)
    score = v24 * 4.0 + vtot * 0.0005 + liq * 0.5
    t = title.lower()
    eid = external_id.upper()
    if source == "kalshi" and ("MULTIGAME" in eid or t.count(",") >= 4):
        score *= 0.02
    if "up or down" in t and ("5m" in t or "10:30" in t):
        score *= 0.05
    if yes_price is not None and (yes_price <= 0.02 or yes_price >= 0.98) and status == "open":
        score *= 0.5
    if v24 <= 0 and vtot <= 0:
        score *= 0.01
    return score
