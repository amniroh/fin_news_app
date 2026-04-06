"""Evaluate stored recommendations (backtest from plan dates); write results into meta_json."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from telegram_agent.agent_db import (
    connect,
    init_db,
    list_recommendations,
    get_close_at_or_before,
    update_recommendation_meta,
    _parse_dt,
)

logger = logging.getLogger(__name__)


def _parse_iso_loose(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    t = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _entry_ts_for_rec(row: Any) -> Optional[datetime]:
    """Prefer end of entry window (assumed fill), then start, then suggestion date."""
    for key in ("entry_window_end_utc", "entry_window_start_utc", "suggestion_ts_utc"):
        try:
            v = row[key]
        except (KeyError, IndexError):
            v = None
        if v:
            return _parse_iso_loose(str(v))
    return _parse_dt(str(row["ts_utc"]))


def _exit_ts_for_rec(row: Any) -> Optional[datetime]:
    try:
        v = row["execute_review_utc"]
    except (KeyError, IndexError):
        v = None
    if v:
        return _parse_iso_loose(str(v))
    return None


def _realized_from_plan(
    con,
    symbol: str,
    entry: datetime,
    exit_: datetime,
) -> Optional[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    end = exit_ if exit_ < now else now
    if end <= entry:
        return {
            "entry_px": get_close_at_or_before(con, symbol, entry),
            "exit_px": None,
            "exit_ts": end.isoformat(),
            "realized_pct": None,
            "note": "evaluation_window_not_elapsed_or_invalid",
        }
    p0 = get_close_at_or_before(con, symbol, entry)
    p1 = get_close_at_or_before(con, symbol, end)
    if not p0 or not p1 or p0 <= 0:
        return None
    ret = (p1 - p0) / p0 * 100.0
    return {
        "entry_px": p0,
        "exit_px": p1,
        "exit_ts": end.isoformat(),
        "realized_pct": round(ret, 4),
        "horizon_days_effective": (end - entry).days,
    }


def run_suggestion_tests(cfg: dict) -> int:
    """
    For each recommendation, backtest from entry (plan) to min(now, execute_review).
    Updates meta_json.tester in place.
    """
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    recs = list_recommendations(con)
    n = 0
    for r in recs:
        rid = int(r["id"])
        sym = r["symbol"]
        meta: Dict[str, Any] = {}
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        if meta.get("tester", {}).get("skipped"):
            continue

        entry = _entry_ts_for_rec(r)
        if not entry:
            continue
        planned_exit = _exit_ts_for_rec(r)
        if not planned_exit:
            planned_exit = entry + timedelta(days=90)

        realized = _realized_from_plan(con, sym, entry, planned_exit)
        block = {
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "entry_ts": entry.isoformat(),
            "planned_execute_review_ts": planned_exit.isoformat(),
        }
        if realized:
            block.update(realized)
        meta["tester"] = block
        update_recommendation_meta(con, rid, meta)
        n += 1

    con.close()
    logger.info("Tester evaluated %s recommendation(s)", n)
    return n


def print_tester_summary(cfg: dict) -> None:
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    for r in list_recommendations(con):
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        t = meta.get("tester") or {}
        if not t:
            continue
        print(
            json.dumps(
                {
                    "id": r["id"],
                    "symbol": r["symbol"],
                    "tester": t,
                },
                default=str,
            )
        )
    con.close()
