"""Evaluate past recommendations vs realized price paths in the DB."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram_agent.agent_db import connect, init_db, list_recommendations, get_close_at_or_before, _parse_dt

logger = logging.getLogger(__name__)

HORIZON_DAYS = {"short": 7, "mid": 90, "long": 365 * 7}


def _realized_return(
    con, symbol: str, t0: datetime, horizon_days: int
) -> Optional[Dict[str, Any]]:
    t1 = t0 + timedelta(days=horizon_days)
    now = datetime.now(timezone.utc)
    end = t1 if t1 < now else now
    p0 = get_close_at_or_before(con, symbol, t0)
    p1 = get_close_at_or_before(con, symbol, end)
    if not p0 or not p1 or p0 <= 0:
        return None
    ret = (p1 - p0) / p0 * 100.0
    return {
        "entry_px": p0,
        "exit_px": p1,
        "exit_ts": end.isoformat(),
        "realized_pct": round(ret, 4),
        "horizon_days_requested": horizon_days,
    }


def run_backtest(cfg: dict) -> List[Dict[str, Any]]:
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    recs = list_recommendations(con)
    results: List[Dict[str, Any]] = []
    for r in recs:
        sym = r["symbol"]
        dur = (r["duration"] or "mid").lower()
        days = HORIZON_DAYS.get(dur, 90)
        try:
            t0 = _parse_dt(r["ts_utc"])
        except Exception:
            continue
        realized = _realized_return(con, sym, t0, days)
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
