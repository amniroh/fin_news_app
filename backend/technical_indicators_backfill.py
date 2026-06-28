#!/usr/bin/env python3
"""
Backfill daily technical indicators (EMA, MACD, ADX, RVOL) into vm_technical_indicators.

Reads OHLCV from agent.sqlite ``prices`` (interval=1d) and writes one row per trading day
per symbol.

Usage (repo root):

  .venv/bin/python backend/technical_indicators_backfill.py
  .venv/bin/python backend/technical_indicators_backfill.py --symbols AAPL,MSFT
  .venv/bin/python backend/technical_indicators_backfill.py --extend-only
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (_REPO_ROOT, _BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("technical_indicators_backfill")


def _load_env() -> None:
    for p in (_REPO_ROOT / ".env", _BACKEND / ".env", _REPO_ROOT / "telegram_agent" / ".env"):
        if p.is_file():
            load_dotenv(p, override=False)


def _agent_db_path() -> Path:
    raw = (os.getenv("AGENT_DB_PATH") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else _REPO_ROOT / p
    return _REPO_ROOT / "telegram_agent" / "data" / "agent.sqlite"


def _vm_db_path(arg: str) -> Path:
    if arg.strip():
        p = Path(arg).expanduser()
        return p if p.is_absolute() else _REPO_ROOT / p
    raw = (os.getenv("VALUE_METRICS_DB_PATH") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else _REPO_ROOT / p
    return _BACKEND / "data" / "value_metrics.sqlite"


def list_symbols_with_daily_prices(agent_con) -> List[str]:
    cur = agent_con.execute(
        "SELECT DISTINCT symbol FROM prices WHERE interval = '1d' ORDER BY symbol"
    )
    return [str(r[0]).strip().upper() for r in cur.fetchall() if r[0]]


def query_ohlcv_daily(
    agent_con,
    symbol: str,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    from telegram_agent.agent_db import _dedupe_daily_price_rows, _price_source_sql_clause

    sym = str(symbol).strip().upper()
    src_clause, src_params = _price_source_sql_clause(None)
    sql = """
        SELECT ts_utc, open, high, low, close, adj_close, volume
        FROM prices
        WHERE symbol = ? AND interval = '1d'
    """
    params: List[Any] = [sym]
    if start_date:
        sql += " AND SUBSTR(ts_utc, 1, 10) >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND SUBSTR(ts_utc, 1, 10) <= ?"
        params.append(end_date)
    sql += f"{src_clause} ORDER BY ts_utc ASC"
    params.extend(src_params)
    cur = agent_con.execute(sql, params)
    deduped = _dedupe_daily_price_rows(list(cur.fetchall()))
    out: List[Dict[str, Any]] = []
    for row in deduped:
        px = row["adj_close"] if row["adj_close"] is not None else row["close"]
        hi = row["high"] if row["high"] is not None else px
        lo = row["low"] if row["low"] is not None else px
        out.append(
            {
                "date": str(row["ts_utc"])[:10],
                "close": float(px) if px is not None else None,
                "high": float(hi) if hi is not None else None,
                "low": float(lo) if lo is not None else None,
                "volume": float(row["volume"]) if row["volume"] is not None else 0.0,
            }
        )
    return out


def backfill_symbol(
    vm_con,
    agent_con,
    symbol: str,
    *,
    start_s: Optional[str] = None,
    end_s: Optional[str] = None,
    provider: str = "yfinance",
) -> int:
    from technical_indicators import compute_technical_indicators, indicators_to_points, ohlcv_rows_to_frame
    from value_metrics_store import upsert_technical_indicators

    # Load full history for warm-up; filter output rows to [start_s, end_s].
    rows = query_ohlcv_daily(agent_con, symbol)
    if not rows:
        return 0
    frame = ohlcv_rows_to_frame(rows)
    indicators = compute_technical_indicators(frame)
    points = indicators_to_points(symbol, indicators, start_s=start_s, end_s=end_s)
    if not points:
        return 0
    return upsert_technical_indicators(vm_con, provider=provider, points=points)


def extend_recent_technical_indicators(
    vm_db: Path,
    agent_db: Path,
    *,
    symbols: Optional[Sequence[str]] = None,
    since_date: Optional[str] = None,
    provider: str = "yfinance",
) -> Dict[str, Any]:
    """Extend indicators from last stored asof_date + 1 through today."""
    from value_metrics_store import connect, init_db, list_interesting_stocks

    end_d = datetime.now(timezone.utc).date()
    end_s = end_d.isoformat()
    floor_d = date.fromisoformat(str(since_date)[:10]) if since_date else None

    vm_con = connect(vm_db)
    init_db(vm_con)
    agent_con = __import__("sqlite3").connect(str(agent_db))
    agent_con.row_factory = __import__("sqlite3").Row

    try:
        if symbols:
            sym_list = [str(s).strip().upper() for s in symbols if str(s).strip()]
        else:
            sym_list = [
                str(r["symbol"]).strip().upper() for r in list_interesting_stocks(vm_con) if r.get("symbol")
            ]
        if not sym_list:
            return {"skipped": True, "reason": "no symbols"}

        ph = ",".join("?" * len(sym_list))
        cur = vm_con.execute(
            f"""
            SELECT symbol, MAX(asof_date) AS dmax
            FROM vm_technical_indicators
            WHERE provider = ? AND symbol IN ({ph})
            GROUP BY symbol
            """,
            [provider] + sym_list,
        )
        last_map = {str(r["symbol"]): str(r["dmax"]) for r in cur.fetchall()}

        ok: List[str] = []
        skipped: List[str] = []
        failed: List[Dict[str, str]] = []
        n_rows = 0
        for sym in sym_list:
            last_s = last_map.get(sym)
            if last_s:
                start_d = date.fromisoformat(last_s[:10]) + timedelta(days=1)
            elif floor_d:
                start_d = floor_d
            else:
                start_d = end_d - timedelta(days=365)
            if floor_d and start_d < floor_d:
                start_d = floor_d
            if start_d > end_d:
                skipped.append(sym)
                continue
            try:
                n = backfill_symbol(
                    vm_con,
                    agent_con,
                    sym,
                    start_s=start_d.isoformat(),
                    end_s=end_s,
                    provider=provider,
                )
                n_rows += int(n)
                ok.append(sym)
            except Exception as e:
                failed.append({"symbol": sym, "error": str(e)})
            time.sleep(0.05)
    finally:
        vm_con.close()
        agent_con.close()

    return {
        "window_end": end_s,
        "since_date": since_date,
        "n_ok": len(ok),
        "n_skipped": len(skipped),
        "n_failed": len(failed),
        "rows_upserted": n_rows,
        "failed_sample": failed[:20],
    }


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser(description="Backfill EMA/MACD/ADX/RVOL into vm_technical_indicators")
    ap.add_argument("--db", default="", help="value_metrics.sqlite path")
    ap.add_argument("--agent-db", default="", help="agent.sqlite path")
    ap.add_argument("--symbols", default="", help="Comma-separated tickers (default: all with daily prices)")
    ap.add_argument("--sleep", type=float, default=0.05)
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument(
        "--extend-only",
        action="store_true",
        help="Only append rows after the last stored asof_date per symbol",
    )
    ap.add_argument("--since-date", default="", help="Minimum start date YYYY-MM-DD (extend mode)")
    args = ap.parse_args()

    vm_db = _vm_db_path(args.db)
    agent_db = _agent_db_path()
    if args.agent_db.strip():
        agent_db = Path(args.agent_db).expanduser()
        if not agent_db.is_absolute():
            agent_db = _REPO_ROOT / agent_db

    from value_metrics_store import connect, init_db

    if args.extend_only:
        out = extend_recent_technical_indicators(
            vm_db,
            agent_db,
            since_date=str(args.since_date).strip() or None,
        )
        print(out)
        return 1 if out.get("n_failed", 0) else 0

    vm_con = connect(vm_db)
    init_db(vm_con)
    agent_con = __import__("sqlite3").connect(str(agent_db))
    agent_con.row_factory = __import__("sqlite3").Row

    if args.symbols.strip():
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = list_symbols_with_daily_prices(agent_con)

    if int(args.max_symbols) > 0:
        symbols = symbols[: int(args.max_symbols)]

    logger.info("Backfilling technical indicators for %s symbol(s)", len(symbols))
    ok = failed = total_rows = 0
    for i, sym in enumerate(symbols):
        try:
            n = backfill_symbol(vm_con, agent_con, sym)
            total_rows += n
            ok += 1
        except Exception as e:
            failed += 1
            logger.warning("[%s] %s", sym, e)
        time.sleep(max(0.0, float(args.sleep)))
        if (i + 1) % 50 == 0:
            logger.info("Progress %s/%s symbols, rows=%s", i + 1, len(symbols), total_rows)

    vm_con.close()
    agent_con.close()
    logger.info("Done. ok=%s failed=%s rows=%s", ok, failed, total_rows)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
