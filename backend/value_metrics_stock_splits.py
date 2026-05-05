"""Cumulative stock split ratio after a calendar date (via yfinance)."""

from __future__ import annotations

from typing import Optional


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
