from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Dict, Optional

import pandas as pd
import yfinance as yf


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        if v != v:
            return None
        return v
    except Exception:
        return None


def _extract_close(df: pd.DataFrame, symbol: str) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=float)
    if isinstance(df.columns, pd.MultiIndex):
        lvl0 = df.columns.get_level_values(0)
        lvl1 = df.columns.get_level_values(1)
        if "Close" in lvl0 and symbol in lvl1:
            ser = df.xs(symbol, axis=1, level=1)["Close"]
        elif "Close" in lvl0:
            ser = df["Close"]
            if isinstance(ser, pd.DataFrame):
                ser = ser.iloc[:, 0]
        else:
            ser = df.iloc[:, 0]
    else:
        ser = df["Close"] if "Close" in df.columns else df.iloc[:, 0]
    if isinstance(ser, pd.DataFrame):
        ser = ser.iloc[:, 0]
    s = pd.to_numeric(ser, errors="coerce").dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None)
    return s.sort_index()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    return 100 - (100 / (1 + rs))


def _total_return(close: pd.Series, years: int) -> Optional[float]:
    if close.empty:
        return None
    end = close.index.max()
    start = end - pd.Timedelta(days=int(years * 365.25))
    s = close.loc[close.index >= start]
    if s.empty:
        return None
    first = float(s.iloc[0])
    last = float(s.iloc[-1])
    if first <= 0:
        return None
    return (last / first) - 1.0


def _max_drawdown(close: pd.Series, lookback_days: int = 365) -> Optional[float]:
    if close.empty:
        return None
    end = close.index.max()
    s = close.loc[close.index >= (end - pd.Timedelta(days=lookback_days))]
    if s.empty:
        return None
    roll_max = s.cummax()
    dd = (s / roll_max) - 1.0
    return float(dd.min()) if len(dd) else None


def fetch_standard_metrics(symbol: str, benchmark: str = "SPY") -> Dict[str, Any]:
    sym = str(symbol).strip().upper()
    t = yf.Ticker(sym)
    try:
        info = dict(t.info or {})
    except Exception:
        info = {}

    hist = yf.download(sym, period="10y", interval="1d", auto_adjust=True, progress=False)
    close = _extract_close(hist, sym)
    if close.empty:
        raise ValueError(f"no price history for {sym}")
    volume: Optional[pd.Series] = None
    if isinstance(hist, pd.DataFrame) and "Volume" in (hist.columns.get_level_values(0) if isinstance(hist.columns, pd.MultiIndex) else hist.columns):
        if isinstance(hist.columns, pd.MultiIndex):
            if sym in hist.columns.get_level_values(1):
                vol_raw = hist.xs(sym, axis=1, level=1).get("Volume")
            else:
                vol_raw = hist["Volume"]
                if isinstance(vol_raw, pd.DataFrame):
                    vol_raw = vol_raw.iloc[:, 0]
        else:
            vol_raw = hist.get("Volume")
        if vol_raw is not None:
            volume = pd.to_numeric(vol_raw, errors="coerce").dropna()
            volume.index = pd.to_datetime(volume.index).tz_localize(None)
            volume = volume.sort_index()

    ret = close.pct_change().dropna()
    ret_1y = ret.loc[ret.index >= (ret.index.max() - pd.Timedelta(days=365))]

    bench_hist = yf.download(benchmark, period="2y", interval="1d", auto_adjust=True, progress=False)
    bench_close = _extract_close(bench_hist, benchmark.strip().upper())
    bench_ret = bench_close.pct_change().dropna()
    joined = pd.concat([ret_1y, bench_ret], axis=1, join="inner").dropna()
    joined.columns = ["asset", "bench"] if not joined.empty else []

    sharpe = None
    beta = None
    alpha = None
    volatility = None
    if len(ret_1y) >= 5:
        mu = float(ret_1y.mean())
        sigma = float(ret_1y.std(ddof=1))
        if sigma > 0:
            sharpe = (mu / sigma) * math.sqrt(252.0)
            volatility = sigma * math.sqrt(252.0)
    if len(joined) >= 20:
        cov = float(joined["asset"].cov(joined["bench"]))
        var_b = float(joined["bench"].var())
        if var_b > 0:
            beta = cov / var_b
            alpha = (float(joined["asset"].mean()) - beta * float(joined["bench"].mean())) * 252.0

    last_close = float(close.iloc[-1])
    c_52 = close.loc[close.index >= (close.index.max() - pd.Timedelta(days=365))]
    high_52 = float(c_52.max()) if len(c_52) else None
    low_52 = float(c_52.min()) if len(c_52) else None
    range_pos = None
    if high_52 is not None and low_52 is not None and high_52 > low_52:
        range_pos = (last_close - low_52) / (high_52 - low_52)

    year_start = pd.Timestamp(close.index.max().year, 1, 1)
    ytd_ser = close.loc[close.index >= year_start]
    ytd = ((float(ytd_ser.iloc[-1]) / float(ytd_ser.iloc[0])) - 1.0) if len(ytd_ser) >= 2 and float(ytd_ser.iloc[0]) > 0 else None

    rsi = _rsi(close, n=14).dropna()
    out = {
        "symbol": sym,
        "fetched_ts_utc": _utcnow_iso(),
        "pe": _f(info.get("trailingPE") or info.get("forwardPE")),
        "pb": _f(info.get("priceToBook")),
        "peg": _f(info.get("pegRatio")),
        "dividend_yield": _f(info.get("trailingAnnualDividendYield") or info.get("dividendYield")),
        "free_cash_flow_yield": None,
        "debt_to_equity": _f(info.get("debtToEquity")),
        "roe": _f(info.get("returnOnEquity")),
        "current_ratio": _f(info.get("currentRatio")),
        "operating_margin": _f(info.get("operatingMargins")),
        "ev_to_ebitda": _f(info.get("enterpriseToEbitda")),
        "total_return_1y": _total_return(close, 1),
        "total_return_3y": _total_return(close, 3),
        "total_return_5y": _total_return(close, 5),
        "total_return_10y": _total_return(close, 10),
        "high_52w": high_52,
        "low_52w": low_52,
        "range_position_52w": range_pos,
        "ytd_return": ytd,
        "sharpe_ratio": sharpe,
        "beta": beta,
        "alpha": alpha,
        "volatility": volatility,
        "max_drawdown": _max_drawdown(close, 365),
        "average_volume": float(volume.tail(30).mean()) if volume is not None and len(volume) else None,
        "expense_ratio": _f(info.get("annualReportExpenseRatio") or info.get("expenseRatio")),
        "trailing_pe": _f(info.get("trailingPE")),
        "mean_rsi_7d": float(rsi.tail(7).mean()) if len(rsi) >= 7 else None,
        "mean_rsi_30d": float(rsi.tail(30).mean()) if len(rsi) >= 30 else None,
        "mean_rsi_3m": float(rsi.tail(63).mean()) if len(rsi) >= 63 else None,
        "mean_rsi_1y": float(rsi.tail(252).mean()) if len(rsi) >= 252 else None,
        "raw": {
            "provider": "yfinance",
            "benchmark": benchmark,
            "history_rows": int(len(close)),
        },
    }
    if out["dividend_yield"] is not None and float(out["dividend_yield"]) > 1:
        out["dividend_yield"] = float(out["dividend_yield"]) / 100.0
    fcf = _f(info.get("freeCashflow"))
    mcap = _f(info.get("marketCap"))
    out["free_cash_flow_yield"] = (fcf / mcap) if (fcf is not None and mcap is not None and mcap > 0) else None
    return out

