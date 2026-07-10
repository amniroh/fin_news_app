"""Sync prediction-market signals and compute time-sensitivity + win/loss analytics."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from prediction_markets_clients import KalshiClient, NormalizedMarket, PolymarketClient
from prediction_markets_store import (
    get_first_snapshot,
    get_snapshots_in_window,
    init_prediction_markets_db,
    insert_snapshot,
    record_source_sync,
    upsert_analytics,
    upsert_signal,
)

logger = logging.getLogger(__name__)

TIME_SENSITIVE_DAYS = 7.0
PRICE_SWING_THRESHOLD = 0.10

# Default sync: scan a large pool, keep only the most tradeable markets.
DEFAULT_POOL_SIZE = 800
DEFAULT_KEEP_PER_SOURCE = 200
DEFAULT_SETTLED_KEEP = 80


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _days_between(a: datetime, b: datetime) -> float:
    return abs((b - a).total_seconds()) / 86400.0


def compute_analytics(
    con: sqlite3.Connection,
    signal_id: int,
    *,
    close_time_utc: Optional[str],
    settlement_result: Optional[str],
    settlement_yes_value: Optional[float],
    latest_yes_price: Optional[float],
    volume_total: Optional[float] = None,
    volume_24h: Optional[float] = None,
    transaction_volume: Optional[float] = None,
    trade_count: int = 0,
    relevance_score: float = 0.0,
) -> Dict[str, Any]:
    first = get_first_snapshot(con, signal_id)
    entry_yes = float(first["yes_price"]) if first and first.get("yes_price") is not None else latest_yes_price
    entry_ts = _parse_dt(first["asof_ts_utc"] if first else None) or datetime.now(timezone.utc)
    close_dt = _parse_dt(close_time_utc)

    days_to_close: Optional[float] = None
    is_time_sensitive = False
    reasons: List[str] = []

    if close_dt:
        days_to_close = _days_between(entry_ts, close_dt)
        if days_to_close <= TIME_SENSITIVE_DAYS:
            is_time_sensitive = True
            reasons.append(f"resolves within {days_to_close:.1f} days")

    snaps = get_snapshots_in_window(con, signal_id)
    if len(snaps) >= 2 and entry_yes is not None:
        first_ts = _parse_dt(snaps[0]["asof_ts_utc"])
        for s in snaps[1:]:
            ts = _parse_dt(s["asof_ts_utc"])
            if not first_ts or not ts:
                continue
            if _days_between(first_ts, ts) * 24.0 <= 24.0 and s.get("yes_price") is not None:
                swing = abs(float(s["yes_price"]) - float(entry_yes))
                if swing >= PRICE_SWING_THRESHOLD:
                    is_time_sensitive = True
                    reasons.append(f"price moved {swing * 100:.0f}% within 24h")
                break

    pending = 1
    wins = 0
    losses = 0
    signal_won: Optional[bool] = None
    profit: Optional[float] = None
    win_rate: Optional[float] = None

    if settlement_result and settlement_result not in ("pending", "void") and settlement_yes_value is not None and entry_yes is not None:
        pending = 0
        favor_yes = entry_yes >= 0.5
        entry_side_price = entry_yes if favor_yes else (1.0 - entry_yes)
        won_side_value = settlement_yes_value if favor_yes else (1.0 - settlement_yes_value)
        profit = float(won_side_value) - float(entry_side_price)
        signal_won = profit > 0
        if signal_won:
            wins = 1
        else:
            losses = 1
        win_rate = float(wins)

    if not reasons and not is_time_sensitive:
        reasons.append("stable window; signal may remain actionable")

    return {
        "is_time_sensitive": is_time_sensitive,
        "time_sensitive_reason": "; ".join(reasons),
        "days_to_close_at_first": days_to_close,
        "entry_yes_price": entry_yes,
        "latest_yes_price": latest_yes_price,
        "settlement_result": settlement_result,
        "settlement_yes_value": settlement_yes_value,
        "signal_won": signal_won,
        "profit_if_followed": profit,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "win_rate": win_rate,
        "volume_total": volume_total,
        "volume_24h": volume_24h,
        "transaction_volume": transaction_volume,
        "trade_count": trade_count,
        "relevance_score": relevance_score,
        "updated_ts_utc": _utcnow_iso(),
    }


def _enrich_polymarket_trades(m: NormalizedMarket, poly: PolymarketClient) -> NormalizedMarket:
    if not m.blockchain_ref:
        return m
    trades = poly.fetch_onchain_trades(m.blockchain_ref, limit=50)
    txn_vol, trade_n = poly.summarize_trades(trades)
    if txn_vol > 0:
        m.transaction_volume = txn_vol
        m.trade_count = trade_n
    elif m.volume_24h:
        m.transaction_volume = m.volume_24h
    return m


def _persist_market(
    con: sqlite3.Connection,
    m: NormalizedMarket,
    *,
    fetch_trades: bool = False,
    poly_client: Optional[PolymarketClient] = None,
) -> int:
    if fetch_trades and poly_client and m.source == "polymarket":
        m = _enrich_polymarket_trades(m, poly_client)

    signal_id = upsert_signal(
        con,
        {
            "source": m.source,
            "external_id": m.external_id,
            "title": m.title,
            "description": m.description,
            "category": m.category,
            "status": m.status,
            "close_time_utc": m.close_time_utc,
            "blockchain_ref": m.blockchain_ref,
            "token_ids": m.token_ids,
            "event_url": m.event_url,
            "raw": m.raw,
        },
    )
    insert_snapshot(
        con,
        signal_id=signal_id,
        snap={
            "asof_ts_utc": _utcnow_iso(),
            "yes_price": m.yes_price,
            "no_price": m.no_price,
            "volume": m.volume,
            "volume_24h": m.volume_24h,
            "liquidity": m.liquidity,
            "transaction_volume": m.transaction_volume,
            "trade_count": m.trade_count,
        },
    )
    analytics = compute_analytics(
        con,
        signal_id,
        close_time_utc=m.close_time_utc,
        settlement_result=m.settlement_result,
        settlement_yes_value=m.settlement_yes_value,
        latest_yes_price=m.yes_price,
        volume_total=m.volume,
        volume_24h=m.volume_24h,
        transaction_volume=m.transaction_volume,
        trade_count=m.trade_count,
        relevance_score=m.relevance_score,
    )
    upsert_analytics(con, signal_id, analytics)
    return signal_id


def sync_prediction_markets(
    con: sqlite3.Connection,
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
    keep_per_source: int = DEFAULT_KEEP_PER_SOURCE,
    settled_keep: int = DEFAULT_SETTLED_KEEP,
    include_settled: bool = True,
    fetch_polymarket_trades: bool = True,
) -> Dict[str, Any]:
    """
    Fetch a large candidate pool per source, rank by trading relevance (24h volume,
    liquidity, penalize combo/noise markets), and persist only the top signals.
    """
    init_prediction_markets_db(con)
    poly = PolymarketClient()
    kalshi = KalshiClient()
    stats: Dict[str, Any] = {
        "polymarket": 0,
        "kalshi": 0,
        "pool_polymarket": 0,
        "pool_kalshi": 0,
        "keep_per_source": keep_per_source,
        "pool_size": pool_size,
        "errors": [],
    }

    try:
        open_pool = poly.fetch_market_pool(pool_size=pool_size, active=True, closed=False, order="volume24hr")
        stats["pool_polymarket"] = len(open_pool)
        for m in open_pool[:keep_per_source]:
            _persist_market(con, m, fetch_trades=fetch_polymarket_trades, poly_client=poly)
            stats["polymarket"] += 1
        if include_settled:
            settled_pool = poly.fetch_market_pool(
                pool_size=max(settled_keep * 3, 200),
                active=None,
                closed=True,
                order="volume",
            )
            stats["pool_polymarket_settled"] = len(settled_pool)
            for m in settled_pool[:settled_keep]:
                _persist_market(con, m, fetch_trades=False, poly_client=poly)
                stats["polymarket"] += 1
        record_source_sync(
            con,
            "polymarket",
            markets_fetched=stats["polymarket"],
            meta={"pool_size": pool_size, "keep": keep_per_source, "ranked_by": "volume24hr+relevance"},
        )
    except Exception as exc:
        logger.exception("Polymarket sync failed: %s", exc)
        stats["errors"].append(f"polymarket: {exc}")

    try:
        open_pool = kalshi.fetch_market_pool(pool_size=pool_size, status="open")
        stats["pool_kalshi"] = len(open_pool)
        for m in open_pool[:keep_per_source]:
            _persist_market(con, m)
            stats["kalshi"] += 1
        if include_settled:
            settled_pool = kalshi.fetch_market_pool(pool_size=max(settled_keep * 3, 200), status="settled")
            stats["pool_kalshi_settled"] = len(settled_pool)
            for m in settled_pool[:settled_keep]:
                _persist_market(con, m)
                stats["kalshi"] += 1
        record_source_sync(
            con,
            "kalshi",
            markets_fetched=stats["kalshi"],
            meta={"pool_size": pool_size, "keep": keep_per_source, "ranked_by": "volume24h+relevance"},
        )
    except Exception as exc:
        logger.exception("Kalshi sync failed: %s", exc)
        stats["errors"].append(f"kalshi: {exc}")

    stats["synced_at"] = _utcnow_iso()
    return stats
