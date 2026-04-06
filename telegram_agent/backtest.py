"""Evaluate past recommendations vs realized price paths in the DB."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram_agent.agent_db import connect, init_db, list_recommendations, get_close_at_or_before, _parse_dt

logger = logging.getLogger(__name__)

HORIZON_DAYS = {"short": 7, "mid": 90, "long": 365 * 7, "plan": 90}


def _parse_iso_row(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    t = str(val).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _entry_t0(r: Any) -> datetime:
    for key in ("entry_window_end_utc", "entry_window_start_utc", "suggestion_ts_utc"):
        v = r[key] if key in r.keys() else None
        if v:
            dt = _parse_iso_row(str(v))
            if dt:
                return dt
    return _parse_dt(str(r["ts_utc"]))


def _planned_horizon_days(r: Any) -> int:
    ex = r["execute_review_utc"] if "execute_review_utc" in r.keys() else None
    t_end = _parse_iso_row(str(ex)) if ex else None
    t0 = _entry_t0(r)
    if t_end and t0:
        d = (t_end - t0).days
        return max(1, d)
    dur = (r["duration"] or "mid").lower()
    return HORIZON_DAYS.get(dur, 90)


def _realized_return(
    con,
    symbol: str,
    t0: datetime,
    horizon_days: int,
    *,
    planned_exit: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    if planned_exit is not None:
        end = min(planned_exit, now)
    else:
        t1 = t0 + timedelta(days=horizon_days)
        end = t1 if t1 < now else now
    if end <= t0:
        return None
    p0 = get_close_at_or_before(con, symbol, t0)
    p1 = get_close_at_or_before(con, symbol, end)
    if not p0 or not p1 or p0 <= 0:
        return None
    ret = (p1 - p0) / p0 * 100.0
    out: Dict[str, Any] = {
        "entry_px": p0,
        "exit_px": p1,
        "exit_ts": end.isoformat(),
        "realized_pct": round(ret, 4),
    }
    if planned_exit is not None:
        out["planned_exit_ts"] = planned_exit.isoformat()
    else:
        out["horizon_days_requested"] = horizon_days
    return out


def run_backtest(cfg: dict) -> List[Dict[str, Any]]:
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    recs = list_recommendations(con)
    results: List[Dict[str, Any]] = []
    for r in recs:
        sym = r["symbol"]
        dur = (r["duration"] or "mid").lower()
        try:
            t0 = _entry_t0(r)
        except Exception:
            continue
        days = _planned_horizon_days(r)
        ex = None
        if "execute_review_utc" in r.keys() and r["execute_review_utc"]:
            ex = _parse_iso_row(str(r["execute_review_utc"]))
        realized = _realized_return(con, sym, t0, days, planned_exit=ex)
        row = {
            "id": r["id"],
            "symbol": sym,
            "duration": dur,
            "ts": r["ts_utc"],
            "forecast_pct": r["forecast_pct"],
            "confidence": r["confidence"],
            "realized": realized,
        }
        results.append(row)
        if realized:
            fc = r["forecast_pct"]
            if fc is not None:
                row["forecast_error_pct"] = round(realized["realized_pct"] - float(fc), 4)
    con.close()
    return results


def print_backtest_report(cfg: dict) -> None:
    rows = run_backtest(cfg)
    ok = [r for r in rows if r.get("realized")]
    logger.info("Backtest: %s recommendations, %s with price path", len(rows), len(ok))
    for r in rows:
        print(json.dumps(r, default=str))
