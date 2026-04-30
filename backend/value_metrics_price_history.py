"""
OHLCV history via Yahoo Finance for charting (daily / hourly / minute bars).

yfinance constraints (approximate): 1m bars ~ last 7 days; 1h bars span-limited.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import yfinance as yf


INTERVAL_MAP = {"daily": "1d", "hourly": "1h", "minute": "1m"}


def _utc_iso(ts: pd.Timestamp) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.isoformat()


def _extract_close_series(px: pd.DataFrame, sym: str) -> pd.DataFrame:
    """Return a single-column OHLCV dataframe indexed by timestamp."""
    if px is None or px.empty:
        return pd.DataFrame()
    sym_u = sym.strip().upper()
    if isinstance(px.columns, pd.MultiIndex):
        lvl0 = px.columns.get_level_values(0)
        if sym_u in px.columns.get_level_values(1):
            sub = px.xs(sym_u, axis=1, level=1)
        elif "Close" in lvl0:
            sub = px["Close"]
            if isinstance(sub, pd.DataFrame):
                sub = sub.iloc[:, [0]]
                sub.columns = ["Close"]
            else:
                sub = pd.DataFrame({"Close": sub})
        else:
            sub = px.iloc[:, : min(5, px.shape[1])]
    else:
        sub = px
    return sub


def fetch_price_history(
    *,
    symbol: str,
    interval: str,  # daily|hourly|minute
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> List[Dict[str, Any]]:
    sym = str(symbol).strip().upper()
    itv_key = str(interval).strip().lower()
    if itv_key not in INTERVAL_MAP:
        raise ValueError("interval must be daily|hourly|minute")
    iv = INTERVAL_MAP[itv_key]

    end_dt = pd.Timestamp(end or datetime.now(timezone.utc).date())
    if end_dt.tzinfo is None:
        end_dt = end_dt.tz_localize("UTC")
    end_naive = end_dt.tz_convert("UTC").tz_localize(None).normalize()

    start_dt: Optional[pd.Timestamp]
    if start:
        start_dt = pd.Timestamp(start)
        if start_dt.tzinfo is None:
            start_dt = start_dt.tz_localize(None)
        start_dt = pd.Timestamp(start_dt).normalize()
    else:
        start_dt = None

    if iv == "1m":
        if start_dt is None:
            start_naive = end_naive - timedelta(days=7)
        else:
            start_naive = start_dt
            if (end_naive - start_naive).days > 7:
                start_naive = end_naive - timedelta(days=7)
    elif iv == "1h":
        if start_dt is None:
            start_naive = end_naive - timedelta(days=120)
        else:
            start_naive = start_dt
            max_days = 729
            if (end_naive - start_naive).days > max_days:
                start_naive = end_naive - timedelta(days=max_days)
    else:
        if start_dt is None:
            start_naive = end_naive - timedelta(days=730)
        else:
            start_naive = start_dt
            max_days = 365 * 10
            if (end_naive - start_naive).days > max_days:
                start_naive = end_naive - timedelta(days=max_days)

    buf_end = (end_naive + timedelta(days=1)).strftime("%Y-%m-%d")
    px = yf.download(
        sym,
        start=start_naive.strftime("%Y-%m-%d"),
        end=buf_end,
        interval=iv,
        auto_adjust=True,
        progress=False,
    )
    sub = _extract_close_series(px, sym)
    if sub.empty:
        return []

    out: List[Dict[str, Any]] = []
    for idx, row in sub.iterrows():
        close_v = row.get("Close")
        if close_v is None or (isinstance(close_v, float) and pd.isna(close_v)):
            continue
        try:
            c = float(close_v)
            if c != c:
                continue
        except Exception:
            continue
        o = row.get("Open")
        h = row.get("High")
        lo = row.get("Low")
        vol = row.get("Volume")
        out.append(
            {
                "ts": _utc_iso(idx),
                "open": float(o) if o is not None and not (isinstance(o, float) and pd.isna(o)) else None,
                "high": float(h) if h is not None and not (isinstance(h, float) and pd.isna(h)) else None,
                "low": float(lo) if lo is not None and not (isinstance(lo, float) and pd.isna(lo)) else None,
                "close": c,
                "volume": float(vol) if vol is not None and not (isinstance(vol, float) and pd.isna(vol)) else None,
            }
        )

    out.sort(key=lambda r: r["ts"])
    return out
