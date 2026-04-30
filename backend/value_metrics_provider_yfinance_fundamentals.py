"""
Fetch quarterly/annual fundamentals snapshots from Yahoo Finance statements.

These are the *inputs* used to compute daily value-metrics time series efficiently
without recomputing statements at web-request time.
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
            out.append(pd.Timestamp(c).tz_localize(None).normalize())
        except Exception:
            continue
    return sorted(set(out))


def _get(df: Optional[pd.DataFrame], row: str, col: pd.Timestamp) -> Optional[float]:
    if df is None or df.empty or row not in df.index or col not in df.columns:
        return None
    return _f(df.at[row, col])


def fetch_yfinance_fundamentals_history(
    *,
    symbol: str,
    period: str,  # quarter|annual
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
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

    out: List[Dict[str, Any]] = []
    for col in dates:
        d = col.strftime("%Y-%m-%d")
        if start_date and d < str(start_date):
            continue
        if end_date and d > str(end_date):
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

        shares = (net_income / eps) if (net_income and eps and abs(float(eps)) > 1e-12) else None

        raw = {
            "source": "yfinance_statements",
            "fiscal_column": str(col),
        }

        out.append(
            {
                "symbol": sym,
                "asof_date": d,
                "fetched_ts_utc": _utcnow_iso(),
                "revenue": revenue,
                "operating_income": op_inc,
                "net_income": net_income,
                "eps": eps,
                "ebitda": ebitda,
                "equity": equity,
                "debt": debt,
                "current_assets": ca,
                "current_liabilities": cl,
                "cash": cash,
                "free_cash_flow": fcf,
                "implied_shares": shares,
                "raw": raw,
            }
        )

    out.sort(key=lambda r: r["asof_date"])
    return out

