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


def _meta_dict(meta_raw: Any) -> Dict[str, Any]:
    if isinstance(meta_raw, dict):
        return meta_raw
    if not meta_raw:
        return {}
    try:
        out = json.loads(meta_raw)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def _parse_memory_row(row) -> Dict[str, Any]:
    from telegram_agent.memory_structured import parse_memory_payload

    text = row["text"] if "text" in row.keys() else ""
    meta_raw = row["meta_json"] if "meta_json" in row.keys() else None
    meta = _meta_dict(meta_raw)
    structured = parse_memory_payload(text or "", meta_raw)
    return {
        "id": int(row["id"]),
        "ts_utc": row["ts_utc"],
        "horizon_months": row["horizon_months"] if "horizon_months" in row.keys() else None,
        "text": text,
        "structured": structured,
        "meta_json": meta_raw,
        "model": meta.get("model"),
        "signal_insights": meta.get("signal_insights") if isinstance(meta.get("signal_insights"), dict) else {},
    }


def _parse_recommendation_row(row) -> Dict[str, Any]:
    meta = _meta_dict(row["meta_json"] if "meta_json" in row.keys() else None)
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
        "model": meta.get("model"),
        "meta": meta,
    }


def _parse_internal_log_row(row) -> Dict[str, Any]:
    payload = _meta_dict(row["payload_json"] if "payload_json" in row.keys() else None)
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    return {
        "id": int(row["id"]),
        "ts_utc": row["ts_utc"],
        "category": row["category"],
        "model": row["model"] if "model" in row.keys() else None,
        "source_run_ts_utc": row["source_run_ts_utc"] if "source_run_ts_utc" in row.keys() else None,
        "payload": payload,
        "issues": issues,
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

    @router.get("/internal-logs")
    async def internal_logs(limit: int = 100, category: Optional[str] = None) -> Dict[str, Any]:
        def _run() -> Dict[str, Any]:
            from telegram_agent.agent_db import connect, init_db, list_research_internal_logs

            db = _agent_db_path()
            if not db.exists():
                raise HTTPException(status_code=404, detail=f"agent DB not found: {db}")
            con = connect(db)
            init_db(con)
            try:
                rows = list_research_internal_logs(
                    con,
                    limit=max(1, min(500, int(limit))),
                    category=category,
                )
                parsed = [_parse_internal_log_row(r) for r in rows]
                n = con.execute("SELECT COUNT(*) AS c FROM research_internal_logs").fetchone()["c"]
                return {
                    "n": len(parsed),
                    "total": int(n),
                    "rows": parsed,
                }
            finally:
                con.close()

        return await run_in_threadpool(_run)

    return router
