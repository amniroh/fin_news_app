from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests


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


def _get_json(url: str, *, timeout_s: int = 30) -> Any:
    r = requests.get(url, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def fetch_ratios_history(
    *,
    api_key: str,
    symbol: str,
    period: str,  # annual|quarter
    start_date: Optional[str] = None,  # YYYY-MM-DD
    end_date: Optional[str] = None,  # YYYY-MM-DD
) -> List[Dict[str, Any]]:
    """
    Uses Financial Modeling Prep:
      /api/v3/ratios/{symbol}?period=quarter|annual&apikey=...

    Returns list of points with a subset mapped into our canonical metric schema.
    """
    sym = str(symbol).strip().upper()
    per = str(period).strip().lower()
    if per not in ("annual", "quarter"):
        raise ValueError("period must be annual or quarter")
    key = str(api_key).strip()
    if not key:
        raise ValueError("missing FMP api key")

    urls = [
        f"https://financialmodelingprep.com/stable/ratios?symbol={sym}&period={per}&apikey={key}",
        f"https://financialmodelingprep.com/api/v3/ratios/{sym}?period={per}&apikey={key}",
    ]
    data: Any = []
    last_err: Optional[Exception] = None
    for url in urls:
        try:
            data = _get_json(url)
            if isinstance(data, list):
                break
        except Exception as e:
            last_err = e
            data = []
    if not isinstance(data, list):
        if last_err:
            raise last_err
        return []

    out: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        d = str(row.get("date") or "").strip()
        if not d:
            continue
        if start_date and d < str(start_date):
            continue
        if end_date and d > str(end_date):
            continue

        pe = _f(row.get("priceEarningsRatio")) or _f(row.get("peRatio"))
        pb = _f(row.get("priceToBookRatio")) or _f(row.get("priceBookValueRatio"))
        peg = _f(row.get("priceEarningsToGrowthRatio")) or _f(row.get("pegRatio"))
        div_y = _f(row.get("dividendYield"))
        if div_y is not None and div_y > 1:
            div_y = div_y / 100.0

        # FCF yield: direct field or inverse of price-to-FCF multiple.
        fcf_y = _f(row.get("freeCashFlowYield"))
        if fcf_y is None:
            pfcf = _f(row.get("priceToFreeCashFlowsRatio")) or _f(row.get("priceToFreeCashFlowRatio"))
            if pfcf is not None and pfcf > 0:
                fcf_y = 1.0 / pfcf

        dte = (
            _f(row.get("debtEquityRatio"))
            or _f(row.get("debtToEquity"))
            or _f(row.get("debtEquity"))
        )
        roe = _f(row.get("returnOnEquity"))
        cur = _f(row.get("currentRatio"))
        opm = _f(row.get("operatingProfitMargin")) or _f(row.get("operatingMargin"))
        ev_e = _f(row.get("enterpriseValueMultiple")) or _f(row.get("evToEbitda"))

        # Map FMP ratio fields into our 10 metric schema where possible.
        # Note: FMP names can differ; keep raw payload as audit trail.
        out.append(
            {
                "symbol": sym,
                "asof_date": d,
                "fetched_ts_utc": _utcnow_iso(),
                "pe": pe,
                "pb": pb,
                "peg": peg,
                "dividend_yield": div_y,
                "free_cash_flow_yield": fcf_y,
                "debt_to_equity": dte,
                "roe": roe,
                "current_ratio": cur,
                "operating_margin": opm,
                "ev_to_ebitda": ev_e,
                "raw": row,
            }
        )

    # Oldest first
    out.sort(key=lambda x: x["asof_date"])
    return out

