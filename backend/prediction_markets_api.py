"""FastAPI routes for prediction-market signals."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from prediction_markets_store import connect, count_signals, init_prediction_markets_db, query_signals, query_sync_status
from prediction_markets_sync import sync_prediction_markets


def build_prediction_markets_router(*, db_path: Path) -> APIRouter:
    router = APIRouter(prefix="/prediction-markets", tags=["prediction-markets"])

    def _con():
        con = connect(db_path)
        init_prediction_markets_db(con)
        return con

    @router.get("/signals")
    async def list_signals(
        source: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        def _run() -> Dict[str, Any]:
            con = _con()
            try:
                rows = query_signals(con, source=source, status=status, limit=limit, offset=offset)
                total = count_signals(con)
                sync = query_sync_status(con)
                settled = [r for r in rows if r.get("status") == "settled" and not r.get("pending")]
                wins = sum(1 for r in settled if r.get("signal_won"))
                losses = sum(1 for r in settled if r.get("signal_won") is False)
                return {
                    "n": len(rows),
                    "total": total,
                    "rows": rows,
                    "sync": sync,
                    "aggregate": {
                        "settled_n": len(settled),
                        "wins": wins,
                        "losses": losses,
                        "win_rate": (wins / len(settled)) if settled else None,
                    },
                }
            finally:
                con.close()

        return await run_in_threadpool(_run)

    @router.post("/sync")
    async def run_sync(
        polymarket_limit: int = 400,
        kalshi_limit: int = 400,
        include_settled: bool = True,
    ) -> Dict[str, Any]:
        def _run() -> Dict[str, Any]:
            con = _con()
            try:
                return sync_prediction_markets(
                    con,
                    polymarket_limit=int(polymarket_limit),
                    kalshi_limit=int(kalshi_limit),
                    include_settled=bool(include_settled),
                )
            finally:
                con.close()

        return await run_in_threadpool(_run)

    @router.get("/sync/status")
    async def sync_status() -> Dict[str, Any]:
        def _run() -> Dict[str, Any]:
            con = _con()
            try:
                return {"sync": query_sync_status(con), "total_signals": count_signals(con)}
            finally:
                con.close()

        return await run_in_threadpool(_run)

    return router
