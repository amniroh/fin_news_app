from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from types import SimpleNamespace

from value_metrics_cache import InMemoryTTLCache, get_or_fetch_metrics
from value_metrics_store import (
    add_to_watchlist,
    batch_latest_analyst_ratings,
    batch_latest_value_trading_assessments,
    connect,
    create_alert_rule,
    delete_alert_rule,
    ensure_user,
    get_watchlist,
    init_db,
    insert_alert_event,
    list_alert_events,
    list_alert_rules,
    list_enabled_rules,
    list_all_watchlist_symbols,
    list_interesting_stocks,
    remove_from_watchlist,
    set_alert_rule_enabled,
    query_fundamental_points,
    query_latest_daily_metric_points,
    query_metric_points,
    query_standard_metrics,
    query_stock_splits,
    query_yearly_metric_coverage,
    upsert_analyst_ratings,
    upsert_metric_points,
    update_rule_state,
)
from value_metrics_provider_fmp import fetch_ratios_history
from value_metrics_price_history import fetch_price_history
from value_metrics_stock_splits import persist_yfinance_splits_to_db
from interesting_stocks_api import register_interesting_stocks_routes

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_STANDARD_SUPPLEMENT_KEYS = (
    "mean_rsi_7d",
    "mean_rsi_30d",
    "mean_rsi_3m",
    "mean_rsi_1y",
    "total_return_1y",
    "total_return_3y",
    "total_return_5y",
    "total_return_10y",
    "ytd_return",
    "high_52w",
    "low_52w",
    "range_position_52w",
    "sharpe_ratio",
    "beta",
    "alpha",
    "volatility",
    "max_drawdown",
    "average_volume",
    "expense_ratio",
    "trailing_pe",
)

_DAILY_FALLBACK_KEYS = (
    "pe",
    "pb",
    "peg",
    "dividend_yield",
    "free_cash_flow_yield",
    "debt_to_equity",
    "roe",
    "current_ratio",
    "operating_margin",
    "ev_to_ebitda",
)

_VALUE_PILLAR_SPECS = (
    ("competitive_edge", "competitive_edge_score"),
    ("management_competence", "management_competence_score"),
    ("financial_fortress", "financial_fortress_score"),
    ("pricing_power", "pricing_power_score"),
    ("understandability", "understandability_score"),
    ("valuation", "valuation_score"),
)


def _apply_analyst_fields(row: Dict[str, Any], ar: Optional[Dict[str, Any]]) -> None:
    if not ar:
        return
    from interesting_stocks_service import recommendation_key_from_mean

    mean = ar.get("recommendation_mean")
    key = ar.get("recommendation_key") or recommendation_key_from_mean(mean)
    row["analyst_recommendation_key"] = key
    row["analyst_recommendation_mean"] = mean
    row["analyst_asof_date"] = ar.get("asof_date")


def _apply_value_trading_fields(row: Dict[str, Any], vt: Optional[Dict[str, Any]]) -> None:
    if not vt:
        return
    row["value_trading_score"] = vt.get("total_score")
    row["value_trading_assessed_ts"] = vt.get("produced_ts_utc")
    row["value_trading_summary"] = vt.get("overall_summary")
    pillars = vt.get("pillars") if isinstance(vt.get("pillars"), dict) else {}
    for key, col in _VALUE_PILLAR_SPECS:
        block = pillars.get(key) if isinstance(pillars.get(key), dict) else {}
        score = vt.get(col)
        if score is None:
            score = block.get("score")
        row[f"value_pillar_{key}"] = score
        rationale = block.get("rationale")
        if rationale:
            row[f"value_pillar_{key}_rationale"] = str(rationale).strip()


def _merge_daily_fallback(row: Dict[str, Any], daily: Optional[Dict[str, Any]]) -> None:
    if not daily:
        return
    for k in _DAILY_FALLBACK_KEYS:
        if row.get(k) is None and daily.get(k) is not None:
            row[k] = daily[k]
    if not row.get("metrics_asof_date") and daily.get("asof_date"):
        row["metrics_asof_date"] = daily.get("asof_date")


def _tracker_symbols(
    con: Any,
    *,
    symbols_param: str,
    priority: Optional[str],
) -> List[str]:
    if symbols_param and str(symbols_param).strip():
        return [s.strip().upper() for s in str(symbols_param).split(",") if s.strip()]
    stocks = list_interesting_stocks(con)
    pr = str(priority or "all").strip().lower()
    if pr in ("", "all", "any"):
        return [str(r["symbol"]).strip().upper() for r in stocks if r.get("symbol")]
    try:
        want = int(pr)
    except ValueError:
        raise HTTPException(status_code=400, detail="priority must be an integer (0, 1, 2, …) or 'all'")
    return [
        str(r["symbol"]).strip().upper()
        for r in stocks
        if r.get("symbol") and int(r.get("universe_priority", 99)) == want
    ]


def _fetch_tracker_rows(
    con: Any,
    cache: InMemoryTTLCache,
    syms: List[str],
) -> List[Dict[str, Any]]:
    """Assemble tracker rows from SQLite (fast). Live Yahoo is handled by daily jobs."""
    daily_map = {
        str(r["symbol"]): r
        for r in query_latest_daily_metric_points(con, symbols=syms, provider="yfinance")
    }
    std_map = {
        str(r["symbol"]): r
        for r in query_standard_metrics(con, symbols=syms, provider="yfinance")
    }
    priority_map = {
        str(r["symbol"]): int(r.get("universe_priority", 3))
        for r in list_interesting_stocks(con)
    }
    rows: List[Dict[str, Any]] = []
    for sym in syms:
        std = std_map.get(sym)
        if std:
            row = dict(std)
            row["data_source"] = "standard_metrics"
        else:
            row = {"data_source": "daily_fallback"}
        _merge_daily_fallback(row, daily_map.get(sym))
        row["symbol"] = sym
        row["universe_priority"] = priority_map.get(sym)
        rows.append(row)
    return _enrich_metric_rows(con, rows, cache=cache, allow_live=False)


def _merge_standard_supplement(row: Dict[str, Any], std: Dict[str, Any]) -> None:
    for k in _STANDARD_SUPPLEMENT_KEYS:
        if row.get(k) is None and std.get(k) is not None:
            row[k] = std[k]


def _enrich_metric_rows(
    con: Any,
    rows: List[Dict[str, Any]],
    *,
    cache: Optional[InMemoryTTLCache] = None,
    allow_live: bool = True,
) -> List[Dict[str, Any]]:
    """Join analyst ratings, value-trading scores, and standard metrics (RSI, momentum)."""
    syms = [str(r.get("symbol") or "").strip().upper() for r in rows if r.get("symbol")]
    if not syms:
        return rows
    analyst_map = batch_latest_analyst_ratings(con, syms)
    vt_map = batch_latest_value_trading_assessments(con, syms)
    std_map = {str(r["symbol"]): r for r in query_standard_metrics(con, symbols=syms, provider="yfinance")}
    out: List[Dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        sym = str(row.get("symbol") or "").strip().upper()
        std = std_map.get(sym) or {}
        _merge_standard_supplement(row, std)
        if allow_live and cache is not None and (
            row.get("mean_rsi_30d") is None or row.get("total_return_1y") is None
        ):
            try:
                live = get_or_fetch_metrics(cache, sym, con=con)
                _merge_standard_supplement(row, live)
            except Exception as e:
                logger.warning("Live supplement fetch failed for %s: %s", sym, e)
        _apply_analyst_fields(row, analyst_map.get(sym))
        _apply_value_trading_fields(row, vt_map.get(sym))
        out.append(row)

    if allow_live:
        missing_analyst = [sym for sym in syms if sym not in analyst_map]
        if missing_analyst:
            try:
                from interesting_stocks_service import fetch_yfinance_analyst_ratings

                for sym in missing_analyst:
                    snaps = fetch_yfinance_analyst_ratings(sym)
                    if not snaps:
                        continue
                    latest = max(snaps, key=lambda x: str(x.get("asof_date") or ""))
                    analyst_map[sym] = latest
                    upsert_analyst_ratings(con, snaps)
            except Exception:
                pass
            for row in out:
                sym = str(row.get("symbol") or "").strip().upper()
                if sym in missing_analyst:
                    _apply_analyst_fields(row, analyst_map.get(sym))

    return out


def _op_eval(op: str, x: Optional[float], thr: float) -> Optional[bool]:
    if x is None:
        return None
    o = (op or "").strip().lower()
    if o == "lt":
        return x < thr
    if o == "lte":
        return x <= thr
    if o == "gt":
        return x > thr
    if o == "gte":
        return x >= thr
    if o == "eq":
        return x == thr
    if o == "neq":
        return x != thr
    return None


ALLOWED_METRICS = {
    "pe",
    "pb",
    "peg",
    "dividend_yield",
    "free_cash_flow_yield",
    "debt_to_equity",
    "roe",
    "current_ratio",
    "operating_margin",
    "ev_to_ebitda",
}


def _extract_metric(metrics_row: Dict[str, Any], metric: str) -> Optional[float]:
    k = str(metric).strip()
    if k not in ALLOWED_METRICS:
        return None
    v = metrics_row.get(k)
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:
            return None
        return f
    except Exception:
        return None


class WatchlistUpdate(BaseModel):
    symbols: List[str] = Field(default_factory=list)


class AlertRuleCreate(BaseModel):
    symbol: str
    metric: str
    op: str
    threshold: float
    cooldown_minutes: int = 240
    enabled: bool = True


class AlertRuleEnable(BaseModel):
    enabled: bool


def build_value_router(
    *,
    db_path: Path,
    cache_ttl_seconds: int = 1800,
) -> APIRouter:
    router = APIRouter(prefix="/value", tags=["value-metrics"])
    # APIRouter doesn't always expose `.state` like FastAPI does; attach our own.
    if not hasattr(router, "state"):
        router.state = SimpleNamespace()  # type: ignore[attr-defined]
    cache = InMemoryTTLCache(ttl_seconds=int(cache_ttl_seconds))

    def _con():
        con = connect(db_path)
        init_db(con)
        return con

    @router.get("/metrics")
    async def get_metrics(symbols: str) -> Dict[str, Any]:
        """
        Read value metrics from SQLite (``vm_standard_metrics`` + enrichments).
        Does not call Yahoo on request; run ``daily_market_refresh`` to refresh snapshots.
        """
        syms = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()]
        if not syms:
            raise HTTPException(status_code=400, detail="symbols is required")
        con = _con()
        try:
            std_map = {
                str(r["symbol"]): r
                for r in query_standard_metrics(con, symbols=syms, provider="yfinance")
            }
            out = []
            for s in syms:
                row = dict(std_map.get(s) or {"symbol": s})
                row["symbol"] = s
                out.append(row)
            out = _enrich_metric_rows(con, out, cache=cache, allow_live=False)
            return {"ts_utc": _utcnow_iso(), "n": len(out), "rows": out}
        finally:
            con.close()

    @router.get("/metrics/tracker")
    async def get_tracker_metrics(
        symbols: str = "",
        priority: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Value Metrics Tracker row set: precomputed SQLite metrics (daily fundamentals,
        standard momentum/RSI, analyst ratings, value-trading pillars). Refreshed by
        ``daily_market_refresh`` and backfill jobs — no per-request Yahoo calls.
        Provide ``symbols`` (comma-separated) or ``priority`` (e.g. 0, 1, all).
        """

        def _run() -> Dict[str, Any]:
            con = _con()
            try:
                syms = _tracker_symbols(con, symbols_param=symbols, priority=priority)
                if not syms:
                    return {
                        "ts_utc": _utcnow_iso(),
                        "n": 0,
                        "rows": [],
                        "priority": priority,
                        "symbols": [],
                    }
                rows = _fetch_tracker_rows(con, cache, syms)
                return {
                    "ts_utc": _utcnow_iso(),
                    "n": len(rows),
                    "rows": rows,
                    "priority": priority,
                    "symbols": syms,
                }
            finally:
                con.close()

        return await run_in_threadpool(_run)

    @router.get("/metrics/latest-daily")
    async def get_latest_daily_metrics(symbols: str, provider: str = "yfinance") -> Dict[str, Any]:
        """
        Latest precomputed daily metric row per symbol from ``vm_metric_points`` (backfill pipeline).
        This P/E matches the SEC/yfinance daily computation used in tests — unlike ``GET /metrics``,
        which snapshots live Yahoo ``info`` (trailing P/E) into ``vm_standard_metrics``.
        """
        syms = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()]
        if not syms:
            raise HTTPException(status_code=400, detail="symbols is required")
        prov = (provider or "yfinance").strip().lower()
        if prov not in ("yfinance", "sec"):
            raise HTTPException(status_code=400, detail="provider must be yfinance or sec")
        con = _con()
        try:
            raw_rows = query_latest_daily_metric_points(con, symbols=syms, provider=prov)
            rows: List[Dict[str, Any]] = []
            for r in raw_rows:
                rows.append(
                    {
                        "symbol": r.get("symbol"),
                        "metrics_asof_date": r.get("asof_date"),
                        "data_source": "vm_metric_points_daily",
                        "provider": prov,
                        "pe": r.get("pe"),
                        "pb": r.get("pb"),
                        "peg": r.get("peg"),
                        "dividend_yield": r.get("dividend_yield"),
                        "free_cash_flow_yield": r.get("free_cash_flow_yield"),
                        "debt_to_equity": r.get("debt_to_equity"),
                        "roe": r.get("roe"),
                        "current_ratio": r.get("current_ratio"),
                        "operating_margin": r.get("operating_margin"),
                        "ev_to_ebitda": r.get("ev_to_ebitda"),
                        "fetched_ts_utc": r.get("fetched_ts_utc"),
                    }
                )
            rows = _enrich_metric_rows(con, rows, cache=cache)
            return {"ts_utc": _utcnow_iso(), "n": len(rows), "rows": rows}
        finally:
            con.close()

    @router.get("/coverage/yearly")
    async def get_coverage_yearly(
        symbol: str,
        provider: str = "yfinance",
        years: float = 20.0,
    ) -> Dict[str, Any]:
        """
        Per calendar year: fraction of trading days with price-derived daily metrics present (SQLite).
        """
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise HTTPException(status_code=400, detail="symbol is required")
        prov = (provider or "yfinance").strip().lower()
        if prov not in ("yfinance", "sec"):
            raise HTTPException(status_code=400, detail="provider must be yfinance or sec")
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=int(float(years) * 365.25))
        start_s = start.isoformat()
        end_s = end.isoformat()
        con = _con()
        try:
            agg = query_yearly_metric_coverage(con, symbol=sym, provider=prov, start_date=start_s, end_date=end_s)
            enriched = []
            for r in agg:
                year = int(r.get("year") or 0)
                n_days = int(r.get("n_days") or 0)
                item = dict(r)
                item["fraction_pe"] = (float(r.get("n_pe") or 0) / float(n_days)) if n_days else None
                enriched.append(item)
            return {
                "symbol": sym,
                "provider": prov,
                "period": "daily",
                "window": {"start": start_s, "end": end_s},
                "years": enriched,
            }
        finally:
            con.close()

    @router.get("/fundamentals/quarterly")
    async def get_quarterly_fundamentals(
        symbol: str,
        provider: str = "yfinance",
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Quarterly diluted EPS (and related fields) from ``vm_fundamental_points`` for yfinance or SEC backfill.
        """
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise HTTPException(status_code=400, detail="symbol is required")
        prov = (provider or "yfinance").strip().lower()
        if prov not in ("yfinance", "sec"):
            raise HTTPException(status_code=400, detail="provider must be yfinance or sec")
        con = _con()
        try:
            raw = query_fundamental_points(
                con,
                symbols=[sym],
                start_date=start,
                end_date=end,
                provider=prov,
                period="quarter",
            )
        finally:
            con.close()
        rows: List[Dict[str, Any]] = []
        for r in raw:
            rows.append(
                {
                    "symbol": r.get("symbol"),
                    "asof_date": r.get("asof_date"),
                    "period": r.get("period"),
                    "provider": r.get("provider"),
                    "eps": r.get("eps"),
                    "net_income": r.get("net_income"),
                    "revenue": r.get("revenue"),
                    "fetched_ts_utc": r.get("fetched_ts_utc"),
                }
            )
        return {"ts_utc": _utcnow_iso(), "symbol": sym, "provider": prov, "n": len(rows), "rows": rows}

    @router.get("/stock/splits")
    async def get_stock_splits(
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        Stock split events from ``vm_stock_splits`` (yfinance-sourced), keyed by ex-date.
        If no rows are stored yet, fetches from yfinance once and upserts. Use ``refresh=true`` to re-pull.
        """
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise HTTPException(status_code=400, detail="symbol is required")
        con = _con()
        try:
            init_db(con)
            rows = query_stock_splits(con, symbol=sym, start_date=start, end_date=end, provider="yfinance")
            if bool(refresh) or not rows:
                # Same-thread SQLite + short yfinance call (avoid passing ``con`` across threads).
                persist_yfinance_splits_to_db(con, sym)
                rows = query_stock_splits(con, symbol=sym, start_date=start, end_date=end, provider="yfinance")
        finally:
            con.close()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "symbol": r.get("symbol"),
                    "ex_date": r.get("ex_date"),
                    "split_ratio": r.get("split_ratio"),
                    "provider": r.get("provider"),
                    "fetched_ts_utc": r.get("fetched_ts_utc"),
                }
            )
        return {"ts_utc": _utcnow_iso(), "symbol": sym, "n": len(out), "rows": out}

    @router.post("/metrics/history/fetch")
    async def fetch_metrics_history(
        symbols: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        period: str = "quarter",
        provider: str = "fmp",
    ) -> Dict[str, Any]:
        """
        Fetch and store historical metric points for symbols for a time window.
        - provider currently supports: fmp
        - period: quarter|annual
        """
        syms = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()]
        if not syms:
            raise HTTPException(status_code=400, detail="symbols is required")
        prov = (provider or "").strip().lower()
        per = (period or "").strip().lower()
        if prov != "fmp":
            raise HTTPException(status_code=400, detail="Only provider=fmp is implemented for historical metrics")
        if per not in ("quarter", "annual"):
            raise HTTPException(status_code=400, detail="period must be quarter or annual")

        import os

        key = os.getenv("FMP_API_KEY", "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="FMP_API_KEY env var is required for provider=fmp")

        # Fetch in-process. (Can be parallelized later; keep simple and reliable.)
        fetched: List[Dict[str, Any]] = []
        for s in syms:
            try:
                pts = fetch_ratios_history(api_key=key, symbol=s, period=per, start_date=start, end_date=end)
                for p in pts:
                    fetched.append(p)
            except Exception:
                continue

        con = _con()
        try:
            n_up = upsert_metric_points(con, provider=prov, period=per, points=fetched)
        finally:
            con.close()

        return {"provider": prov, "period": per, "symbols_n": len(syms), "points_fetched": len(fetched), "points_upserted": n_up}

    @router.get("/metrics/history")
    async def get_metrics_history(
        symbols: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        period: str = "quarter",
        provider: str = "yfinance",
    ) -> Dict[str, Any]:
        """
        Read stored historical metric points (precomputed daily/quarterly rows in SQLite).
        Default provider is yfinance (daily pipeline); use sec for SEC-backed fundamentals.
        """
        syms = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()]
        if not syms:
            raise HTTPException(status_code=400, detail="symbols is required")
        prov = (provider or "").strip().lower()
        per = (period or "").strip().lower()
        con = _con()
        try:
            rows = query_metric_points(con, symbols=syms, start_date=start, end_date=end, provider=prov, period=per)
        finally:
            con.close()
        return {"provider": prov, "period": per, "n": len(rows), "rows": rows}

    @router.get("/price/history")
    async def get_price_history(
        symbol: str,
        interval: str = "daily",
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        OHLCV bars from Yahoo Finance for charting.

        - interval: daily | hourly | minute  (maps to 1d / 1h / 1m)
        - Minute data is clamped to ~last 7 days (yfinance limit).
        - Hourly span is clamped (~2y max).

        start/end: YYYY-MM-DD (optional). Defaults choose a sensible window per interval.
        """
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise HTTPException(status_code=400, detail="symbol is required")
        itv = (interval or "daily").strip().lower()
        if itv not in ("daily", "hourly", "minute"):
            raise HTTPException(status_code=400, detail="interval must be daily|hourly|minute")
        try:
            rows = await run_in_threadpool(
                lambda: fetch_price_history(symbol=sym, interval=itv, start=start, end=end)
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"symbol": sym, "interval": itv, "n": len(rows), "rows": rows}

    @router.get("/price/history/stored")
    async def get_stored_price_history(
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        years: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Daily close prices from ``agent.sqlite`` (backfill pipeline). No Yahoo calls.
        Default window: last ``years`` (default 1).
        """
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise HTTPException(status_code=400, detail="symbol is required")

        def _run() -> Dict[str, Any]:
            from interesting_stocks_service import _agent_price_history

            end_dt = datetime.now(timezone.utc).date()
            if end and str(end).strip():
                end_dt = datetime.fromisoformat(str(end).strip()[:10]).date()
            end_s = end_dt.isoformat()
            if start and str(start).strip():
                start_s = str(start).strip()[:10]
            else:
                start_s = (end_dt - timedelta(days=max(1, int(float(years) * 365.25)))).isoformat()
            rows = _agent_price_history(sym, start_s, end_s)
            return {
                "symbol": sym,
                "source": "agent_db",
                "start": start_s,
                "end": end_s,
                "n": len(rows),
                "rows": rows,
            }

        return await run_in_threadpool(_run)

    @router.get("/watchlist/{user_id}")
    async def watchlist_get(user_id: str) -> Dict[str, Any]:
        con = _con()
        try:
            ensure_user(con, user_id)
            return {"user_id": user_id, "symbols": get_watchlist(con, user_id)}
        finally:
            con.close()

    @router.post("/watchlist/{user_id}/add")
    async def watchlist_add(user_id: str, body: WatchlistUpdate) -> Dict[str, Any]:
        con = _con()
        try:
            n = add_to_watchlist(con, user_id, body.symbols)
            return {"user_id": user_id, "added": n, "symbols": get_watchlist(con, user_id)}
        finally:
            con.close()

    @router.post("/watchlist/{user_id}/remove")
    async def watchlist_remove(user_id: str, body: WatchlistUpdate) -> Dict[str, Any]:
        con = _con()
        try:
            n = remove_from_watchlist(con, user_id, body.symbols)
            return {"user_id": user_id, "removed": n, "symbols": get_watchlist(con, user_id)}
        finally:
            con.close()

    @router.get("/alerts/{user_id}")
    async def alerts_list(user_id: str) -> Dict[str, Any]:
        con = _con()
        try:
            ensure_user(con, user_id)
            return {"user_id": user_id, "rules": list_alert_rules(con, user_id), "events": list_alert_events(con, user_id)}
        finally:
            con.close()

    @router.post("/alerts/{user_id}/create")
    async def alerts_create(user_id: str, body: AlertRuleCreate) -> Dict[str, Any]:
        if body.metric not in ALLOWED_METRICS:
            raise HTTPException(status_code=400, detail=f"metric must be one of {sorted(ALLOWED_METRICS)}")
        if (body.op or "").strip().lower() not in ("lt", "lte", "gt", "gte", "eq", "neq"):
            raise HTTPException(status_code=400, detail="op must be one of lt,lte,gt,gte,eq,neq")
        con = _con()
        try:
            rid = create_alert_rule(
                con,
                user_id=user_id,
                symbol=body.symbol,
                metric=body.metric,
                op=body.op,
                threshold=body.threshold,
                cooldown_minutes=body.cooldown_minutes,
                enabled=body.enabled,
            )
            return {"user_id": user_id, "rule_id": rid, "rules": list_alert_rules(con, user_id)}
        finally:
            con.close()

    @router.post("/alerts/rule/{rule_id}/enabled")
    async def alerts_set_enabled(rule_id: int, body: AlertRuleEnable) -> Dict[str, Any]:
        con = _con()
        try:
            set_alert_rule_enabled(con, int(rule_id), bool(body.enabled))
            return {"ok": True}
        finally:
            con.close()

    @router.delete("/alerts/rule/{rule_id}")
    async def alerts_delete(rule_id: int) -> Dict[str, Any]:
        con = _con()
        try:
            delete_alert_rule(con, int(rule_id))
            return {"ok": True}
        finally:
            con.close()

    async def _alert_loop(stop_evt: asyncio.Event) -> None:
        """
        Periodically refresh metrics and trigger alert events on threshold crossings.
        This is provider-agnostic; it just reads the rules table.
        """
        poll_seconds = 900
        while not stop_evt.is_set():
            try:
                con = _con()
                try:
                    rules = list_enabled_rules(con)
                    # Warm cache for all watchlist symbols (best-effort).
                    for s in list_all_watchlist_symbols(con):
                        try:
                            get_or_fetch_metrics(cache, s, con=con)
                        except Exception:
                            pass

                    now = datetime.now(timezone.utc)
                    for r in rules:
                        rid = int(r["id"])
                        uid = str(r["user_id"])
                        sym = str(r["symbol"])
                        metric = str(r["metric"])
                        op = str(r["op"])
                        thr = float(r["threshold"])
                        cooldown = int(r["cooldown_minutes"] or 0)
                        last_state = r["last_state"]
                        last_trig = r["last_triggered_ts_utc"]

                        row = None
                        try:
                            row = get_or_fetch_metrics(cache, sym, con=con)
                        except Exception:
                            row = None
                        val = _extract_metric(row or {}, metric)
                        ok = _op_eval(op, val, thr)
                        if ok is None:
                            continue

                        prev = int(last_state) if last_state is not None else None
                        crossed = (prev == 0 and ok is True) if prev is not None else False

                        # Cooldown check.
                        allow = True
                        if last_trig and cooldown > 0:
                            try:
                                lt = datetime.fromisoformat(str(last_trig))
                                if lt.tzinfo is None:
                                    lt = lt.replace(tzinfo=timezone.utc)
                                dt_min = (now - lt.astimezone(timezone.utc)).total_seconds() / 60.0
                                if dt_min < float(cooldown):
                                    allow = False
                            except Exception:
                                allow = True

                        if crossed and allow:
                            insert_alert_event(
                                con,
                                user_id=uid,
                                rule_id=rid,
                                symbol=sym,
                                metric=metric,
                                op=op,
                                threshold=thr,
                                value=val,
                                meta={"provider": "yfinance", "fetched_ts_utc": (row or {}).get("fetched_ts_utc")},
                            )
                            update_rule_state(con, rule_id=rid, last_state=1, last_triggered_ts_utc=_utcnow_iso())
                        else:
                            update_rule_state(
                                con,
                                rule_id=rid,
                                last_state=1 if ok else 0,
                                last_triggered_ts_utc=str(last_trig) if last_trig else None,
                            )
                finally:
                    con.close()
            except Exception:
                # Never crash the background loop.
                pass

            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass

    router.state._stop_evt = asyncio.Event()
    router.state._task = None
    router.state._alert_loop = _alert_loop

    register_interesting_stocks_routes(router, db_path=db_path)

    return router

