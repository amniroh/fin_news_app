"""Daily orchestrator: ingest → prices → preprocess → test concluded legs → research.

Designed for accurate backfills: each simulated day uses a deterministic `current_runtime`
and ensures the tester only evaluates concluded suggestions as-of that time (no future leakage).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from telegram_agent.agent_db import connect, init_db
from telegram_agent.agent_research import ResearchRunContext, _run_research_once
from telegram_agent.agent_tester import run_suggestion_tests
from telegram_agent.ingest import run_ingest
from telegram_agent.news_universe_preprocess import run_news_universe_preprocess
from telegram_agent.prices import incremental_prices

logger = logging.getLogger(__name__)


def _utc_day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _end_of_utc_day(day_start: datetime) -> datetime:
    return day_start + timedelta(days=1) - timedelta(seconds=1)


def _has_any_news_for_utc_day(con, *, day_start_utc: datetime) -> bool:
    if day_start_utc.tzinfo is None:
        day_start_utc = day_start_utc.replace(tzinfo=timezone.utc)
    day_start_utc = day_start_utc.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_excl = day_start_utc + timedelta(days=1)
    cur = con.execute(
        "SELECT 1 FROM news_items WHERE ts_utc >= ? AND ts_utc < ? LIMIT 1",
        (day_start_utc.isoformat(), end_excl.isoformat()),
    )
    return cur.fetchone() is not None


def _has_any_prices_for_utc_day(con, *, day_start_utc: datetime, interval: str = "1d") -> bool:
    if day_start_utc.tzinfo is None:
        day_start_utc = day_start_utc.replace(tzinfo=timezone.utc)
    day_start_utc = day_start_utc.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_excl = day_start_utc + timedelta(days=1)
    cur = con.execute(
        "SELECT 1 FROM prices WHERE interval = ? AND ts_utc >= ? AND ts_utc < ? LIMIT 1",
        (interval, day_start_utc.isoformat(), end_excl.isoformat()),
    )
    return cur.fetchone() is not None


def _has_any_memory_for_utc_day(con, *, day_start_utc: datetime) -> bool:
    if day_start_utc.tzinfo is None:
        day_start_utc = day_start_utc.replace(tzinfo=timezone.utc)
    day_start_utc = day_start_utc.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_excl = day_start_utc + timedelta(days=1)
    cur = con.execute(
        "SELECT 1 FROM memories WHERE ts_utc >= ? AND ts_utc < ? LIMIT 1",
        (day_start_utc.isoformat(), end_excl.isoformat()),
    )
    return cur.fetchone() is not None


@dataclass
class OrchestratorResult:
    day: str
    current_runtime_utc: str
    ingest_rows: int
    preprocess: Dict[str, Any]
    tester_updated: int
    research_new_recs: int


async def run_orchestration_live(cfg: dict) -> OrchestratorResult:
    """Run the full pipeline using wall-clock now for incremental ingest/prices/research."""
    now = datetime.now(timezone.utc)
    day = now.date()
    day_start = _utc_day_start(day)

    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)

    # 1) ingest + prices: if *any* data exists for today, skip fetching to avoid duplication/leakage.
    ingest_n = 0
    if not _has_any_news_for_utc_day(con, day_start_utc=day_start):
        logger.info("Orchestrator: ingest START (no news present for %s UTC)", day.isoformat())
        ingest_n = await run_ingest(cfg, mode="incremental", source_mode=cfg.get("source_mode"))
        logger.info("Orchestrator: ingest DONE (upserted=%s)", ingest_n)
    else:
        logger.info("Orchestrator: ingest SKIP (news already present for %s UTC)", day.isoformat())

    if not _has_any_prices_for_utc_day(con, day_start_utc=day_start, interval="1d"):
        logger.info("Orchestrator: prices START (no 1d bars present for %s UTC)", day.isoformat())
        incremental_prices(cfg)
        logger.info("Orchestrator: prices DONE")
    else:
        logger.info("Orchestrator: prices SKIP (1d bars already present for %s UTC)", day.isoformat())

    # 2) preprocess pending news up to now
    logger.info("Orchestrator: preprocess START (pending news -> linkage; max_ts=%s)", now.isoformat())
    preprocess_out = run_news_universe_preprocess(cfg, con, max_ts_utc_inclusive=now)
    if preprocess_out.get("skipped"):
        logger.info("Orchestrator: preprocess SKIP (reason=%s)", preprocess_out.get("reason"))
    else:
        logger.info(
            "Orchestrator: preprocess DONE (processed=%s batches=%s)",
            preprocess_out.get("processed"),
            preprocess_out.get("batches"),
        )
    # 3) tester: concluded-only as-of now
    logger.info("Orchestrator: tester START (concluded_only=True asof=%s)", now.isoformat())
    tester_n = run_suggestion_tests(cfg, asof_utc=now, concluded_only=True)
    logger.info("Orchestrator: tester DONE (updated=%s)", tester_n)
    # 4) research
    recs = 0
    if _has_any_memory_for_utc_day(con, day_start_utc=day_start):
        logger.info("Orchestrator: research SKIP (memory already present for %s UTC)", day.isoformat())
    else:
        logger.info("Orchestrator: research START (no memory present for %s UTC)", day.isoformat())
        ctx = ResearchRunContext(sim_now=now, daily_mode=False)
        recs = _run_research_once(cfg, con, ctx)
        logger.info("Orchestrator: research DONE (new_recommendations=%s)", recs)
    con.close()
    return OrchestratorResult(
        day=day.isoformat(),
        current_runtime_utc=now.isoformat(),
        ingest_rows=int(ingest_n or 0),
        preprocess=preprocess_out,
        tester_updated=int(tester_n or 0),
        research_new_recs=int(recs or 0),
    )


def run_orchestration_backfill_day(cfg: dict, *, day: date) -> OrchestratorResult:
    """
    Run the daily pipeline for a single historical UTC calendar day.

    Key rule: `current_runtime` is end-of-day UTC for that day. Tester only evaluates legs
    whose execute_review_utc < current_runtime.
    """
    day_start = _utc_day_start(day)
    current_runtime = _end_of_utc_day(day_start)

    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)

    # Step 1: ingest/prices for the day are assumed to have been backfilled already.
    # If any data is present for this day, skip fetching and move on.
    ingest_n = 0
    if not _has_any_news_for_utc_day(con, day_start_utc=day_start):
        logger.info(
            "Orchestrator(backfill %s): ingest SKIP (no news present; backfill mode does not fetch day-level news to avoid future leakage)",
            day.isoformat(),
        )
    else:
        logger.info("Orchestrator(backfill %s): ingest SKIP (news present for day)", day.isoformat())
    if not _has_any_prices_for_utc_day(con, day_start_utc=day_start, interval="1d"):
        logger.info(
            "Orchestrator(backfill %s): prices SKIP (no 1d bars present; backfill mode does not fetch day-level prices to avoid future leakage)",
            day.isoformat(),
        )
    else:
        logger.info("Orchestrator(backfill %s): prices SKIP (1d bars present for day)", day.isoformat())

    # Step 2: preprocess only pending news with ts_utc <= current_runtime
    logger.info(
        "Orchestrator(backfill %s): preprocess START (max_ts_utc_inclusive=%s)",
        day.isoformat(),
        current_runtime.isoformat(),
    )
    preprocess_out = run_news_universe_preprocess(cfg, con, max_ts_utc_inclusive=current_runtime)
    if preprocess_out.get("skipped"):
        logger.info(
            "Orchestrator(backfill %s): preprocess SKIP (reason=%s)",
            day.isoformat(),
            preprocess_out.get("reason"),
        )
    else:
        logger.info(
            "Orchestrator(backfill %s): preprocess DONE (processed=%s batches=%s)",
            day.isoformat(),
            preprocess_out.get("processed"),
            preprocess_out.get("batches"),
        )

    # Step 3: tester as-of current_runtime, concluded legs only
    logger.info(
        "Orchestrator(backfill %s): tester START (concluded_only=True asof=%s)",
        day.isoformat(),
        current_runtime.isoformat(),
    )
    tester_n = run_suggestion_tests(cfg, asof_utc=current_runtime, concluded_only=True)
    logger.info("Orchestrator(backfill %s): tester DONE (updated=%s)", day.isoformat(), tester_n)

    # Step 4: research for this day (daily-mode prompt window)
    recs = 0
    if _has_any_memory_for_utc_day(con, day_start_utc=day_start):
        logger.info(
            "Orchestrator(backfill %s): research SKIP (memory already present for day)",
            day.isoformat(),
        )
    else:
        logger.info("Orchestrator(backfill %s): research START (no memory present for day)", day.isoformat())
        ctx = ResearchRunContext(sim_now=current_runtime, daily_mode=True, day_start_utc=day_start)
        recs = _run_research_once(cfg, con, ctx)
        logger.info("Orchestrator(backfill %s): research DONE (new_recommendations=%s)", day.isoformat(), recs)
    con.close()

    return OrchestratorResult(
        day=day.isoformat(),
        current_runtime_utc=current_runtime.isoformat(),
        ingest_rows=int(ingest_n),
        preprocess=preprocess_out,
        tester_updated=int(tester_n or 0),
        research_new_recs=int(recs or 0),
    )


def run_orchestration_backfill(
    cfg: dict, *, start: date, end: date, cadence: int = 1
) -> Dict[str, Any]:
    """
    Backfill orchestration in chronological order.

    With ``cadence=1`` (default), runs every calendar day from ``start`` through ``end`` inclusive.
    With ``cadence=n`` (n >= 1), runs only on ``start``, ``start + n days``, ``start + 2n days``, ...
    while each date is <= ``end`` (both bounds inclusive when they land on a run day).
    """
    if end < start:
        raise ValueError("end must be >= start")
    if cadence < 1:
        raise ValueError("cadence must be >= 1")
    days = 0
    total_recs = 0
    total_tested = 0
    d = start
    per_day: list[dict] = []
    while d <= end:
        out = run_orchestration_backfill_day(cfg, day=d)
        per_day.append(out.__dict__)
        days += 1
        total_recs += int(out.research_new_recs or 0)
        total_tested += int(out.tester_updated or 0)
        d = d + timedelta(days=cadence)
    return {
        "cadence": cadence,
        "days": days,
        "recommendations": total_recs,
        "tester_updated": total_tested,
        "per_day": per_day,
    }

