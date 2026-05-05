from __future__ import annotations

import asyncio
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
    remove_from_watchlist,
    set_alert_rule_enabled,
    query_fundamental_points,
    query_latest_daily_metric_points,
    query_metric_points,
    query_yearly_metric_coverage,
    upsert_metric_points,
    update_rule_state,
)
from value_metrics_provider_fmp import fetch_ratios_history
from value_metrics_price_history import fetch_price_history


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        Fetch value metrics for a comma-separated symbol list.
        Cached in-memory to avoid hitting provider rate limits.
        """
        syms = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()]
        if not syms:
            raise HTTPException(status_code=400, detail="symbols is required")
        con = _con()
        try:
            out = [get_or_fetch_metrics(cache, s, con=con) for s in syms]
            return {"ts_utc": _utcnow_iso(), "n": len(out), "rows": out}
        finally:
            con.close()

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
    return router

