"""Cumulative stock split ratio after a calendar date (via yfinance)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def split_factor_after(symbol: str, asof_date: str) -> float:
    """
    Product of split ratios with ex-dates strictly after ``asof_date`` (YYYY-MM-DD).

    Used to reconcile SEC ``EarningsPerShareDiluted`` when weighted-average share tags are missing.
    Returns 1.0 if unavailable.
    """
    try:
        import pandas as pd
        import yfinance as yf

        t = yf.Ticker(str(symbol).strip().upper())
        splits: Optional[object] = getattr(t, "splits", None)
        if splits is None or len(splits) == 0:
            return 1.0
        s = splits.astype(float)
        if s.index.tz is not None:
            s = s.copy()
            s.index = s.index.tz_convert("UTC").tz_localize(None)
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        dt = pd.Timestamp(asof_date).tz_localize(None).normalize()
        after = s.loc[s.index > dt]
        if after.empty:
            return 1.0
        f = float(after.prod())
        return f if f > 0 else 1.0
    except Exception:
        return 1.0


def persist_yfinance_splits_to_db(con: sqlite3.Connection, symbol: str) -> int:
    """
    Upsert yfinance ``Ticker.splits`` into ``vm_stock_splits`` for chart overlays / EPS logic.
    Returns number of rows upserted (0 if none or error).
    """
    sym = str(symbol).strip().upper()
    if not sym:
        return 0
    try:
        import pandas as pd
        import yfinance as yf

        from value_metrics_store import upsert_stock_splits

        t = yf.Ticker(sym)
        splits = getattr(t, "splits", None)
        if splits is None or len(splits) == 0:
            return 0
        s = splits.astype(float)
        if s.index.tz is not None:
            s = s.copy()
            s.index = s.index.tz_convert("UTC").tz_localize(None)
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        fetched = _utcnow_iso()
        rows: List[Dict[str, Any]] = []
        for idx, ratio in s.items():
            ex = pd.Timestamp(idx).strftime("%Y-%m-%d")
            rows.append(
                {
                    "symbol": sym,
                    "ex_date": ex,
                    "split_ratio": float(ratio),
                    "provider": "yfinance",
                    "fetched_ts_utc": fetched,
                }
            )
        return upsert_stock_splits(con, rows=rows)
    except Exception:
        return 0
