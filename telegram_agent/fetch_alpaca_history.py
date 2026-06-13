#!/usr/bin/env python3
"""
Fetch historical stock bars from Alpaca Data API v2 and upsert into SQLite:

- **Hourly** → ``prices_hourly`` (timeframe ``1Hour``)
- **Daily** → ``prices`` with ``interval='1d'`` (timeframe ``1Day``)

Uses the same credentials as ``rsi_alpaca_live`` (``APCA_API_KEY_ID``, ``APCA_API_SECRET_KEY``)
and the same client. Inserts use ``source='alpaca'`` and ``ON CONFLICT DO NOT UPDATE`` so
existing rows (e.g. yfinance) are kept.

By default, each symbol only requests **head** and **tail** gaps versus
``MIN(ts_utc)`` / ``MAX(ts_utc)`` already in SQLite (hourly vs daily tables).
Use ``--force-full-range`` to always refetch the whole window. Alpaca calls
use pagination pacing and 429 backoff (see ``AlpacaRest.get_stock_bars``).

**Coverage:** IEX feed (default) has limited history; for deep backfill use SIP (``ALPACA_DATA_FEED=sip``)
if your Alpaca subscription includes it.

Examples::

    python -m telegram_agent.fetch_alpaca_history --symbols AAPL,MSFT --start 2016-01-01 --dry-run
    python -m telegram_agent.fetch_alpaca_history --symbols-min-daily-after 2024-01-01 --start 2016-01-01
    python -m telegram_agent.fetch_alpaca_history --start 2016-01-01 --max-symbols 20
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telegram_agent.agent_db import (
    _utc_iso,
    connect,
    delete_alpaca_daily_prices,
    init_db,
    upsert_intraday_rows,
    upsert_price_rows,
)
from telegram_agent.config import load_config
from telegram_agent.derive_hourly_daily_from_5m import _load_symbols_by_max_priority
from telegram_agent.rsi_alpaca_live import AlpacaRest
from telegram_agent.symbol_universe import crypto_symbols_from_universe, normalize_symbol, sp500_symbols_from_env

logger = logging.getLogger(__name__)

SOURCE = "alpaca"


def _load_dotenv_files() -> None:
    """Load repo and package ``.env`` into the process (same pattern as ``rsi_alpaca_live``)."""
    root = Path(__file__).resolve().parent.parent
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass


def _parse_alpaca_bar_time(t: str) -> datetime:
    if not t:
        raise ValueError("missing bar time")
    s = str(t).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bars_to_ohlc_rows(bars: Sequence[Dict[str, Any]]) -> List[Tuple[str, float, float, float, float, Optional[float], Optional[float]]]:
    rows: List[Tuple[str, float, float, float, float, Optional[float], Optional[float]]] = []
    for b in bars:
        try:
            ts = _parse_alpaca_bar_time(str(b.get("t") or ""))
            o = float(b["o"])
            h = float(b["h"])
            l = float(b["l"])
            c = float(b["c"])
        except (KeyError, TypeError, ValueError) as e:
            logger.debug("skip bad bar %s: %s", b, e)
            continue
        vol = float(b["v"]) if b.get("v") is not None else None
        rows.append((_utc_iso(ts), o, h, l, c, None, vol))
    return rows


def _merge_bars_by_time(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_t: Dict[str, Dict[str, Any]] = {}
    for b in bars:
        t = b.get("t")
        if t:
            by_t[str(t)] = b
    return [by_t[k] for k in sorted(by_t.keys())]


def _parse_db_ts(ts: str) -> datetime:
    s = str(ts).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _symbol_ts_bounds_hourly(con: sqlite3.Connection, symbol: str) -> Optional[Tuple[datetime, datetime]]:
    row = con.execute(
        "SELECT MIN(ts_utc) AS mn, MAX(ts_utc) AS mx FROM prices_hourly WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    if not row or row["mn"] is None:
        return None
    return _parse_db_ts(str(row["mn"])), _parse_db_ts(str(row["mx"]))


def _symbol_ts_bounds_daily_1d(con: sqlite3.Connection, symbol: str) -> Optional[Tuple[datetime, datetime]]:
    row = con.execute(
        """
        SELECT MIN(ts_utc) AS mn, MAX(ts_utc) AS mx
        FROM prices
        WHERE symbol = ? AND interval = '1d'
        """,
        (symbol,),
    ).fetchone()
    if not row or row["mn"] is None:
        return None
    return _parse_db_ts(str(row["mn"])), _parse_db_ts(str(row["mx"]))


def _tail_start_after_last_bar(last: datetime, timeframe: str) -> datetime:
    """First instant after ``last`` bar where the next bar of this timeframe may begin."""
    last = last.astimezone(timezone.utc)
    if timeframe == "1Day":
        return datetime.combine(last.date(), datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)
    return last + timedelta(hours=1)


def missing_bar_ranges(
    start: datetime,
    end: datetime,
    timeframe: str,
    bounds: Optional[Tuple[datetime, datetime]],
) -> List[Tuple[datetime, datetime]]:
    """Return UTC [lo, hi) windows that need fetching (head before first row, tail after last row)."""
    if start >= end:
        return []
    if bounds is None:
        return [(start, end)]
    mn, mx = bounds
    mn = mn.astimezone(timezone.utc)
    mx = mx.astimezone(timezone.utc)
    out: List[Tuple[datetime, datetime]] = []
    if mn > start:
        he = min(mn, end)
        if start < he:
            out.append((start, he))
    tail_from = _tail_start_after_last_bar(mx, timeframe)
    if tail_from < end:
        out.append((tail_from, end))
    return [(lo, hi) for lo, hi in out if lo < hi]


def _iter_time_chunks(
    start: datetime, end: datetime, *, max_days: int
) -> List[Tuple[datetime, datetime]]:
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    out: List[Tuple[datetime, datetime]] = []
    cur = start
    step = timedelta(days=max(1, int(max_days)))
    while cur < end:
        nxt = min(cur + step, end)
        out.append((cur, nxt))
        cur = nxt
    return out


def fetch_bars_chunked(
    client: AlpacaRest,
    symbol: str,
    *,
    ranges: Sequence[Tuple[datetime, datetime]],
    timeframe: str,
    feed: str,
    chunk_days: int,
    page_delay_sec: float,
    sleep_between_chunks: float,
) -> List[Dict[str, Any]]:
    """Paginated Alpaca bars over one or more UTC windows (split into calendar chunks)."""
    all_bars: List[Dict[str, Any]] = []
    first_chunk = True
    for win_start, win_end in ranges:
        if win_start >= win_end:
            continue
        for a, b in _iter_time_chunks(win_start, win_end, max_days=chunk_days):
            if not first_chunk and sleep_between_chunks > 0:
                time.sleep(float(sleep_between_chunks))
            first_chunk = False
            try:
                m = client.get_stock_bars(
                    [symbol],
                    start=a,
                    end=b,
                    timeframe=timeframe,
                    feed=feed,
                    page_delay_sec=float(page_delay_sec),
                )
            except Exception as e:
                logger.warning("%s %s %s..%s: %s", symbol, timeframe, a.date(), b.date(), e)
                continue
            bars = m.get(symbol) or []
            all_bars.extend(bars)
    return _merge_bars_by_time(all_bars)


def symbols_min_daily_after(con, cutoff_iso: str) -> List[str]:
    """Symbols whose earliest **daily** row in ``prices`` is on/after ``cutoff_iso`` (ISO date prefix)."""
    cur = con.execute(
        """
        SELECT symbol
        FROM prices
        WHERE interval = '1d'
        GROUP BY symbol
        HAVING MIN(ts_utc) >= ?
        ORDER BY symbol
        """,
        (cutoff_iso,),
    )
    return [str(r["symbol"]) for r in cur.fetchall()]


def _load_symbol_list(
    cfg: dict,
    args: argparse.Namespace,
    con,
) -> List[str]:
    if args.symbols.strip():
        return sorted({normalize_symbol(x) for x in args.symbols.split(",") if x.strip()})
    if bool(getattr(args, "spy_symbols", False)):
        return sp500_symbols_from_env()
    if args.symbols_min_daily_after.strip():
        syms = symbols_min_daily_after(con, args.symbols_min_daily_after.strip())
        logger.info("symbols_min_daily_after=%s -> %s tickers", args.symbols_min_daily_after, len(syms))
        return syms
    syms = _load_symbols_by_max_priority(
        cfg,
        max_priority=int(args.max_priority),
        universe_path=args.universe_path,
    )
    if syms:
        return syms
    print(
        "No symbols: pass --symbols, --spy_symbols, --symbols-min-daily-after, or set SYMBOL_UNIVERSE_PATH "
        "(universe file works even if SYMBOL_UNIVERSE_ENABLED=false).",
        file=sys.stderr,
    )
    return []


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="Backfill hourly/daily prices from Alpaca Data API v2.")
    p.add_argument("--symbols", type=str, default="", help="Comma-separated tickers")
    p.add_argument(
        "--spy_symbols",
        action="store_true",
        help="Use SP500_SYMBOLS from repo .env as the symbol list (overrides universe/min-daily filters).",
    )
    p.add_argument(
        "--symbols-min-daily-after",
        type=str,
        default="",
        help="Only symbols whose MIN(daily ts_utc) in DB is >= this (e.g. 2024-01-01).",
    )
    p.add_argument(
        "--max-priority",
        type=int,
        default=1,
        help="With universe: priority <= N (default 1). Ignored if --symbols set.",
    )
    p.add_argument(
        "--universe-path",
        type=Path,
        default=None,
        help="JSON universe file (optional; same as derive_hourly_daily_from_5m).",
    )
    p.add_argument(
        "--start",
        type=str,
        default="2016-01-01",
        help="Fetch start (UTC date or ISO), default 2016-01-01",
    )
    p.add_argument(
        "--end",
        type=str,
        default="",
        help="Fetch end (UTC); default = now",
    )
    p.add_argument(
        "--hourly-chunk-days",
        type=int,
        default=120,
        help="Split hourly requests into windows of N days (Alpaca paginates 10k bars per call).",
    )
    p.add_argument(
        "--daily-chunk-days",
        type=int,
        default=4000,
        help="Split daily requests (rarely needed; set lower if you hit limits).",
    )
    p.add_argument("--no-hourly", action="store_true", help="Skip hourly backfill")
    p.add_argument("--no-daily", action="store_true", help="Skip daily backfill")
    p.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.5,
        help="Pause between symbols (rate limits).",
    )
    p.add_argument(
        "--request-page-delay",
        type=float,
        default=0.15,
        help="Pause between Alpaca paginated page fetches (seconds).",
    )
    p.add_argument(
        "--chunk-request-sleep",
        type=float,
        default=0.12,
        help="Extra pause between calendar-chunk HTTP calls (seconds).",
    )
    p.add_argument(
        "--force-full-range",
        action="store_true",
        help="Ignore DB MIN/MAX coverage; request the full [--start, --end] window every time.",
    )
    p.add_argument("--max-symbols", type=int, default=0, help="Process at most N symbols (0=all)")
    p.add_argument("--dry-run", action="store_true", help="Fetch and count only; no DB writes")
    args = p.parse_args(list(argv) if argv is not None else None)

    _load_dotenv_files()
    cfg = load_config()
    key = (cfg.get("alpaca_api_key_id") or "").strip()
    secret = (cfg.get("alpaca_api_secret_key") or "").strip()
    if not key or not secret:
        print(
            "Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY (or ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY). "
            "Set them in market_analysis/.env (repo root) or telegram_agent/.env, then re-run. "
            "This script loads those files automatically; export in the shell is optional.",
            file=sys.stderr,
        )
        return 2

    feed = (cfg.get("alpaca_data_feed") or "iex").strip().lower()
    paper = bool(cfg.get("alpaca_paper", True))

    end = datetime.now(timezone.utc)
    if args.end.strip():
        raw = args.end.strip()
        if len(raw) == 10 and raw[4] == "-":
            end = datetime.combine(
                datetime.strptime(raw, "%Y-%m-%d").date(),
                datetime.max.time(),
                tzinfo=timezone.utc,
            )
        else:
            s = raw.replace("Z", "+00:00")
            end = datetime.fromisoformat(s)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            end = end.astimezone(timezone.utc)

    raw_s = args.start.strip() or "2016-01-01"
    if len(raw_s) == 10 and raw_s[4] == "-":
        start = datetime.combine(
            datetime.strptime(raw_s, "%Y-%m-%d").date(),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
    else:
        s = raw_s.replace("Z", "+00:00")
        start = datetime.fromisoformat(s)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        start = start.astimezone(timezone.utc)

    if start >= end:
        print("start must be before end", file=sys.stderr)
        return 2

    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    try:
        crypto_skip = crypto_symbols_from_universe()
        if crypto_skip:
            removed = delete_alpaca_daily_prices(con, sorted(crypto_skip))
            if removed:
                logger.info(
                    "Removed %s mistaken Alpaca daily bar(s) for crypto symbols (ticker collision with equities)",
                    removed,
                )
    finally:
        con.close()

    con = connect(db)
    init_db(con)
    try:
        syms = _load_symbol_list(cfg, args, con)
    finally:
        con.close()

    crypto_skip = crypto_symbols_from_universe()
    if crypto_skip:
        before = len(syms)
        syms = [s for s in syms if s not in crypto_skip]
        skipped = before - len(syms)
        if skipped:
            logger.info("Skipping %s crypto symbol(s) for Alpaca stock bars (use yfinance for crypto)", skipped)

    if not syms:
        return 2
    if args.max_symbols and args.max_symbols > 0:
        syms = syms[: int(args.max_symbols)]

    client = AlpacaRest(key, secret, paper=paper)
    logger.info(
        "Alpaca feed=%s paper=%s window=%s .. %s symbols=%s",
        feed,
        paper,
        start.date(),
        end.date(),
        len(syms),
    )

    con = connect(db)
    init_db(con)
    try:
        page_delay = float(args.request_page_delay)
        chunk_sleep = float(args.chunk_request_sleep)
        for sym in syms:
            if args.force_full_range:
                ranges_h = [(start, end)] if not args.no_hourly else []
                ranges_d = [(start, end)] if not args.no_daily else []
            else:
                hb = None if args.no_hourly else _symbol_ts_bounds_hourly(con, sym)
                db1 = None if args.no_daily else _symbol_ts_bounds_daily_1d(con, sym)
                ranges_h = [] if args.no_hourly else missing_bar_ranges(start, end, "1Hour", hb)
                ranges_d = [] if args.no_daily else missing_bar_ranges(start, end, "1Day", db1)

            if args.dry_run:
                n_h = n_d = 0
                if not args.no_hourly:
                    if ranges_h:
                        bars_h = fetch_bars_chunked(
                            client,
                            sym,
                            ranges=ranges_h,
                            timeframe="1Hour",
                            feed=feed,
                            chunk_days=max(7, int(args.hourly_chunk_days)),
                            page_delay_sec=page_delay,
                            sleep_between_chunks=chunk_sleep,
                        )
                        n_h = len(_bars_to_ohlc_rows(bars_h))
                    else:
                        logger.info("%s hourly: dry-run skip (DB already covers range)", sym)
                if not args.no_daily:
                    if ranges_d:
                        bars_d = fetch_bars_chunked(
                            client,
                            sym,
                            ranges=ranges_d,
                            timeframe="1Day",
                            feed=feed,
                            chunk_days=max(30, int(args.daily_chunk_days)),
                            page_delay_sec=page_delay,
                            sleep_between_chunks=chunk_sleep,
                        )
                        n_d = len(_bars_to_ohlc_rows(bars_d))
                    else:
                        logger.info("%s daily: dry-run skip (DB already covers range)", sym)
                logger.info("%s: dry-run hourly=%s daily=%s", sym, n_h, n_d)
                time.sleep(float(args.sleep_seconds))
                continue

            ch_before = con.total_changes
            if not args.no_hourly:
                if ranges_h:
                    bars_h = fetch_bars_chunked(
                        client,
                        sym,
                        ranges=ranges_h,
                        timeframe="1Hour",
                        feed=feed,
                        chunk_days=max(7, int(args.hourly_chunk_days)),
                        page_delay_sec=page_delay,
                        sleep_between_chunks=chunk_sleep,
                    )
                    h_rows = _bars_to_ohlc_rows(bars_h)
                    upsert_intraday_rows(
                        con,
                        "prices_hourly",
                        sym,
                        h_rows,
                        source=SOURCE,
                        commit=False,
                        on_conflict="ignore",
                    )
                else:
                    logger.info("%s hourly: skip (DB already covers range)", sym)
            if not args.no_daily:
                if ranges_d:
                    bars_d = fetch_bars_chunked(
                        client,
                        sym,
                        ranges=ranges_d,
                        timeframe="1Day",
                        feed=feed,
                        chunk_days=max(30, int(args.daily_chunk_days)),
                        page_delay_sec=page_delay,
                        sleep_between_chunks=chunk_sleep,
                    )
                    d_rows = _bars_to_ohlc_rows(bars_d)
                    upsert_price_rows(
                        con,
                        sym,
                        d_rows,
                        interval="1d",
                        source=SOURCE,
                        commit=False,
                        on_conflict="ignore",
                    )
                else:
                    logger.info("%s daily: skip (DB already covers range)", sym)
            con.commit()
            delta = con.total_changes - ch_before
            logger.info("%s: sqlite total_changes +%s (includes inserts ignored on conflict)", sym, delta)
            time.sleep(float(args.sleep_seconds))
    finally:
        con.close()

    logger.info("Done. (Alpaca returns partial history on IEX without SIP; use ALPACA_DATA_FEED=sip if entitled.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
