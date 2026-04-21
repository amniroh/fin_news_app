"""
Stock split discovery (yfinance) and OHLC normalization across price tables.

``share_multiplier`` follows Yahoo (e.g. 4.0 for a 4-for-1 split). Pre-split rows
are adjusted by dividing OHLC (and ``adj_close`` when set) by that factor and
multiplying ``volume`` by it, applied **newest split first** so cumulative
alignment matches current share basis.

Daily bars in ``prices`` with ``interval='1d'`` use NY **calendar** ``ex_date_ny``
(``substr(ts_utc,1,10) < ex_date_ny``) so a daily row dated on the ex-day is not
scaled. Intraday tables and non-daily ``prices`` intervals compare ``ts_utc`` to
``effective_ts_utc`` from Yahoo (typically regular-session open on the ex-day).

Running normalization **twice** will double-apply factors; use DB backup or
re-import raw data before a second pass.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from telegram_agent.agent_db import (
    _utc_iso,
    get_stock_splits_chronological,
    list_symbols_with_any_price_rows,
    upsert_stock_split_rows,
)
from telegram_agent.prices import _yf_symbol

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _ny_zone():
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo (Python 3.9+) is required for split ex-date handling")
    return ZoneInfo("America/New_York")


def split_event_from_yfinance_row(ts_index, share_multiplier: float) -> Tuple[str, str, float]:
    """Build (effective_ts_utc, ex_date_ny, share_multiplier) from a yfinance split index."""
    import pandas as pd

    if isinstance(ts_index, pd.Timestamp):
        ts = ts_index.to_pydatetime()
    else:
        ts = ts_index  # type: ignore[assignment]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    utc = ts.astimezone(timezone.utc)
    effective = _utc_iso(utc)
    ex_ny = utc.astimezone(_ny_zone()).date().isoformat()
    m = float(share_multiplier)
    if abs(m) < 1e-12:
        raise ValueError("split share_multiplier too small")
    return effective, ex_ny, m


def fetch_splits_yfinance(canonical_symbol: str, yf_ticker: str) -> List[Tuple[str, str, float]]:
    import yfinance as yf

    t = yf.Ticker(yf_ticker)
    sp = t.splits
    if sp is None or len(sp) == 0:
        return []
    out: List[Tuple[str, str, float]] = []
    for idx, val in sp.items():
        try:
            r = float(val)
        except (TypeError, ValueError):
            continue
        if abs(r) < 1e-12:
            continue
        out.append(split_event_from_yfinance_row(idx, r))
    return out


def instrument_kind(con, symbol: str) -> str:
    row = con.execute(
        "SELECT kind FROM instruments WHERE symbol = ?",
        (symbol.strip().upper(),),
    ).fetchone()
    if not row:
        return "unknown"
    return str(row["kind"] or "unknown").strip().lower()


def yfinance_ticker_for_symbol(con, canonical: str) -> str:
    sym = canonical.strip().upper()
    kind = instrument_kind(con, sym)
    if kind == "crypto":
        c = sym
        if not c.endswith("-USD") and not c.startswith("^") and "=X" not in c:
            c = f"{c}-USD"
        return _yf_symbol(c)
    return _yf_symbol(sym)


@dataclass
class NormalizationStats:
    symbol: str
    splits_applied: int
    rowcounts: Dict[str, int]

    def total_rows(self) -> int:
        return int(sum(self.rowcounts.values()))


def _cumulative_price_scale_factor(
    ts_utc: str,
    interval: str,
    splits_chronological_asc: Sequence[Tuple[str, str, float]],
) -> float:
    """Product of (1/mult) for each split that applies to this bar (splits oldest → newest)."""
    f = 1.0
    iv = (interval or "").strip()
    for eff, ex_ny, mult in splits_chronological_asc:
        m = float(mult)
        if abs(m) < 1e-15:
            continue
        if iv == "1d":
            if len(ts_utc) >= 10 and ts_utc[:10] < ex_ny:
                f /= m
        else:
            if ts_utc < eff:
                f /= m
    return f


def _cumulative_price_scale_factor_intraday_bar(
    ts_utc: str,
    splits_chronological_asc: Sequence[Tuple[str, str, float]],
) -> float:
    f = 1.0
    for eff, _ex_ny, mult in splits_chronological_asc:
        m = float(mult)
        if abs(m) < 1e-15:
            continue
        if ts_utc < eff:
            f /= m
    return f


def symbol_price_ts_bounds(con, symbol: str) -> Optional[Tuple[str, str]]:
    """Min and max ``ts_utc`` across ``prices``, ``prices_hourly``, ``prices_minute`` (lexicographic OK for ISO)."""
    sym = symbol.strip().upper()
    row = con.execute(
        """
        SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM (
            SELECT ts_utc AS ts FROM prices WHERE symbol = ?
            UNION ALL
            SELECT ts_utc FROM prices_hourly WHERE symbol = ?
            UNION ALL
            SELECT ts_utc FROM prices_minute WHERE symbol = ?
        )
        """,
        (sym, sym, sym),
    ).fetchone()
    if not row or row["lo"] is None:
        return None
    return str(row["lo"]), str(row["hi"])


def splits_touching_stored_bars(
    con,
    symbol: str,
    splits_chronological_asc: Sequence[Tuple[str, str, float]],
) -> List[Tuple[str, str, float]]:
    """Splits for which at least one stored row matches the normalization WHERE clause."""
    out: List[Tuple[str, str, float]] = []
    for eff, ex_ny, mult in splits_chronological_asc:
        part = apply_split_to_symbol_prices(
            con,
            symbol,
            effective_ts_utc=eff,
            ex_date_ny=ex_ny,
            share_multiplier=mult,
            dry_run=True,
        )
        if sum(part.values()) > 0:
            out.append((eff, ex_ny, mult))
    return out


def unique_bar_update_counts(
    con,
    symbol: str,
    splits_chronological_asc: Sequence[Tuple[str, str, float]],
    *,
    eps: float = 1e-12,
) -> Tuple[Dict[str, int], int]:
    """
    Count distinct rows whose OHLC would be scaled (cumulative factor ≠ 1) if normalization runs.

    One pass per table; ``prices`` counts each (ts_utc, interval) row at most once.
    """
    sym = symbol.strip().upper()
    chrono = list(splits_chronological_asc)
    n_prices = 0
    cur = con.execute("SELECT ts_utc, interval FROM prices WHERE symbol = ?", (sym,))
    for row in cur:
        ts_ = str(row["ts_utc"])
        iv = str(row["interval"] or "")
        f = _cumulative_price_scale_factor(ts_, iv, chrono)
        if abs(f - 1.0) > eps:
            n_prices += 1
    n_h = 0
    cur = con.execute("SELECT ts_utc FROM prices_hourly WHERE symbol = ?", (sym,))
    for row in cur:
        f = _cumulative_price_scale_factor_intraday_bar(str(row["ts_utc"]), chrono)
        if abs(f - 1.0) > eps:
            n_h += 1
    n_m = 0
    cur = con.execute("SELECT ts_utc FROM prices_minute WHERE symbol = ?", (sym,))
    for row in cur:
        f = _cumulative_price_scale_factor_intraday_bar(str(row["ts_utc"]), chrono)
        if abs(f - 1.0) > eps:
            n_m += 1
    d = {"prices": n_prices, "prices_hourly": n_h, "prices_minute": n_m}
    return d, int(n_prices + n_h + n_m)


def _count_matching(
    con,
    sql: str,
    params: Tuple,
) -> int:
    row = con.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def _update_ohlcv_table(
    con,
    table: str,
    symbol: str,
    *,
    price_factor: float,
    volume_factor: float,
    where_sql: str,
    where_params: Tuple,
    dry_run: bool,
) -> int:
    sym = symbol.upper()
    params = (price_factor, price_factor, price_factor, price_factor, price_factor, volume_factor, sym) + where_params
    count_sql = f"""
        SELECT COUNT(*) FROM {table}
        WHERE symbol = ? AND ({where_sql})
    """
    n = _count_matching(con, count_sql, (sym,) + where_params)
    if dry_run or n == 0:
        return n
    upd = f"""
        UPDATE {table} SET
            open = CASE WHEN open IS NOT NULL THEN open * ? ELSE NULL END,
            high = CASE WHEN high IS NOT NULL THEN high * ? ELSE NULL END,
            low = CASE WHEN low IS NOT NULL THEN low * ? ELSE NULL END,
            close = CASE WHEN close IS NOT NULL THEN close * ? ELSE NULL END,
            adj_close = CASE WHEN adj_close IS NOT NULL THEN adj_close * ? ELSE NULL END,
            volume = CASE WHEN volume IS NOT NULL THEN volume * ? ELSE NULL END
        WHERE symbol = ? AND ({where_sql})
    """
    con.execute(upd, params)
    return n


def apply_split_to_symbol_prices(
    con,
    symbol: str,
    *,
    effective_ts_utc: str,
    ex_date_ny: str,
    share_multiplier: float,
    dry_run: bool,
) -> Dict[str, int]:
    """
    Apply one split (``share_multiplier`` from Yahoo) to all stored bars **before**
    the split (newest-first caller handles ordering).

    Returns per-table row counts matching the WHERE clause (updated or would-update).
    """
    sym = symbol.upper()
    inv = 1.0 / float(share_multiplier)
    vol_mult = float(share_multiplier)
    counts: Dict[str, int] = {}

    # Daily bars: NY calendar date on the bar label (UTC date prefix).
    w_daily = "interval = '1d' AND substr(ts_utc, 1, 10) < ?"
    counts["prices_1d"] = _update_ohlcv_table(
        con,
        "prices",
        sym,
        price_factor=inv,
        volume_factor=vol_mult,
        where_sql=w_daily,
        where_params=(ex_date_ny,),
        dry_run=dry_run,
    )

    # Intraday and non-daily intervals in `prices` (e.g. 5m).
    w_intraday_prices = "interval != '1d' AND ts_utc < ?"
    counts["prices_intraday"] = _update_ohlcv_table(
        con,
        "prices",
        sym,
        price_factor=inv,
        volume_factor=vol_mult,
        where_sql=w_intraday_prices,
        where_params=(effective_ts_utc,),
        dry_run=dry_run,
    )

    w_h = "ts_utc < ?"
    counts["prices_hourly"] = _update_ohlcv_table(
        con,
        "prices_hourly",
        sym,
        price_factor=inv,
        volume_factor=vol_mult,
        where_sql=w_h,
        where_params=(effective_ts_utc,),
        dry_run=dry_run,
    )
    counts["prices_minute"] = _update_ohlcv_table(
        con,
        "prices_minute",
        sym,
        price_factor=inv,
        volume_factor=vol_mult,
        where_sql=w_h,
        where_params=(effective_ts_utc,),
        dry_run=dry_run,
    )
    return counts


def normalize_symbol_for_stored_splits(
    con,
    symbol: str,
    *,
    dry_run: bool = False,
) -> NormalizationStats:
    splits = get_stock_splits_chronological(con, symbol)
    if not splits:
        return NormalizationStats(symbol=symbol, splits_applied=0, rowcounts={})
    merged: Dict[str, int] = {}
    # Newest split first avoids double-scaling ranges.
    for effective_ts_utc, ex_date_ny, mult in reversed(splits):
        part = apply_split_to_symbol_prices(
            con,
            symbol,
            effective_ts_utc=effective_ts_utc,
            ex_date_ny=ex_date_ny,
            share_multiplier=mult,
            dry_run=dry_run,
        )
        for k, v in part.items():
            merged[k] = merged.get(k, 0) + v
    if not dry_run:
        con.commit()
    return NormalizationStats(symbol=symbol, splits_applied=len(splits), rowcounts=merged)


def fetch_and_store_splits_for_symbols(
    con,
    symbols: Sequence[str],
    *,
    sleep_seconds: float = 0.35,
    skip_crypto: bool = True,
) -> Dict[str, int]:
    """Fetch Yahoo splits for each symbol and upsert into ``stock_splits``. Returns symbol -> n splits."""
    fetched = _utc_iso(datetime.now(timezone.utc))
    out: Dict[str, int] = {}
    for raw in symbols:
        sym = raw.strip().upper()
        if not sym:
            continue
        if skip_crypto and instrument_kind(con, sym) == "crypto":
            out[sym] = 0
            continue
        yf_t = yfinance_ticker_for_symbol(con, sym)
        try:
            evs = fetch_splits_yfinance(sym, yf_t)
        except Exception as e:
            logger.warning("yfinance splits failed %s (%s): %s", sym, yf_t, e)
            out[sym] = 0
            continue
        rows = [
            (sym, eff, ex_ny, mult, "yfinance", fetched)
            for eff, ex_ny, mult in evs
        ]
        upsert_stock_split_rows(con, rows, commit=True)
        out[sym] = len(rows)
        if sleep_seconds > 0:
            time.sleep(float(sleep_seconds))
    return out


def default_symbol_list(con) -> List[str]:
    return list_symbols_with_any_price_rows(con)
