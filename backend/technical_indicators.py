"""
Daily technical indicators from OHLCV: EMA, MACD, ADX, RVOL.

Default periods (documented in value_web/README.md):
  EMA 20 | MACD 12/26/9 | ADX 14 | RVOL vs 20-day average volume
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

EMA_PERIOD = 20
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ADX_PERIOD = 14
RVOL_PERIOD = 20


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / float(period), adjust=False, min_periods=period).mean()


def compute_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = ADX_PERIOD) -> pd.Series:
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=close.index,
        dtype=float,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=close.index,
        dtype=float,
    )

    tr_s = _wilder_smooth(tr, period)
    plus_dm_s = _wilder_smooth(plus_dm, period)
    minus_dm_s = _wilder_smooth(minus_dm, period)

    plus_di = 100.0 * plus_dm_s / tr_s.replace(0.0, np.nan)
    minus_di = 100.0 * minus_dm_s / tr_s.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return _wilder_smooth(dx, period)


def compute_technical_indicators(
    df: pd.DataFrame,
    *,
    ema_period: int = EMA_PERIOD,
    macd_fast: int = MACD_FAST,
    macd_slow: int = MACD_SLOW,
    macd_signal_period: int = MACD_SIGNAL,
    adx_period: int = ADX_PERIOD,
    rvol_period: int = RVOL_PERIOD,
) -> pd.DataFrame:
    """
    Input ``df`` indexed by date with columns close, high, low, volume (float).
    Returns DataFrame with ema, macd_line, macd_signal, adx, rvol, close.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.copy()
    work.index = pd.to_datetime(work.index).tz_localize(None).normalize()
    work = work.sort_index()

    close = pd.to_numeric(work["close"], errors="coerce")
    high = pd.to_numeric(work.get("high", close), errors="coerce").fillna(close)
    low = pd.to_numeric(work.get("low", close), errors="coerce").fillna(close)
    volume = pd.to_numeric(work.get("volume"), errors="coerce").fillna(0.0)

    ema = close.ewm(span=int(ema_period), adjust=False, min_periods=int(ema_period)).mean()
    ema_fast = close.ewm(span=int(macd_fast), adjust=False, min_periods=int(macd_fast)).mean()
    ema_slow = close.ewm(span=int(macd_slow), adjust=False, min_periods=int(macd_slow)).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=int(macd_signal_period), adjust=False, min_periods=int(macd_signal_period)).mean()

    avg_vol = volume.rolling(int(rvol_period), min_periods=max(5, int(rvol_period) // 2)).mean()
    rvol = volume / avg_vol.replace(0.0, np.nan)
    adx = compute_adx(high, low, close, period=int(adx_period))

    out = pd.DataFrame(
        {
            "close": close,
            "ema": ema,
            "macd_line": macd_line,
            "macd_signal": macd_signal,
            "adx": adx,
            "rvol": rvol,
        },
        index=work.index,
    )
    return out


def indicators_to_points(
    symbol: str,
    indicators: pd.DataFrame,
    *,
    start_s: Optional[str] = None,
    end_s: Optional[str] = None,
    fetched_ts_utc: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Convert indicator DataFrame rows to upsert payloads."""
    sym = str(symbol).strip().upper()
    if indicators is None or indicators.empty:
        return []
    start = pd.Timestamp(start_s).tz_localize(None).normalize() if start_s else None
    end = pd.Timestamp(end_s).tz_localize(None).normalize() if end_s else None
    fetched = fetched_ts_utc or _utcnow_iso()
    points: List[Dict[str, Any]] = []
    for dt, row in indicators.iterrows():
        ts = pd.Timestamp(dt).tz_localize(None).normalize()
        if start is not None and ts < start:
            continue
        if end is not None and ts > end:
            continue
        if pd.isna(row.get("close")):
            continue
        points.append(
            {
                "symbol": sym,
                "asof_date": ts.strftime("%Y-%m-%d"),
                "close": None if pd.isna(row["close"]) else float(row["close"]),
                "ema": None if pd.isna(row.get("ema")) else float(row["ema"]),
                "macd_line": None if pd.isna(row.get("macd_line")) else float(row["macd_line"]),
                "macd_signal": None if pd.isna(row.get("macd_signal")) else float(row["macd_signal"]),
                "adx": None if pd.isna(row.get("adx")) else float(row["adx"]),
                "rvol": None if pd.isna(row.get("rvol")) else float(row["rvol"]),
                "fetched_ts_utc": fetched,
            }
        )
    return points


def ohlcv_rows_to_frame(rows: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    recs = []
    for r in rows:
        d = str(r.get("date") or r.get("asof_date") or str(r.get("ts", ""))[:10])
        if not d:
            continue
        recs.append(
            {
                "date": d,
                "close": r.get("close"),
                "high": r.get("high"),
                "low": r.get("low"),
                "volume": r.get("volume"),
            }
        )
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    for c in ("close", "high", "low", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df
