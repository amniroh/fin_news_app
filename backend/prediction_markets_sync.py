"""Sync prediction-market signals and compute time-sensitivity + win/loss analytics."""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from prediction_markets_clients import KalshiClient, NormalizedMarket, PolymarketClient
from prediction_markets_categories import (
    infer_categories,
    matches_allowlist,
)
from prediction_markets_store import (
    delete_signals_not_in_categories,
    get_first_snapshot,
    get_snapshots_in_window,
    init_prediction_markets_db,
    insert_snapshot,
    record_source_sync,
    repair_polymarket_event_urls,
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
    amount_won: Optional[float] = None
    amount_lost: Optional[float] = None
    win_rate: Optional[float] = None

    if settlement_result and settlement_result not in ("pending", "void") and settlement_yes_value is not None and entry_yes is not None:
        pending = 0
        favor_yes = entry_yes >= 0.5
        entry_side_price = entry_yes if favor_yes else (1.0 - entry_yes)
        won_side_value = settlement_yes_value if favor_yes else (1.0 - settlement_yes_value)
        profit = float(won_side_value) - float(entry_side_price)
        signal_won = profit > 0
        # Split P&L into separate won/lost columns (fraction of stake on the favored side).
        if profit > 0:
            amount_won = float(profit)
            amount_lost = 0.0
            wins = 1
        else:
            amount_won = 0.0
            amount_lost = float(abs(profit))
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
        "amount_won": amount_won,
        "amount_lost": amount_lost,
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


def _enrich_kalshi_categories(markets: List[NormalizedMarket], kalshi: KalshiClient) -> None:
    """Fill category from series metadata for markets that only have title-based hints."""
    for m in markets:
        if m.source != "kalshi":
            continue
        event_ticker = None
        try:
            event_ticker = (m.raw or {}).get("event_ticker")
        except Exception:
            event_ticker = None
        meta = kalshi.resolve_series_meta(event_ticker=str(event_ticker) if event_ticker else None)
        cats = infer_categories(
            title=m.title,
            category=meta.get("category") or m.category,
            tags=list(meta.get("tags") or []),
        )
        if cats:
            m.category = ",".join(cats)
        time.sleep(0.02)


def _assign_categories(
    markets: List[NormalizedMarket],
    *,
    kalshi: Optional[KalshiClient] = None,
) -> None:
    """Infer/normalize category labels on markets (does not filter)."""
    if kalshi is not None:
        need = [m for m in markets if m.source == "kalshi"]
        need = sorted(need, key=lambda x: x.relevance_score, reverse=True)[:800]
        _enrich_kalshi_categories(need, kalshi)
    for m in markets:
        cats = infer_categories(title=m.title, category=m.category)
        if cats:
            m.category = ",".join(cats)


def _filter_by_category_allowlist(
    markets: List[NormalizedMarket],
    allowlist: List[str],
    *,
    kalshi: Optional[KalshiClient] = None,
) -> List[NormalizedMarket]:
    """Keep markets whose inferred categories intersect the allowlist."""
    _assign_categories(markets, kalshi=kalshi)
    kept: List[NormalizedMarket] = []
    for m in markets:
        cats = [c.strip() for c in str(m.category or "").split(",") if c.strip()]
        if matches_allowlist(cats, allowlist):
            kept.append(m)
    return kept


def sync_prediction_markets(
    con: sqlite3.Connection,
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
    keep_per_source: int = DEFAULT_KEEP_PER_SOURCE,
    settled_keep: int = DEFAULT_SETTLED_KEEP,
    include_settled: bool = True,
    fetch_polymarket_trades: bool = True,
    category_allowlist: Optional[List[str]] = None,
    wipe_disallowed: bool = False,
) -> Dict[str, Any]:
    """
    Fetch a large candidate pool per source, rank by trading relevance (24h volume,
    liquidity, penalize combo/noise markets), and persist only the top signals.

    Polymarket Gamma caps deep ``offset`` (~2100); we also scan alternate orderings
    (volume24hr, volume, liquidity) and dedupe to maximize coverage.

    By default all categories are stored. Pass ``category_allowlist`` to restrict
    fetches; set ``wipe_disallowed`` to delete non-matching rows already in the DB.
    """
    init_prediction_markets_db(con)
    # None = all categories; empty list treated as all.
    allowlist = list(category_allowlist) if category_allowlist else None
    poly = PolymarketClient()
    kalshi = KalshiClient()
    # Cap Polymarket single-order scans under the Gamma offset limit.
    poly_pool = min(int(pool_size), 2000)
    stats: Dict[str, Any] = {
        "polymarket": 0,
        "kalshi": 0,
        "pool_polymarket": 0,
        "pool_kalshi": 0,
        "keep_per_source": keep_per_source,
        "pool_size": pool_size,
        "category_allowlist": allowlist or ["*"],
        "errors": [],
    }

    try:
        by_id: Dict[str, NormalizedMarket] = {}
        for order in ("volume24hr", "volume", "liquidity"):
            for m in poly.fetch_market_pool(
                pool_size=poly_pool, active=True, closed=False, order=order
            ):
                prev = by_id.get(m.external_id)
                if prev is None or m.relevance_score > prev.relevance_score:
                    by_id[m.external_id] = m
            time.sleep(0.2)
        open_pool = sorted(by_id.values(), key=lambda m: m.relevance_score, reverse=True)
        stats["pool_polymarket"] = len(open_pool)
        if allowlist:
            open_pool = _filter_by_category_allowlist(open_pool, allowlist)
        else:
            _assign_categories(open_pool)
        stats["pool_polymarket_filtered"] = len(open_pool)
        for m in open_pool[:keep_per_source]:
            _persist_market(con, m, fetch_trades=fetch_polymarket_trades, poly_client=poly)
            stats["polymarket"] += 1
        if include_settled:
            settled_by_id: Dict[str, NormalizedMarket] = {}
            for order in ("volume", "liquidity"):
                try:
                    for m in poly.fetch_market_pool(
                        pool_size=min(max(settled_keep * 3, 200), 2000),
                        active=None,
                        closed=True,
                        order=order,
                    ):
                        prev = settled_by_id.get(m.external_id)
                        if prev is None or m.relevance_score > prev.relevance_score:
                            settled_by_id[m.external_id] = m
                except Exception as exc:
                    logger.warning("Polymarket settled scan order=%s failed: %s", order, exc)
                time.sleep(0.2)
            settled_pool = sorted(settled_by_id.values(), key=lambda m: m.relevance_score, reverse=True)
            if allowlist:
                settled_pool = _filter_by_category_allowlist(settled_pool, allowlist)
            else:
                _assign_categories(settled_pool)
            stats["pool_polymarket_settled"] = len(settled_pool)
            for m in settled_pool[:settled_keep]:
                _persist_market(con, m, fetch_trades=False, poly_client=poly)
                stats["polymarket"] += 1
        record_source_sync(
            con,
            "polymarket",
            markets_fetched=stats["polymarket"],
            meta={
                "pool_size": pool_size,
                "poly_pool_cap": poly_pool,
                "keep": keep_per_source,
                "category_allowlist": allowlist or ["*"],
                "ranked_by": "volume24hr+volume+liquidity+relevance",
            },
        )
    except Exception as exc:
        logger.exception("Polymarket sync failed: %s", exc)
        stats["errors"].append(f"polymarket: {exc}")

    try:
        open_pool = kalshi.fetch_market_pool(pool_size=pool_size, status="open")
        stats["pool_kalshi"] = len(open_pool)
        if allowlist:
            open_pool = _filter_by_category_allowlist(open_pool, allowlist, kalshi=kalshi)
        else:
            _assign_categories(open_pool, kalshi=kalshi)
        stats["pool_kalshi_filtered"] = len(open_pool)
        for m in open_pool[:keep_per_source]:
            _persist_market(con, m)
            stats["kalshi"] += 1
        if include_settled:
            settled_pool = kalshi.fetch_market_pool(pool_size=max(settled_keep * 3, 200), status="settled")
            if allowlist:
                settled_pool = _filter_by_category_allowlist(settled_pool, allowlist, kalshi=kalshi)
            else:
                _assign_categories(settled_pool, kalshi=kalshi)
            stats["pool_kalshi_settled"] = len(settled_pool)
            for m in settled_pool[:settled_keep]:
                _persist_market(con, m)
                stats["kalshi"] += 1
        record_source_sync(
            con,
            "kalshi",
            markets_fetched=stats["kalshi"],
            meta={
                "pool_size": pool_size,
                "keep": keep_per_source,
                "category_allowlist": allowlist or ["*"],
                "ranked_by": "volume24h+relevance",
            },
        )
    except Exception as exc:
        logger.exception("Kalshi sync failed: %s", exc)
        stats["errors"].append(f"kalshi: {exc}")

    try:
        stats["urls_repaired"] = repair_polymarket_event_urls(con)
    except Exception as exc:
        logger.warning("URL repair failed: %s", exc)
        stats["errors"].append(f"url_repair: {exc}")

    if wipe_disallowed and allowlist:
        try:
            stats["wiped"] = delete_signals_not_in_categories(con, allowlist)
        except Exception as exc:
            logger.exception("Wipe disallowed categories failed: %s", exc)
            stats["errors"].append(f"wipe: {exc}")

    stats["synced_at"] = _utcnow_iso()
    return stats


def _iso_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(microsecond=0).isoformat()


def backfill_price_history(
    con: sqlite3.Connection,
    *,
    days: int = 30,
    fidelity_minutes: int = 60,
    max_signals: Optional[int] = None,
    sources: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Pull historical YES prices for the last ``days`` into ``pm_signal_snapshots``.

    - Polymarket: CLOB ``prices-history`` (YES token, interval=1m, hourly fidelity).
    - Kalshi: series candlesticks (hourly) when available; short-lived markets may
      only return a few bars.
    """
    init_prediction_markets_db(con)
    end_ts = int(time.time())
    start_ts = end_ts - max(1, int(days)) * 86400
    src_filter = {s.strip().lower() for s in (sources or []) if s.strip()} or None

    where = ["1=1"]
    params: List[Any] = []
    if src_filter:
        placeholders = ",".join("?" for _ in src_filter)
        where.append(f"source IN ({placeholders})")
        params.extend(sorted(src_filter))
    sql = f"""
      SELECT id, source, external_id, token_ids_json, raw_json, status
      FROM pm_signals
      WHERE {' AND '.join(where)}
      ORDER BY CASE source WHEN 'polymarket' THEN 0 ELSE 1 END, last_seen_ts_utc DESC
    """
    if max_signals is not None:
        sql += " LIMIT ?"
        params.append(int(max_signals))

    rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    poly = PolymarketClient()
    kalshi = KalshiClient()
    out: Dict[str, Any] = {
        "days": int(days),
        "fidelity_minutes": int(fidelity_minutes),
        "signals_considered": len(rows),
        "polymarket_signals": 0,
        "kalshi_signals": 0,
        "snapshots_inserted": 0,
        "snapshots_skipped": 0,
        "errors": [],
    }

    import json as _json

    for i, row in enumerate(rows):
        sid = int(row["id"])
        source = str(row["source"]).lower()
        try:
            if source == "polymarket":
                try:
                    tokens = _json.loads(row.get("token_ids_json") or "[]")
                except Exception:
                    tokens = []
                if not tokens:
                    continue
                history = poly.fetch_price_history(
                    str(tokens[0]),
                    interval="1m",
                    fidelity=int(fidelity_minutes),
                    # Gamma/CLOB often returns empty when startTs/endTs are set;
                    # request the 1m window and filter client-side.
                )
                out["polymarket_signals"] += 1
                for pt in history:
                    try:
                        t = int(pt.get("t") or 0)
                        p = float(pt.get("p"))
                    except Exception:
                        continue
                    if t < start_ts or t > end_ts + 3600:
                        continue
                    inserted = insert_snapshot(
                        con,
                        signal_id=sid,
                        snap={
                            "asof_ts_utc": _iso_from_unix(t),
                            "yes_price": p,
                            "no_price": max(0.0, 1.0 - p) if p is not None else None,
                        },
                    )
                    if inserted:
                        out["snapshots_inserted"] += 1
                    else:
                        out["snapshots_skipped"] += 1
                time.sleep(0.03)

            elif source == "kalshi":
                try:
                    raw = _json.loads(row.get("raw_json") or "{}")
                except Exception:
                    raw = {}
                event_ticker = raw.get("event_ticker")
                sticks = kalshi.fetch_candlesticks(
                    str(row["external_id"]),
                    event_ticker=str(event_ticker) if event_ticker else None,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    period_interval=int(fidelity_minutes),
                )
                out["kalshi_signals"] += 1
                for c in sticks:
                    try:
                        t = int(c.get("end_period_ts") or 0)
                        price_block = c.get("price") or {}
                        p_raw = price_block.get("close_dollars") or price_block.get("mean_dollars")
                        p = float(p_raw) if p_raw not in (None, "") else None
                        vol = c.get("volume_fp")
                        vol_f = float(vol) if vol not in (None, "") else None
                        oi = c.get("open_interest_fp")
                        oi_f = float(oi) if oi not in (None, "") else None
                    except Exception:
                        continue
                    if not t or p is None:
                        continue
                    inserted = insert_snapshot(
                        con,
                        signal_id=sid,
                        snap={
                            "asof_ts_utc": _iso_from_unix(t),
                            "yes_price": p,
                            "no_price": max(0.0, 1.0 - p),
                            "volume": vol_f,
                            "liquidity": oi_f,
                        },
                    )
                    if inserted:
                        out["snapshots_inserted"] += 1
                    else:
                        out["snapshots_skipped"] += 1
                time.sleep(0.04)
        except Exception as exc:
            logger.warning("backfill failed signal_id=%s: %s", sid, exc)
            out["errors"].append(f"{source}:{row.get('external_id')}: {exc}")

        if (i + 1) % 50 == 0:
            logger.info(
                "backfill progress %d/%d inserted=%d",
                i + 1,
                len(rows),
                out["snapshots_inserted"],
            )

    out["finished_at"] = _utcnow_iso()
    return out
