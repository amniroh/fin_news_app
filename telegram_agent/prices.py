"""Fetch OHLCV via yfinance and store in agent DB."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

from telegram_agent.agent_db import (
    connect,
    init_db,
    get_latest_price_ts,
    upsert_price_rows,
    list_mentioned_symbols,
)
from telegram_agent.symbol_universe import symbol_universe_set

logger = logging.getLogger(__name__)


def _yf_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if s.endswith("-USD") and not s.startswith("^"):
        # yfinance crypto: BTC-USD
        return s
    # yfinance uses dashes for share-class tickers (e.g. BRK.B -> BRK-B)
    if "." in s and len(s) <= 8:
        s = s.replace(".", "-")
    return s


def fetch_and_store_history(
    con,
    symbol: str,
    *,
    start: datetime,
    end: Optional[datetime] = None,
    interval: str = "1d",
) -> int:
    import yfinance as yf

    end = end or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    sym = _yf_symbol(symbol)
    try:
        t = yf.Ticker(sym)
        df = t.history(start=start.date(), end=(end + timedelta(days=1)).date(), interval="1d", auto_adjust=False)
    except Exception as e:
        logger.warning("yfinance failed for %s: %s", sym, e)
        return 0
    if df is None or df.empty:
        logger.info("No price rows for %s", sym)
        return 0

    rows: List[tuple] = []
    for idx, row in df.iterrows():
        ts = idx.to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        ts_iso = ts.replace(microsecond=0).isoformat()
        o = float(row["Open"]) if row.get("Open") == row.get("Open") else None
        h = float(row["High"]) if row.get("High") == row.get("High") else None
        l = float(row["Low"]) if row.get("Low") == row.get("Low") else None
        c = float(row["Close"]) if row.get("Close") == row.get("Close") else None
        adj = float(row["Adj Close"]) if "Adj Close" in row and row["Adj Close"] == row["Adj Close"] else None
        vol = float(row["Volume"]) if "Volume" in row and row["Volume"] == row["Volume"] else None
        if c is None:
            continue
        rows.append((ts_iso, o or c, h or c, l or c, c, adj, vol))

    return upsert_price_rows(con, symbol, rows, interval=interval)


def backfill_all_mentioned(cfg: dict, *, days: int = 400) -> None:
    """Fetch daily history for symbols.

    In fixed-universe mode, this fetches for the configured universe (top-1000).
    Otherwise it fetches for all symbols seen in `news_mentions`.
    """
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    uni = symbol_universe_set(cfg)
    use_universe = uni is not None
    syms = sorted(uni) if use_universe else list_mentioned_symbols(con)
    if not syms:
        if use_universe:
            logger.info(
                "No symbols to price (universe empty or failed to load). Check SYMBOL_UNIVERSE_PATH / JSON."
            )
        else:
            logger.info("No symbols in news_mentions; set SYMBOL_UNIVERSE_PATH or run extract after ingest.")
        con.close()
        return
    if use_universe:
        logger.info("Backfilling prices for %s universe symbols (%s days)", len(syms), days)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    for sym in syms:
        latest = get_latest_price_ts(con, sym)
        if latest:
            start_sym = max(start, latest - timedelta(days=7))
        else:
            start_sym = start
        n = fetch_and_store_history(con, sym, start=start_sym, end=end)
        logger.info("Prices %s: upserted %s rows", sym, n)
    con.close()


def incremental_prices(cfg: dict) -> None:
    """Update price history.

    In fixed-universe mode, it updates prices for the configured universe.
    Otherwise it updates for all symbols seen in `news_mentions`.
    """
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    end = datetime.now(timezone.utc)
    uni = symbol_universe_set(cfg)
    use_universe = uni is not None
    syms = sorted(uni) if use_universe else list_mentioned_symbols(con)
    if not syms:
        if use_universe:
            logger.info(
                "No symbols to price (universe empty or failed to load). Check SYMBOL_UNIVERSE_PATH / JSON."
            )
        else:
            logger.info("No symbols in news_mentions; set SYMBOL_UNIVERSE_PATH or run extract after ingest.")
        con.close()
        return
    if use_universe:
        logger.info("Incremental prices for %s universe symbols", len(syms))
    for sym in syms:
        latest = get_latest_price_ts(con, sym)
        start = (latest - timedelta(days=2)) if latest else end - timedelta(days=400)
        n = fetch_and_store_history(con, sym, start=start, end=end)
        if n:
            logger.info("Incremental %s: %s rows", sym, n)
    con.close()
