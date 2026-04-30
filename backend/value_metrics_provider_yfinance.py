from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import yfinance as yf


@dataclass(frozen=True)
class ValueMetrics:
    symbol: str
    fetched_ts_utc: str
    pe: Optional[float]
    pb: Optional[float]
    peg: Optional[float]
    dividend_yield: Optional[float]  # fraction, e.g. 0.03
    free_cash_flow_yield: Optional[float]  # fraction, e.g. 0.05
    debt_to_equity: Optional[float]
    roe: Optional[float]  # fraction
    current_ratio: Optional[float]
    operating_margin: Optional[float]  # fraction
    ev_to_ebitda: Optional[float]
    raw: Dict[str, Any]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        if v != v:  # NaN
            return None
        return v
    except Exception:
        return None


def fetch_value_metrics(symbol: str) -> ValueMetrics:
    sym = str(symbol).strip().upper()
    t = yf.Ticker(sym)
    info = {}
    try:
        info = dict(t.info or {})
    except Exception:
        info = {}

    pe = _f(info.get("trailingPE") or info.get("forwardPE"))
    pb = _f(info.get("priceToBook"))
    peg = _f(info.get("pegRatio"))

    # Yahoo sometimes reports `dividendYield` inconsistently (fraction vs percent-ish).
    # Prefer the trailing annual yield when available; otherwise normalize values that look like percents.
    div_y = _f(info.get("trailingAnnualDividendYield"))
    if div_y is None:
        div_y = _f(info.get("dividendYield"))
    if div_y is not None and div_y > 1:
        div_y = div_y / 100.0

    # FCF yield: freeCashflow / marketCap
    fcf = _f(info.get("freeCashflow"))
    mcap = _f(info.get("marketCap"))
    fcf_y = (fcf / mcap) if (fcf is not None and mcap is not None and mcap > 0) else None

    # Prefer computed ratios when provided directly.
    dte = _f(info.get("debtToEquity"))
    roe = _f(info.get("returnOnEquity"))
    cur = _f(info.get("currentRatio"))
    opm = _f(info.get("operatingMargins"))
    ev_e = _f(info.get("enterpriseToEbitda"))

    return ValueMetrics(
        symbol=sym,
        fetched_ts_utc=_utcnow_iso(),
        pe=pe,
        pb=pb,
        peg=peg,
        dividend_yield=div_y,
        free_cash_flow_yield=fcf_y,
        debt_to_equity=dte,
        roe=roe,
        current_ratio=cur,
        operating_margin=opm,
        ev_to_ebitda=ev_e,
        raw=info,
    )

