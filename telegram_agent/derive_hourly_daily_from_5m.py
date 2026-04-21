#!/usr/bin/env python3
"""
Derive ``prices_hourly`` and daily ``prices`` (interval ``1d``) from stored **5m** bars.

For each symbol (default: P0+P1 from the configured symbol universe, priority <= 1):

- **Hourly**: last 5m close in each UTC hour (same logic as ``rsi_portfolio_simulator._resample_to_hourly_close``).
- **Daily**: last 5m close in each **UTC calendar day**, bar timestamp ``YYYY-MM-DD 00:00:00+00:00``.

Rows are inserted with ``source='derived_from_5m'`` and ``ON CONFLICT DO NOTHING`` so existing
yfinance / parquet / manual rows for the same (symbol, ts) are preserved.

Usage::

    python -m telegram_agent.derive_hourly_daily_from_5m --dry-run
    python -m telegram_agent.derive_hourly_daily_from_5m
    python -m telegram_agent.derive_hourly_daily_from_5m --symbols AAPL,MSFT --limit 5

If ``SYMBOL_UNIVERSE_ENABLED`` is false, symbols are still loaded from ``SYMBOL_UNIVERSE_PATH``
(or ``--universe-path``). Flat JSON maps ``{TICKER: priority}`` (e.g. ``top1000_investments_prioritised.json``) are supported.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from telegram_agent.agent_db import (
    _utc_iso,
    connect,
    get_full_adj_close_series_asc,
    init_db,
    upsert_intraday_rows,
    upsert_price_rows,
)
from telegram_agent.config import load_config
from telegram_agent.rsi_portfolio_simulator import _resample_to_hourly_close
from telegram_agent.symbol_universe import (
    _load_json_symbols_with_priority,
    load_symbol_universe,
    normalize_symbol,
)

logger = logging.getLogger(__name__)

SOURCE_DERIVED = "derived_from_5m"


def _load_priority_pairs_from_json(path: Path) -> List[Tuple[str, Optional[int]]]:
    """
    Load (symbol, priority) pairs from universe JSON.

    Supports the same layouts as ``_load_json_symbols_with_priority``, plus a flat map::
        {"AAPL": 0, "MSFT": 1, ...}
    """
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, dict) and data:
        vals = list(data.values())
        if vals and all(isinstance(v, int) for v in vals):
            out: List[Tuple[str, Optional[int]]] = []
            for k, v in data.items():
                if not isinstance(k, str) or not str(k).strip():
                    continue
                out.append((normalize_symbol(str(k)), int(v)))
            if out:
                return out
    return _load_json_symbols_with_priority(path)


def _load_symbols_by_max_priority(
    cfg: dict,
    *,
    max_priority: int,
    universe_path: Optional[Path] = None,
) -> List[str]:
    """
    Same filtering as ``load_symbol_universe`` (priority <= max_priority).

    If the universe is disabled in config, ``load_symbol_universe`` returns None; we then
    read ``--universe-path`` or ``SYMBOL_UNIVERSE_PATH`` JSON directly so backfills still work.
    """
    cfg_m = {**cfg, "max_priority": int(max_priority)}
    syms = load_symbol_universe(cfg_m)
    if syms:
        return syms

    path = universe_path
    if path is None:
        raw = (cfg.get("symbol_universe_path") or "").strip()
        if raw:
            path = Path(raw)
    if path is None or not path.is_file():
        return []

    try:
        pairs = _load_priority_pairs_from_json(path)
    except (ValueError, json.JSONDecodeError, OSError) as e:
        logger.warning("Could not parse universe file %s: %s", path, e)
        return []
    m = int(max_priority)
    out = [
        s
        for s, pr in pairs
        if (pr is None) or (int(pr) <= m)
    ]
    return sorted(set(s for s in out if s))


def _resample_to_daily_close_utc(
    series: Sequence[Tuple[datetime, float]],
) -> List[Tuple[datetime, float]]:
    """Last price in each UTC calendar day; timestamp = midnight UTC on that day."""
    if not series:
        return []
    out: List[Tuple[datetime, float]] = []
    cur_day: Optional[date] = None
    last_px: Optional[float] = None

    for ts, px in series:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        d = ts.date()
        if cur_day is None:
            cur_day = d
        elif d != cur_day:
            if last_px is not None:
                dt0 = datetime.combine(cur_day, time.min, tzinfo=timezone.utc)
                out.append((dt0, float(last_px)))
            cur_day = d
            last_px = None
        if px is not None and float(px) > 0:
            last_px = float(px)

    if cur_day is not None and last_px is not None:
        dt0 = datetime.combine(cur_day, time.min, tzinfo=timezone.utc)
        out.append((dt0, float(last_px)))
    return out


def _to_ohlc_rows(
    series: List[Tuple[datetime, float]],
) -> List[Tuple[str, float, float, float, float, Optional[float], Optional[float]]]:
    rows: List[Tuple[str, float, float, float, float, Optional[float], Optional[float]]] = []
    for ts, px in series:
        p = float(px)
        iso = _utc_iso(ts)
        rows.append((iso, p, p, p, p, None, None))
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(
        description="Backfill prices_hourly and daily prices from 5m data (P0+P1 by default)."
    )
    p.add_argument(
        "--symbols",
        type=str,
        default="",
        help="Comma-separated tickers; overrides universe when non-empty",
    )
    p.add_argument(
        "--max-priority",
        type=int,
        default=1,
        help="When using universe: include symbols with priority <= this (default 1 = P0+P1)",
    )
    p.add_argument(
        "--universe-path",
        type=Path,
        default=None,
        help=(
            "Optional JSON universe file (overrides SYMBOL_UNIVERSE_PATH). "
            "Used when SYMBOL_UNIVERSE_ENABLED is false — reads priorities from file without enabling the global flag."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N symbols (0 = all)",
    )
    p.add_argument(
        "--min-5m-bars",
        type=int,
        default=2,
        help="Skip symbols with fewer than this many 5m bars",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Log counts only; no DB writes",
    )
    args = p.parse_args(list(argv) if argv is not None else None)

    cfg = load_config()
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))

    if args.symbols.strip():
        syms = sorted(
            {normalize_symbol(x) for x in args.symbols.split(",") if x.strip()}
        )
    else:
        syms = _load_symbols_by_max_priority(
            cfg,
            max_priority=int(args.max_priority),
            universe_path=args.universe_path,
        )
        if not syms:
            print(
                "No symbols: set SYMBOL_UNIVERSE_PATH to a JSON universe (see .env.example), "
                "or pass --universe-path path/to/universe.json, "
                "or enable SYMBOL_UNIVERSE_ENABLED=true, "
                "or pass --symbols AAPL,MSFT",
                file=sys.stderr,
            )
            return 2

    if args.limit and args.limit > 0:
        syms = syms[: int(args.limit)]

    con = connect(db)
    init_db(con)

    total_h = 0
    total_d = 0
    skipped = 0
    sym_done = 0

    try:
        for sym in syms:
            ser_5m = get_full_adj_close_series_asc(con, sym, "5m")
            if len(ser_5m) < int(args.min_5m_bars):
                logger.info("%s: skip (5m bars=%s < min_5m_bars)", sym, len(ser_5m))
                skipped += 1
                continue

            hourly = _resample_to_hourly_close(ser_5m)
            daily = _resample_to_daily_close_utc(ser_5m)
            h_rows = _to_ohlc_rows(hourly)
            d_rows = _to_ohlc_rows(daily)

            if args.dry_run:
                logger.info(
                    "%s: would insert hourly=%s daily=%s (from 5m=%s)",
                    sym,
                    len(h_rows),
                    len(d_rows),
                    len(ser_5m),
                )
                total_h += len(h_rows)
                total_d += len(d_rows)
                sym_done += 1
                continue

            ch0 = con.total_changes
            upsert_intraday_rows(
                con,
                "prices_hourly",
                sym,
                h_rows,
                source=SOURCE_DERIVED,
                commit=False,
                on_conflict="ignore",
            )
            nh = con.total_changes - ch0
            ch1 = con.total_changes
            upsert_price_rows(
                con,
                sym,
                d_rows,
                interval="1d",
                source=SOURCE_DERIVED,
                commit=False,
                on_conflict="ignore",
            )
            nd = con.total_changes - ch1
            con.commit()
            total_h += nh
            total_d += nd
            sym_done += 1
            logger.info(
                "%s: new rows hourly=%s daily=%s (attempted %s/%s; 5m=%s)",
                sym,
                nh,
                nd,
                len(h_rows),
                len(d_rows),
                len(ser_5m),
            )
    finally:
        con.close()

    if args.dry_run:
        logger.info(
            "Done (dry-run): symbols_processed=%s skipped=%s hourly_bars=%s daily_bars=%s (no DB writes)",
            sym_done,
            skipped,
            total_h,
            total_d,
        )
    else:
        logger.info(
            "Done: symbols_processed=%s skipped=%s hourly_rows_new=%s daily_rows_new=%s",
            sym_done,
            skipped,
            total_h,
            total_d,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
