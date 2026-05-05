"""
Quarterly / annual fundamentals from SEC XBRL Company Facts API.

Important: many GAAP flow concepts (EPS, net income, revenue, …) publish **multiple**
facts for the same ``end`` date with different ``start`` dates — e.g. Apple FY2012 Q2
(`end` 2012-03-31) includes both a ~188-day **YTD** slice (EPS 26.17) and a ~90-day
**quarter** slice (EPS 12.30). Picking the wrong one inflates “quarterly” EPS and revenue.

Rule used here:
  - **Quarterly** flow metrics: among facts with the same ``end``, choose the fact with the
    **smallest** ``(end - start)`` duration (single quarter).
  - **Annual** flow metrics: among facts with ``fp == 'FY'`` and the same ``end``, choose the
    **largest** duration (full fiscal year).
Instant facts (balance sheet): facts without ``start``; tie-break by latest ``filed``.

**EPS (quarterly):** prefer ``NetIncomeLoss / WeightedAverageNumberOfDilutedSharesOutstanding``
(min-duration slice, same ``end`` as other flow facts). That keeps numerator and denominator on one
basis and avoids mixed restated-vs-pre-split ``EarningsPerShareDiluted`` tags. If weighted-average
share tags are missing, fall back to the diluted EPS tag plus split reconciliation (see
``_reconcile_tag_eps_with_splits``), not a blind multiply/divide for every symbol.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from value_metrics_stock_splits import split_factor_after as _yf_split_factor_after

_TICKERS_CIKS_CACHE: Optional[Dict[str, str]] = None
_TICKERS_FETCH_TS: float = 0.0


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sec_headers() -> Dict[str, str]:
    ua = (os.getenv("SEC_EDGAR_USER_AGENT") or "").strip()
    if not ua:
        ua = "market_analysis/1.0 (contact: set SEC_EDGAR_USER_AGENT in .env)"
    # Avoid gzip so ``urlopen`` returns plain JSON without manual decompress.
    return {"User-Agent": ua, "Accept-Encoding": "identity"}


def _duration_days(f: Dict[str, Any]) -> Optional[int]:
    s, e = f.get("start"), f.get("end")
    if not s or not e:
        return None
    try:
        return (datetime.fromisoformat(str(e)) - datetime.fromisoformat(str(s))).days
    except Exception:
        return None


def pick_flow_fact_same_end(facts: Sequence[Dict[str, Any]], *, annual: bool) -> Optional[Dict[str, Any]]:
    """Prefer narrow duration for quarters; widest for FY annual metrics.

    When multiple facts share the same duration (duplicate filings), prefer the latest ``filed`` date.
    """
    rows = [f for f in facts if f.get("end") and f.get("start")]
    if not rows:
        return facts[0] if facts else None
    if annual:
        max_d = max((_duration_days(f) or -1) for f in rows)
        tier = [f for f in rows if (_duration_days(f) or -1) == max_d]
        return max(tier, key=lambda f: f.get("filed") or "")
    min_d = min((_duration_days(f) if _duration_days(f) is not None else 10**9) for f in rows)
    tier = [f for f in rows if (_duration_days(f) if _duration_days(f) is not None else 10**9) == min_d]
    return max(tier, key=lambda f: f.get("filed") or "")


def pick_instant_fact_same_end(facts: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    instant = [f for f in facts if f.get("end") and not f.get("start")]
    if instant:
        return max(instant, key=lambda f: f.get("filed") or "")
    return facts[0] if facts else None


def _http_get_json(url: str, *, timeout: float = 120) -> Any:
    req = Request(url, headers=_sec_headers())
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B:
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _load_ticker_cik_map() -> Dict[str, str]:
    global _TICKERS_CIKS_CACHE, _TICKERS_FETCH_TS
    now = time.time()
    if _TICKERS_CIKS_CACHE is not None and (now - _TICKERS_FETCH_TS) < 86400:
        return _TICKERS_CIKS_CACHE
    url = "https://www.sec.gov/files/company_tickers.json"
    try:
        data = _http_get_json(url, timeout=60)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Failed to load SEC ticker map: {e}") from e
    rows: List[Dict[str, Any]]
    if isinstance(data, dict):
        rows = [v for v in data.values() if isinstance(v, dict)]
    elif isinstance(data, list):
        rows = [x for x in data if isinstance(x, dict)]
    else:
        rows = []
    out: Dict[str, str] = {}
    for row in rows:
        t = str(row.get("ticker") or "").strip().upper()
        cik = row.get("cik_str")
        if t and cik is not None:
            out[t] = str(int(cik)).zfill(10)
    _TICKERS_CIKS_CACHE = out
    _TICKERS_FETCH_TS = now
    time.sleep(0.15)
    return out


def _cik_for_symbol(symbol: str) -> str:
    m = _load_ticker_cik_map()
    sym = str(symbol).strip().upper()
    if sym not in m:
        raise ValueError(f"Unknown ticker for SEC CIK mapping: {sym}")
    return m[sym]


def _fetch_company_facts(cik: str) -> Dict[str, Any]:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        data = _http_get_json(url, timeout=120)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        raise RuntimeError(f"SEC companyfacts request failed: {e}") from e
    time.sleep(0.12)
    return data


def _companyfacts_cache_path(cache_dir: Path, cik: str) -> Path:
    return Path(cache_dir) / f"CIK{str(cik).zfill(10)}.companyfacts.json"


def _load_companyfacts_from_cache(cache_dir: Path, cik: str) -> Optional[Dict[str, Any]]:
    try:
        p = _companyfacts_cache_path(cache_dir, cik)
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_companyfacts_to_cache(cache_dir: Path, cik: str, facts_json: Dict[str, Any]) -> None:
    try:
        d = Path(cache_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = _companyfacts_cache_path(d, cik)
        p.write_text(json.dumps(facts_json), encoding="utf-8")
    except Exception:
        # Cache is best-effort; never fail the pipeline because of it.
        return


def _units_for_tag(facts_response: Dict[str, Any], tag: str) -> Dict[str, List[Dict[str, Any]]]:
    us_gaap = (facts_response.get("facts") or {}).get("us-gaap") or {}
    node = us_gaap.get(tag) or {}
    return dict(node.get("units") or {})


def _facts_for_tag_units(facts_response: Dict[str, Any], tag: str, unit: str) -> List[Dict[str, Any]]:
    u = _units_for_tag(facts_response, tag)
    return list(u.get(unit) or [])


def _collect_quarterly_end_dates(facts: Dict[str, Any], start_date: str, end_date: str) -> Set[str]:
    """Fiscal quarter ``end`` dates in range (from EPS and/or net income Q1–Q4 facts)."""
    ends: Set[str] = set()

    def _pull(tag: str, units: Tuple[str, ...]) -> None:
        for unit in units:
            for f in _facts_for_tag_units(facts, tag, unit):
                if str(f.get("fp") or "") not in ("Q1", "Q2", "Q3", "Q4"):
                    continue
                e = f.get("end")
                if not e:
                    continue
                es = str(e)
                if es >= start_date and es <= end_date:
                    ends.add(es)

    _pull("EarningsPerShareDiluted", ("USD/shares", "USD/shares1"))
    _pull("EarningsPerShareBasic", ("USD/shares", "USD/shares1"))
    _pull("NetIncomeLoss", ("USD",))
    _pull("WeightedAverageNumberOfDilutedSharesOutstanding", ("shares",))
    _pull("WeightedAverageNumberOfSharesOutstandingDiluted", ("shares",))
    _pull("WeightedAverageNumberOfSharesOutstandingBasicAndDiluted", ("shares",))
    return ends


def _collect_fy_end_dates(facts: Dict[str, Any], start_date: str, end_date: str) -> Set[str]:
    ends: Set[str] = set()
    for f in _facts_for_tag_units(facts, "NetIncomeLoss", "USD"):
        if str(f.get("fp") or "") != "FY":
            continue
        e = f.get("end")
        if e and str(e) >= start_date and str(e) <= end_date:
            ends.add(str(e))
    return ends


def _flow_value(
    facts: Dict[str, Any],
    tag: str,
    end: str,
    *,
    annual: bool,
    unit_preference: Sequence[str],
) -> Optional[float]:
    """Pick value from first tag that has data for ``end``."""
    for unit in unit_preference:
        arr = _facts_for_tag_units(facts, tag, unit)
        at_end = [x for x in arr if str(x.get("end") or "") == end]
        if not at_end:
            continue
        picked = pick_flow_fact_same_end(at_end, annual=annual)
        if picked and picked.get("val") is not None:
            return float(picked["val"])
    return None


def _instant_value(facts: Dict[str, Any], tag: str, end: str, unit: str = "USD") -> Optional[float]:
    arr = _facts_for_tag_units(facts, tag, unit)
    at_end = [x for x in arr if str(x.get("end") or "") == end]
    if not at_end:
        return None
    picked = pick_instant_fact_same_end(at_end)
    if picked and picked.get("val") is not None:
        return float(picked["val"])
    return None


def _revenue_value(facts: Dict[str, Any], end: str, *, annual: bool) -> Optional[float]:
    for tag in ("Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerExcludingAssessedTax"):
        v = _flow_value(facts, tag, end, annual=annual, unit_preference=("USD",))
        if v is not None:
            return v
    return None


def _debt_value(facts: Dict[str, Any], end: str) -> Optional[float]:
    lt = _instant_value(facts, "LongTermDebt", end) or 0.0
    cur = _instant_value(facts, "DebtCurrent", end) or 0.0
    if lt == 0.0 and cur == 0.0:
        # Some filers use different axes
        alt = _instant_value(facts, "LongTermDebtNoncurrent", end)
        if alt is not None:
            lt = alt
    s = lt + cur
    return s if s != 0.0 else None


def _free_cash_flow_value(facts: Dict[str, Any], end: str, *, annual: bool) -> Optional[float]:
    op = _flow_value(
        facts,
        "NetCashProvidedByUsedInOperatingActivities",
        end,
        annual=annual,
        unit_preference=("USD",),
    )
    capex_tags = (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForPropertyPlantAndEquipment",
        "PaymentsForProceedsFromProductiveAssets",
    )
    capex = None
    for tag in capex_tags:
        v = _flow_value(facts, tag, end, annual=annual, unit_preference=("USD",))
        if v is not None:
            capex = abs(v)
            break
    if op is None:
        return None
    if capex is None:
        return None
    return op - capex


def _weighted_average_diluted_shares(facts: Dict[str, Any], end: str, *, annual: bool) -> Optional[float]:
    """
    Diluted weighted-average shares for the same fiscal slice as other flow facts.

    Prefer diluted-specific tags; fall back to combined basic+diluted only if needed.
    Units vary by filer (usually ``shares``).
    """
    unit_pref = ("shares", "pure", "Shares")
    for tag in (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingDiluted",
        "WeightedAverageNumberOfSharesOutstandingBasicAndDiluted",
    ):
        v = _flow_value(facts, tag, end, annual=annual, unit_preference=unit_pref)
        if v is not None and float(v) > 1e-6:
            return float(v)
    return None


def _tag_eps_diluted(facts: Dict[str, Any], end: str, *, annual: bool) -> Optional[float]:
    eps = _flow_value(
        facts,
        "EarningsPerShareDiluted",
        end,
        annual=annual,
        unit_preference=("USD/shares", "USD/shares1"),
    )
    if eps is None:
        eps = _flow_value(
            facts,
            "EarningsPerShareBasic",
            end,
            annual=annual,
            unit_preference=("USD/shares", "USD/shares1"),
        )
    return eps


def _reconcile_tag_eps_with_splits(*, tag_eps: float, split_factor_after_end: float) -> Tuple[float, str]:
    """
    SEC ``EarningsPerShareDiluted`` is sometimes on a pre-split per-share basis and sometimes already
    restated after later splits. When we cannot use net income / weighted diluted shares, pick between
    ``tag_eps`` and ``tag_eps / S`` using magnitude heuristics (works for common ~10–20:1 splits).
    """
    S = float(split_factor_after_end)
    if S <= 1.001 or tag_eps is None:
        return float(tag_eps), "tag_only"

    adj = float(tag_eps) / S
    a, b = float(tag_eps), adj

    # Already looks like post-split "street scale" quarterly EPS; adjusted candidate implausibly tiny.
    if abs(a) <= 12 and abs(b) < 0.2:
        return a, "tag_restated_scale"

    # Tag looks like pre-split dollars per old share; adjusted lands in normal quarterly range.
    if abs(a) >= 8 and abs(b) <= 12 and abs(b) > 1e-9:
        return b, "tag_presplit_scaled"

    # Very large tag, smaller adjusted in plausible EPS band.
    if abs(a) > 20 and abs(b) <= 15:
        return b, "tag_presplit_scaled"

    return a, "tag_default"


def _build_row(
    *,
    symbol: str,
    facts_json: Dict[str, Any],
    end: str,
    fy: Optional[int],
    fp: Optional[str],
    annual: bool,
) -> Dict[str, Any]:
    facts = facts_json
    revenue = _revenue_value(facts, end, annual=annual)
    op_inc = _flow_value(facts, "OperatingIncomeLoss", end, annual=annual, unit_preference=("USD",))
    net_income = _flow_value(facts, "NetIncomeLoss", end, annual=annual, unit_preference=("USD",))
    wa_dil_shares = _weighted_average_diluted_shares(facts, end, annual=annual)

    eps_tag = _tag_eps_diluted(facts, end, annual=annual)

    eps: Optional[float]
    eps_method: str
    split_factor_after_end = 1.0
    eps_tag_reconcile_reason: Optional[str] = None

    if net_income is not None and wa_dil_shares is not None and abs(float(wa_dil_shares)) > 1e-9:
        eps = float(net_income) / float(wa_dil_shares)
        eps_method = "net_income_over_weighted_avg_diluted_shares"
    elif eps_tag is not None:
        split_factor_after_end = _yf_split_factor_after(str(symbol).strip().upper(), str(end)[:10])
        eps, eps_tag_reconcile_reason = _reconcile_tag_eps_with_splits(
            tag_eps=float(eps_tag), split_factor_after_end=split_factor_after_end
        )
        eps_method = f"eps_tag_reconciled:{eps_tag_reconcile_reason}"
    else:
        eps = None
        eps_method = "missing"

    ebitda = _flow_value(facts, "EBITDA", end, annual=annual, unit_preference=("USD",))

    equity = _instant_value(facts, "StockholdersEquity", end)
    debt = _debt_value(facts, end)
    ca = _instant_value(facts, "AssetsCurrent", end)
    cl = _instant_value(facts, "LiabilitiesCurrent", end)
    cash = _instant_value(facts, "CashAndCashEquivalentsAtCarryingValue", end)
    if cash is None:
        cash = _instant_value(facts, "CashCashEquivalentsAndFederalFundsSold", end)

    fcf = _free_cash_flow_value(facts, end, annual=annual)

    shares: Optional[float]
    if wa_dil_shares is not None:
        shares = float(wa_dil_shares)
    elif net_income is not None and eps is not None and abs(float(eps)) > 1e-15:
        shares = float(net_income) / float(eps)
    else:
        shares = None

    cik_raw = facts.get("cik")
    cik_str = str(int(cik_raw)).zfill(10) if cik_raw is not None else None
    raw = {
        "source": "sec_edgar_companyfacts",
        "cik": cik_str,
        "fy": fy,
        "fp": fp,
        "end": end,
        "selection": "min_duration_flow_quarter" if not annual else "max_duration_flow_fy",
        "eps_method": eps_method,
        "eps_tag": eps_tag,
        "weighted_avg_diluted_shares": wa_dil_shares,
        "eps_tag_split_factor_after_end": split_factor_after_end,
        "eps_tag_reconcile_reason": eps_tag_reconcile_reason,
        "concepts": {
            "revenue": ["us-gaap:Revenues", "us-gaap:SalesRevenueNet"],
            "operating_income": "us-gaap:OperatingIncomeLoss",
            "net_income": "us-gaap:NetIncomeLoss",
            "eps": "us-gaap:EarningsPerShareDiluted",
            "weighted_avg_diluted_shares": "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
            "ebitda": "us-gaap:EBITDA",
            "equity": "us-gaap:StockholdersEquity",
            "debt": ["us-gaap:LongTermDebt", "us-gaap:DebtCurrent"],
            "current_assets": "us-gaap:AssetsCurrent",
            "current_liabilities": "us-gaap:LiabilitiesCurrent",
            "cash": "us-gaap:CashAndCashEquivalentsAtCarryingValue",
        },
    }

    return {
        "symbol": str(symbol).strip().upper(),
        "asof_date": end,
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


def fetch_sec_fundamentals_history(
    *,
    symbol: str,
    period: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    companyfacts_cache_dir: Optional[str | Path] = None,
    cache_mode: str = "use",  # use|refresh|only
) -> List[Dict[str, Any]]:
    """
    Fetch fundamentals from SEC Company Facts for ``symbol`` (US-listed).
    ``period`` is quarter | annual (maps to fiscal quarter / FY rows).
    """
    per = str(period).strip().lower()
    if per not in ("quarter", "annual"):
        raise ValueError("period must be quarter or annual")

    sym = str(symbol).strip().upper()
    cik = _cik_for_symbol(sym)

    mode = str(cache_mode or "use").strip().lower()
    cache_dir = Path(companyfacts_cache_dir) if companyfacts_cache_dir else None
    facts_json: Optional[Dict[str, Any]] = None

    if cache_dir is not None and mode in ("use", "only"):
        facts_json = _load_companyfacts_from_cache(cache_dir, cik)
    if facts_json is None and mode == "only":
        raise RuntimeError(f"SEC companyfacts cache miss for {sym} (CIK{cik}) in {cache_dir}")
    if facts_json is None or mode == "refresh":
        facts_json = _fetch_company_facts(cik)
        if cache_dir is not None:
            _save_companyfacts_to_cache(cache_dir, cik, facts_json)

    start_s = str(start_date or "1900-01-01")
    end_s = str(end_date or "2100-01-01")

    out: List[Dict[str, Any]] = []

    if per == "quarter":
        ends = _collect_quarterly_end_dates(facts_json, start_s, end_s)
        for end in sorted(ends):
            ni_all = [
                x for x in _facts_for_tag_units(facts_json, "NetIncomeLoss", "USD") if str(x.get("end") or "") == end
            ]
            picked_ni = pick_flow_fact_same_end(ni_all, annual=False) if ni_all else None
            fy = picked_ni.get("fy") if picked_ni else None
            fp = picked_ni.get("fp") if picked_ni else None
            row = _build_row(symbol=sym, facts_json=facts_json, end=end, fy=fy, fp=fp, annual=False)
            out.append(row)
    else:
        ends = _collect_fy_end_dates(facts_json, start_s, end_s)
        for end in sorted(ends):
            fy_facts = [
                x
                for x in _facts_for_tag_units(facts_json, "NetIncomeLoss", "USD")
                if str(x.get("end") or "") == end and str(x.get("fp") or "") == "FY"
            ]
            picked = pick_flow_fact_same_end(fy_facts, annual=True) if fy_facts else None
            fy = picked.get("fy") if picked else None
            row = _build_row(symbol=sym, facts_json=facts_json, end=end, fy=fy, fp="FY", annual=True)
            out.append(row)

    out.sort(key=lambda r: r["asof_date"])
    return out
