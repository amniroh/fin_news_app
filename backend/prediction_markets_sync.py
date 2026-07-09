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
        "updated_ts_utc": _utcnow_iso(),
    }


def _persist_market(con: sqlite3.Connection, m: NormalizedMarket, *, fetch_trades: bool = False, poly_client: Optional[PolymarketClient] = None) -> int:
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
            "liquidity": m.liquidity,
        },
    )
    if fetch_trades and poly_client and m.source == "polymarket" and m.blockchain_ref:
        trades = poly_client.fetch_onchain_trades(m.blockchain_ref, limit=20)
        if trades:
            raw = m.raw.copy()
            raw["_recent_trades"] = trades[:5]
            upsert_signal(
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
                    "raw": raw,
                },
            )
    analytics = compute_analytics(
        con,
        signal_id,
        close_time_utc=m.close_time_utc,
        settlement_result=m.settlement_result,
        settlement_yes_value=m.settlement_yes_value,
        latest_yes_price=m.yes_price,
    )
    upsert_analytics(con, signal_id, analytics)
    return signal_id


def sync_prediction_markets(
    con: sqlite3.Connection,
    *,
    polymarket_limit: int = 400,
    kalshi_limit: int = 400,
    include_settled: bool = True,
    fetch_polymarket_trades: bool = True,
) -> Dict[str, Any]:
    init_prediction_markets_db(con)
    poly = PolymarketClient()
    kalshi = KalshiClient()
    stats: Dict[str, Any] = {"polymarket": 0, "kalshi": 0, "errors": []}

    try:
        for m in poly.iter_markets(limit=polymarket_limit, active=True, closed=False):
            _persist_market(con, m, fetch_trades=fetch_polymarket_trades, poly_client=poly)
            stats["polymarket"] += 1
        if include_settled:
            for m in poly.iter_markets(limit=max(100, polymarket_limit // 4), active=None, closed=True):
                _persist_market(con, m, fetch_trades=False, poly_client=poly)
                stats["polymarket"] += 1
        record_source_sync(con, "polymarket", markets_fetched=stats["polymarket"], meta={"limit": polymarket_limit})
    except Exception as exc:
        logger.exception("Polymarket sync failed: %s", exc)
        stats["errors"].append(f"polymarket: {exc}")

    try:
        for m in kalshi.iter_markets(limit=kalshi_limit, status="open"):
            _persist_market(con, m)
            stats["kalshi"] += 1
        if include_settled:
            for m in kalshi.iter_markets(limit=max(100, kalshi_limit // 4), status="settled"):
                _persist_market(con, m)
                stats["kalshi"] += 1
        record_source_sync(con, "kalshi", markets_fetched=stats["kalshi"], meta={"limit": kalshi_limit})
    except Exception as exc:
        logger.exception("Kalshi sync failed: %s", exc)
        stats["errors"].append(f"kalshi: {exc}")

    stats["synced_at"] = _utcnow_iso()
    return stats
