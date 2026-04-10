"""
Evaluate stored strategy legs (`recommendations` rows) vs prices; write per-row `meta_json.tester`
and aggregate metrics to `kv_state` for research feedback.

Generic use: any strategy with the same schema (symbol, entry/suggestion timestamps,
execute_review, optional meta.source) can be inserted into `recommendations` — e.g.
`meta_json={"source": "manual"}` or `{"source": "external_api"}` — and tested the same way.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from telegram_agent.agent_db import (
    connect,
    init_db,
    list_recommendations,
    get_close_at_or_before,
    update_recommendation_meta,
    kv_get,
    kv_set,
    _parse_dt,
)
from telegram_agent.strategy_metrics import (
    TradeLeg,
    compute_aggregate_metrics,
    pick_optimization_value,
)

logger = logging.getLogger(__name__)

STRATEGY_TEST_KV_KEY = "strategy_test:aggregate_v1"


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
    *,
    asof: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    now = asof or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
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


def _metrics_enabled_set(cfg: dict) -> Set[str]:
    raw = cfg.get("test_metrics_enabled")
    if isinstance(raw, list) and raw:
        return {str(x).strip().lower() for x in raw if str(x).strip()}
    if isinstance(raw, str) and raw.strip():
        return {x.strip().lower() for x in raw.split(",") if x.strip()}
    return {
        "sharpe",
        "alpha",
        "max_drawdown",
        "oos_sharpe",
        "calmar",
        "significance",
    }


def load_strategy_test_aggregate(con) -> Optional[Dict[str, Any]]:
    """Latest aggregate metrics from the last `test-suggestions` run (JSON)."""
    raw = kv_get(con, STRATEGY_TEST_KV_KEY)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        return None


def run_suggestion_tests(
    cfg: dict,
    *,
    asof_utc: Optional[datetime] = None,
    concluded_only: bool = False,
) -> int:
    """
    For each recommendation (strategy leg), backtest from entry to min(now, execute_review).
    Updates meta_json.tester per row and stores aggregate metrics + optimization scalar in kv_state.
    """
    cfg = {**cfg}
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    asof = asof_utc or datetime.now(timezone.utc)
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc)
    asof = asof.astimezone(timezone.utc)
    recs = list_recommendations(con)
    n = 0
    legs: List[TradeLeg] = []

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
        if concluded_only and planned_exit >= asof:
            # Avoid future data leakage in backfills: only evaluate legs whose review window
            # has fully elapsed as-of the simulated runtime.
            continue

        realized = _realized_from_plan(con, sym, entry, planned_exit, asof=asof)
        block = {
            "evaluated_at": asof.isoformat(),
            "entry_ts": entry.isoformat(),
            "planned_execute_review_ts": planned_exit.isoformat(),
        }
        if realized:
            block.update(realized)
        meta["tester"] = block
        update_recommendation_meta(con, rid, meta)
        n += 1

        if realized and realized.get("realized_pct") is not None:
            ex_ts = _parse_iso_loose(str(realized.get("exit_ts") or ""))
            if ex_ts and ex_ts > entry:
                legs.append(
                    TradeLeg(
                        entry=entry,
                        exit=ex_ts,
                        symbol=sym,
                        realized_pct=float(realized["realized_pct"]),
                        leg_id=rid,
                    )
                )

    enabled = _metrics_enabled_set(cfg)
    bench = str(cfg.get("test_benchmark_symbol") or "SPY").strip().upper()
    rf = float(cfg.get("test_risk_free_annual", 0.04))
    oos = float(cfg.get("test_oos_split", 0.5))

    agg = compute_aggregate_metrics(
        con,
        legs,
        benchmark_symbol=bench,
        risk_free_annual=rf,
        oos_split=oos,
        enabled=enabled,
    )
    opt_key = str(cfg.get("test_optimization_metric") or "sharpe").strip().lower()
    opt_val = pick_optimization_value(agg, opt_key)
    agg["optimization_metric"] = opt_key
    agg["optimization_value"] = opt_val
    agg["evaluated_at"] = asof.isoformat()
    kv_set(con, STRATEGY_TEST_KV_KEY, json.dumps(agg, default=str))

    con.close()
    logger.info(
        "Tester evaluated %s leg(s); aggregate n_legs=%s optimization=%s=%s",
        n,
        agg.get("n_legs"),
        opt_key,
        opt_val,
    )
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


def print_strategy_aggregate(cfg: dict) -> None:
    """Print latest aggregate JSON from kv_state (same as used for research feedback)."""
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    agg = load_strategy_test_aggregate(con)
    con.close()
    if not agg:
        print("(no aggregate strategy metrics — run test-suggestions first)")
        return
    print(json.dumps(agg, indent=2, default=str))
