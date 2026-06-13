"""FastAPI routes for interesting stocks MVP."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from interesting_stocks_service import (
    coverage_for_symbol,
    default_universe_path,
    list_stocks_with_coverage,
    load_universe_priorities,
    seed_interesting_stocks_from_universe,
    ticker_detail,
)
from value_metrics_store import (
    add_interesting_stock,
    connect,
    init_db,
    list_interesting_stocks,
    remove_interesting_stock,
)


class InterestingStockAdd(BaseModel):
    symbol: str
    universe_priority: Optional[int] = None


def register_interesting_stocks_routes(router: APIRouter, *, db_path: Path) -> None:
    @router.get("/interesting/stocks")
    async def get_interesting_stocks(seed: bool = True) -> Dict[str, Any]:
        if seed:
            await run_in_threadpool(seed_interesting_stocks_from_universe, db_path)
        rows = await run_in_threadpool(list_stocks_with_coverage, db_path)
        return {"n": len(rows), "rows": rows, "universe_path": str(default_universe_path())}

    @router.post("/interesting/stocks/seed")
    async def post_seed_interesting_stocks(force: bool = False) -> Dict[str, Any]:
        n = await run_in_threadpool(
            seed_interesting_stocks_from_universe, db_path, force=force
        )
        return {"seeded": n, "universe_path": str(default_universe_path())}

    @router.post("/interesting/stocks")
    async def post_add_interesting_stock(body: InterestingStockAdd) -> Dict[str, Any]:
        sym = str(body.symbol or "").strip().upper()
        if not sym:
            raise HTTPException(status_code=400, detail="symbol is required")
        pr = body.universe_priority
        if pr is None:
            pr = load_universe_priorities().get(sym, 3)

        def _add():
            con = connect(db_path)
            init_db(con)
            try:
                add_interesting_stock(con, symbol=sym, universe_priority=int(pr))
                return list_interesting_stocks(con)
            finally:
                con.close()

        rows = await run_in_threadpool(_add)
        return {"symbol": sym, "universe_priority": int(pr), "n": len(rows)}

    @router.delete("/interesting/stocks/{symbol}")
    async def delete_interesting_stock(symbol: str) -> Dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise HTTPException(status_code=400, detail="symbol is required")

        def _rm():
            con = connect(db_path)
            init_db(con)
            try:
                remove_interesting_stock(con, sym)
            finally:
                con.close()

        await run_in_threadpool(_rm)
        return {"removed": sym}

    @router.get("/interesting/stocks/{symbol}/coverage")
    async def get_stock_coverage(symbol: str) -> Dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        return await run_in_threadpool(coverage_for_symbol, db_path, sym)

    @router.get("/interesting/stocks/{symbol}/detail")
    async def get_stock_detail(symbol: str) -> Dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise HTTPException(status_code=400, detail="symbol is required")
        try:
            return await run_in_threadpool(ticker_detail, db_path, sym)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/interesting/stocks/{symbol}/value-trading")
    async def get_value_trading(symbol: str, limit: int = 20) -> Dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise HTTPException(status_code=400, detail="symbol is required")

        def _fetch():
            from value_trading_agent import get_value_trading_history
            from value_metrics_store import latest_value_trading_assessment, connect, init_db

            rows = get_value_trading_history(db_path, sym, limit=max(1, min(int(limit), 100)))
            con = connect(db_path)
            init_db(con)
            try:
                latest = latest_value_trading_assessment(con, symbol=sym)
            finally:
                con.close()
            return {"symbol": sym, "latest": latest, "history": rows}

        return await run_in_threadpool(_fetch)
