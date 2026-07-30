"""API for research agent memory snapshots and recommendations (agent.sqlite)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _agent_db_path() -> Path:
    p = Path(os.getenv("AGENT_DB_PATH", str(_REPO_ROOT / "telegram_agent" / "data" / "agent.sqlite"))).expanduser()
    if not p.is_absolute():
        p = (_REPO_ROOT / p).resolve()
    return p


def _parse_memory_row(row) -> Dict[str, Any]:
    from telegram_agent.memory_structured import parse_memory_payload

    text = row["text"] if "text" in row.keys() else ""
    meta_raw = row["meta_json"] if "meta_json" in row.keys() else None
    structured = parse_memory_payload(text or "", meta_raw)
    return {
        "id": int(row["id"]),
        "ts_utc": row["ts_utc"],
        "horizon_months": row["horizon_months"] if "horizon_months" in row.keys() else None,
        "text": text,
        "structured": structured,
        "meta_json": meta_raw,
    }


def _parse_recommendation_row(row) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except Exception:
        meta = {}
    return {
        "id": int(row["id"]),
        "ts_utc": row["ts_utc"],
        "symbol": row["symbol"],
        "duration": row["duration"] if "duration" in row.keys() else None,
        "forecast_pct": row["forecast_pct"] if "forecast_pct" in row.keys() else None,
        "forecast_usd": row["forecast_usd"] if "forecast_usd" in row.keys() else None,
        "confidence": row["confidence"] if "confidence" in row.keys() else None,
        "rationale": row["rationale"] if "rationale" in row.keys() else None,
        "suggestion_ts_utc": row["suggestion_ts_utc"] if "suggestion_ts_utc" in row.keys() else None,
        "entry_window_start_utc": row["entry_window_start_utc"] if "entry_window_start_utc" in row.keys() else None,
        "entry_window_end_utc": row["entry_window_end_utc"] if "entry_window_end_utc" in row.keys() else None,
        "execute_review_utc": row["execute_review_utc"] if "execute_review_utc" in row.keys() else None,
        "plan": meta.get("plan"),
        "tester": meta.get("tester"),
        "meta": meta,
    }


def build_agent_research_router() -> APIRouter:
    router = APIRouter(prefix="/agent/research", tags=["agent-research"])

    @router.get("/overview")
    async def overview(
        memory_limit: int = 20,
        rec_limit: int = 50,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        def _run() -> Dict[str, Any]:
            from telegram_agent.agent_db import connect, init_db

            db = _agent_db_path()
            if not db.exists():
                raise HTTPException(status_code=404, detail=f"agent DB not found: {db}")
            con = connect(db)
            init_db(con)
            try:
                mem_rows = con.execute(
                    """
                    SELECT id, ts_utc, horizon_months, text, meta_json
                    FROM memories
                    ORDER BY ts_utc DESC
                    LIMIT ?
                    """,
                    (max(1, min(200, int(memory_limit))),),
                ).fetchall()
                memories = [_parse_memory_row(r) for r in mem_rows]
                latest = memories[0] if memories else None

                where = ["1=1"]
                params: List[Any] = []
                if symbol and str(symbol).strip():
                    where.append("symbol = ?")
                    params.append(str(symbol).strip().upper())
                params.append(max(1, min(500, int(rec_limit))))
                # Columns may vary by migration; select * then normalize.
                rec_sql = f"""
                    SELECT *
                    FROM recommendations
                    WHERE {' AND '.join(where)}
                    ORDER BY ts_utc DESC
                    LIMIT ?
                """
                rec_rows = con.execute(rec_sql, params).fetchall()
                recommendations = [_parse_recommendation_row(r) for r in rec_rows]

                n_mem = con.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
                n_rec = con.execute("SELECT COUNT(*) AS c FROM recommendations").fetchone()["c"]
                return {
                    "db_path": str(db),
                    "counts": {"memories": int(n_mem), "recommendations": int(n_rec)},
                    "latest_memory": latest,
                    "memories": memories,
                    "recommendations": recommendations,
                }
            finally:
                con.close()

        return await run_in_threadpool(_run)

    @router.get("/memories")
    async def list_memories(limit: int = 20) -> Dict[str, Any]:
        data = await overview(memory_limit=limit, rec_limit=1)
        return {
            "n": len(data.get("memories") or []),
            "latest": data.get("latest_memory"),
            "rows": data.get("memories") or [],
            "counts": data.get("counts"),
        }

    @router.get("/recommendations")
    async def list_recommendations(limit: int = 50, symbol: Optional[str] = None) -> Dict[str, Any]:
        data = await overview(memory_limit=1, rec_limit=limit, symbol=symbol)
        return {
            "n": len(data.get("recommendations") or []),
            "rows": data.get("recommendations") or [],
            "counts": data.get("counts"),
        }

    return router
