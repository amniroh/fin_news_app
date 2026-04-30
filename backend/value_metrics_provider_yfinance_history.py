"""
Historical fundamentals / valuation-style metrics derived from Yahoo Finance
quarterly or annual financial statements plus daily closes.

This is used when FMP ratio endpoints are unavailable (subscription / legacy limits).

Output rows match ``value_metrics_store.upsert_metric_points`` expectations.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import yfinance as yf


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, (float, int)) and pd.isna(x):
            return None
        v = float(x)
        if v != v:
            return None
        return v
    except Exception:
        return None


def _col_dates(df: Optional[pd.DataFrame]) -> List[pd.Timestamp]:
    if df is None or df.empty:
        return []
    out: List[pd.Timestamp] = []
    for c in df.columns:
        try:
            out.append(pd.Timestamp(c).normalize())
        except Exception:
            continue
    return sorted(set(out), reverse=True)


def _get(df: Optional[pd.DataFrame], row: str, col: pd.Timestamp) -> Optional[float]:
    if df is None or row not in df.index or col not in df.columns:
        return None
    return _f(df.at[row, col])


def _price_on_or_before(closes: pd.Series, asof: pd.Timestamp) -> Optional[float]:
    if closes is None or closes.empty:
        return None
    ts = pd.Timestamp(asof).tz_localize(None).normalize()
    try:
        s = closes.sort_index()
        s = s.loc[:ts]
        if s.empty:
            return None
        return _f(s.iloc[-1])
    except Exception:
        return None


def _dividends_in_range(divs: pd.Series, start_excl: Optional[pd.Timestamp], end_incl: pd.Timestamp) -> float:
    if divs is None or divs.empty:
        return 0.0
    d = divs.sort_index()
    end = pd.Timestamp(end_incl).tz_localize(None)
    if start_excl is not None:
        st = pd.Timestamp(start_excl).tz_localize(None)
        d = d.loc[d.index > st]
    d = d.loc[d.index <= end]
    return float(d.sum()) if len(d) else 0.0


def fetch_yfinance_metrics_history(
    *,
    symbol: str,
    period: str,  # quarter|annual
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Build metric points per fiscal column date in yfinance statements.

    - P/E: close / diluted EPS (quarterly EPS is already per share)
    - P/B: close / (stockholders equity / implied shares)
    - PEG: not estimated here (often needs multi-year growth); left None
    - Dividend yield: sum(dividends in (prev_fiscal_end, fiscal_end]) / (price * shares))
    - FCF yield: free cash flow / (price * shares)
    - Debt/Equity: total debt / stockholders equity
    - ROE: net income / stockholders equity
    - Current ratio: current assets / current liabilities
    - Operating margin: operating income / total revenue
    - EV/EBITDA: (market cap + total debt - cash) / EBITDA, market cap ≈ price * shares
    """
    sym = str(symbol).strip().upper()
    per = str(period).strip().lower()
    if per not in ("quarter", "annual"):
        raise ValueError("period must be quarter or annual")

    t = yf.Ticker(sym)
    if per == "quarter":
        inc = getattr(t, "quarterly_income_stmt", None)
        bal = getattr(t, "quarterly_balance_sheet", None)
        cf = getattr(t, "quarterly_cashflow", None)
    else:
        inc = getattr(t, "income_stmt", None)
        bal = getattr(t, "balance_sheet", None)
        cf = getattr(t, "cashflow", None)

    if inc is None or bal is None or cf is None or inc.empty or bal.empty or cf.empty:
        return []

    dates = sorted(set(_col_dates(inc)) & set(_col_dates(bal)) & set(_col_dates(cf)))
    if not dates:
        return []

    # Price history for the requested window (+buffer for quarter-end alignment)
    start = str(start_date) if start_date else str(dates[0].date())
    end = str(end_date) if end_date else str(dates[-1].date())
    buf_start = (pd.Timestamp(start) - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    buf_end = (pd.Timestamp(end) + pd.Timedelta(days=10)).strftime("%Y-%m-%d")

    px = yf.download(sym, start=buf_start, end=buf_end, interval="1d", auto_adjust=True, progress=False)
    closes = pd.Series(dtype=float)
    if isinstance(px, pd.DataFrame) and not px.empty:
        if isinstance(px.columns, pd.MultiIndex):
            lvl0 = px.columns.get_level_values(0)
            if "Close" in lvl0 and sym in px.columns.get_level_values(1):
                ser = px.xs(sym, axis=1, level=1)["Close"]
            elif "Close" in lvl0:
                ser = px["Close"].iloc[:, 0]
            else:
                ser = px.iloc[:, 0]
        elif "Close" in px.columns:
            ser = px["Close"]
        else:
            ser = px.iloc[:, 0]
        if isinstance(ser, pd.DataFrame):
            ser = ser.iloc[:, 0]
        closes = pd.to_numeric(ser, errors="coerce").dropna()
        if closes.index.tz is not None:
            closes = closes.copy()
            closes.index = closes.index.tz_convert("UTC").tz_localize(None)

    divs = getattr(t, "dividends", None)
    if divs is None or divs.empty:
        div_series = pd.Series(dtype=float)
    else:
        div_series = divs.astype(float)
        if div_series.index.tz is not None:
            div_series = div_series.copy()
            div_series.index = div_series.index.tz_convert("UTC").tz_localize(None)

    out: List[Dict[str, Any]] = []
    prev: Optional[pd.Timestamp] = None
    for col in dates:
        d = col.strftime("%Y-%m-%d")
        if start_date and d < str(start_date):
            prev = col
            continue
        if end_date and d > str(end_date):
            prev = col
            continue

        revenue = _get(inc, "Total Revenue", col)
        op_inc = _get(inc, "Operating Income", col)
        net_income = _get(inc, "Net Income", col)
        eps = _get(inc, "Diluted EPS", col) or _get(inc, "Basic EPS", col)
        ebitda = _get(inc, "EBITDA", col)

        equity = _get(bal, "Stockholders Equity", col)
        debt = _get(bal, "Total Debt", col)
        ca = _get(bal, "Current Assets", col)
        cl = _get(bal, "Current Liabilities", col)
        cash = _get(bal, "Cash Cash Equivalents And Short Term Investments", col) or _get(
            bal, "Cash And Cash Equivalents", col
        )

        fcf = _get(cf, "Free Cash Flow", col)

        px_close = _price_on_or_before(closes, col)
        shares = (net_income / eps) if (net_income and eps and abs(float(eps)) > 1e-12) else None

        mcap = (px_close * shares) if (px_close and shares) else None

        pe = (px_close / eps) if (px_close and eps and abs(float(eps)) > 1e-12) else None
        bps = (equity / shares) if (equity and shares and abs(float(shares)) > 1e-12) else None
        pb = (px_close / bps) if (px_close and bps and abs(float(bps)) > 1e-12) else None

        opm = (op_inc / revenue) if (op_inc is not None and revenue not in (None, 0.0)) else None
        roe = (net_income / equity) if (net_income is not None and equity not in (None, 0.0)) else None
        dte = (debt / equity) if (debt is not None and equity not in (None, 0.0)) else None
        cur_r = (ca / cl) if (ca is not None and cl not in (None, 0.0)) else None

        fcf_y = (fcf / mcap) if (fcf is not None and mcap not in (None, 0.0)) else None

        div_cash = _dividends_in_range(div_series, prev, col)
        div_y = (div_cash / mcap) if (mcap not in (None, 0.0) and div_cash > 0) else None

        ev = None
        ev_e = None
        if mcap is not None and debt is not None and cash is not None and ebitda not in (None, 0.0):
            ev = float(mcap) + float(debt) - float(cash)
            ev_e = ev / float(ebitda) if float(ebitda) != 0 else None

        raw = {
            "source": "yfinance_statements",
            "fiscal_column": str(col),
            "price_close_used": px_close,
            "implied_shares": shares,
        }

        out.append(
            {
                "symbol": sym,
                "asof_date": d,
                "fetched_ts_utc": _utcnow_iso(),
                "pe": pe,
                "pb": pb,
                "peg": None,
                "dividend_yield": div_y,
                "free_cash_flow_yield": fcf_y,
                "debt_to_equity": dte,
                "roe": roe,
                "current_ratio": cur_r,
                "operating_margin": opm,
                "ev_to_ebitda": ev_e,
                "raw": raw,
            }
        )
        prev = col

    out.sort(key=lambda r: r["asof_date"])
    return out
