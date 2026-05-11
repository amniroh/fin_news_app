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
  - Tie-break across same end+duration filings: latest ``filed`` date.
Instant facts (balance sheet): facts without ``start``; tie-break by latest ``filed``.

**EPS (split-adjusted to today's share basis).** SEC ``companyfacts`` returns each fact
**as-filed**, in whatever per-share basis was current on the filing date. To reconcile against
present-day series (Macrotrends, brokerage charts), we divide the picked tag value by the
cumulative split ratio for splits with **ex-date strictly after the picked fact's ``filed``**.

  - If a quarter was restated in a later post-split filing (the picker prefers latest ``filed``),
    the picked value is already on the new basis and ``split_factor_after(filed) == 1.0`` →
    no adjustment.
  - If a quarter was never refiled after a later split (e.g. GOOGL 2017 Q1 vs the 2022 20:1
    split), the picked value is on the old basis and we divide by the post-filing split factor.

We prefer ``EarningsPerShareDiluted`` (min-duration quarter slice). If the tag is missing we
fall back to ``NetIncomeLoss / WeightedAverageNumberOfDilutedSharesOutstanding`` and use the
share-count fact's ``filed`` date as the split-adjustment reference (NI dollars are
split-invariant; the share-count basis determines the resulting per-share basis).
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
    """
    SEC EDGAR rejects requests without a contact-style ``User-Agent``. Set
    ``SEC_EDGAR_USER_AGENT`` in the environment to e.g. ``"value_metrics/1.0 you@example.com"``.
    The default contains an email-shaped placeholder so a fresh checkout still gets through
    SEC's UA filter (you should still configure a real address per their fair-use policy).
    """
    ua = (os.getenv("SEC_EDGAR_USER_AGENT") or "").strip()
    if not ua:
        ua = "value_metrics/1.0 set-SEC_EDGAR_USER_AGENT@example.com"
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


def _ticker_map_cache_path() -> Path:
    base = os.getenv("SEC_TICKER_MAP_CACHE_PATH")
    if base:
        return Path(base).expanduser()
    here = Path(__file__).resolve().parent
    return here / "data" / "sec_companyfacts_cache" / "company_tickers.json"


def _parse_ticker_map_payload(data: Any) -> Dict[str, str]:
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
    return out


def _load_ticker_cik_map() -> Dict[str, str]:
    """
    Ticker → 10-digit CIK map.

    Priority: in-memory (24h) → live SEC fetch → on-disk cache (best-effort).
    The disk cache lets backfills succeed when SEC briefly 403s, as long as a previous
    successful fetch persisted ``company_tickers.json`` next to the companyfacts cache.
    """
    global _TICKERS_CIKS_CACHE, _TICKERS_FETCH_TS
    now = time.time()
    if _TICKERS_CIKS_CACHE is not None and (now - _TICKERS_FETCH_TS) < 86400:
        return _TICKERS_CIKS_CACHE

    cache_path = _ticker_map_cache_path()
    url = "https://www.sec.gov/files/company_tickers.json"
    out: Dict[str, str] = {}
    fetch_err: Optional[BaseException] = None
    try:
        data = _http_get_json(url, timeout=60)
        out = _parse_ticker_map_payload(data)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass
        time.sleep(0.15)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        fetch_err = e

    if not out:
        try:
            if cache_path.is_file():
                disk_data = json.loads(cache_path.read_text(encoding="utf-8"))
                out = _parse_ticker_map_payload(disk_data)
        except Exception:
            pass

    if not out:
        raise RuntimeError(
            f"Failed to load SEC ticker map (live fetch and disk cache both empty): {fetch_err}"
        )

    _TICKERS_CIKS_CACHE = out
    _TICKERS_FETCH_TS = now
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


def _flow_pick(
    facts: Dict[str, Any],
    tag: str,
    end: str,
    *,
    annual: bool,
    unit_preference: Sequence[str],
) -> Optional[Dict[str, Any]]:
    """Pick the full fact dict from the first tag/unit that has data for ``end``."""
    for unit in unit_preference:
        arr = _facts_for_tag_units(facts, tag, unit)
        at_end = [x for x in arr if str(x.get("end") or "") == end]
        if not at_end:
            continue
        picked = pick_flow_fact_same_end(at_end, annual=annual)
        if picked and picked.get("val") is not None:
            return picked
    return None


def _flow_value(
    facts: Dict[str, Any],
    tag: str,
    end: str,
    *,
    annual: bool,
    unit_preference: Sequence[str],
) -> Optional[float]:
    p = _flow_pick(facts, tag, end, annual=annual, unit_preference=unit_preference)
    return float(p["val"]) if (p and p.get("val") is not None) else None


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


def _weighted_average_diluted_shares_pick(
    facts: Dict[str, Any], end: str, *, annual: bool
) -> Optional[Dict[str, Any]]:
    """Pick the diluted weighted-average shares fact for the same fiscal slice."""
    unit_pref = ("shares", "pure", "Shares")
    for tag in (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingDiluted",
        "WeightedAverageNumberOfSharesOutstandingBasicAndDiluted",
    ):
        p = _flow_pick(facts, tag, end, annual=annual, unit_preference=unit_pref)
        if p is not None and p.get("val") is not None and float(p["val"]) > 1e-6:
            return p
    return None


def _weighted_average_diluted_shares(facts: Dict[str, Any], end: str, *, annual: bool) -> Optional[float]:
    p = _weighted_average_diluted_shares_pick(facts, end, annual=annual)
    return float(p["val"]) if (p and p.get("val") is not None) else None


def _tag_eps_diluted_pick(facts: Dict[str, Any], end: str, *, annual: bool) -> Optional[Dict[str, Any]]:
    p = _flow_pick(
        facts,
        "EarningsPerShareDiluted",
        end,
        annual=annual,
        unit_preference=("USD/shares", "USD/shares1"),
    )
    if p is None:
        p = _flow_pick(
            facts,
            "EarningsPerShareBasic",
            end,
            annual=annual,
            unit_preference=("USD/shares", "USD/shares1"),
        )
    return p


def _filed_date(fact: Optional[Dict[str, Any]]) -> Optional[str]:
    if not fact:
        return None
    f = str(fact.get("filed") or "").strip()
    return f[:10] if f else None


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
    sym_u = str(symbol).strip().upper()
    revenue = _revenue_value(facts, end, annual=annual)
    op_inc = _flow_value(facts, "OperatingIncomeLoss", end, annual=annual, unit_preference=("USD",))

    ni_pick = _flow_pick(facts, "NetIncomeLoss", end, annual=annual, unit_preference=("USD",))
    net_income = float(ni_pick["val"]) if (ni_pick and ni_pick.get("val") is not None) else None

    shares_pick = _weighted_average_diluted_shares_pick(facts, end, annual=annual)
    wa_dil_shares = (
        float(shares_pick["val"]) if (shares_pick and shares_pick.get("val") is not None) else None
    )

    eps_pick = _tag_eps_diluted_pick(facts, end, annual=annual)
    eps_tag = float(eps_pick["val"]) if (eps_pick and eps_pick.get("val") is not None) else None
    eps_tag_filed = _filed_date(eps_pick)

    eps_ni_over_wa: Optional[float] = None
    if net_income is not None and wa_dil_shares is not None and abs(float(wa_dil_shares)) > 1e-9:
        eps_ni_over_wa = float(net_income) / float(wa_dil_shares)

    eps: Optional[float]
    eps_method: str
    split_factor_after_filed: float = 1.0
    split_ref_filed: Optional[str] = None

    # Prefer GAAP diluted EPS tag — already published as a per-share number, no shares-basis
    # reconstruction needed. Forward-split adjust by splits with ex-date strictly after the
    # picked fact's ``filed``: if the latest filing was post-split, the value is on the new
    # basis (S=1.0); if it was never refiled after a later split, divide by the split factor.
    if eps_tag is not None:
        split_ref_filed = eps_tag_filed or str(end)[:10]
        split_factor_after_filed = _yf_split_factor_after(sym_u, split_ref_filed)
        S = float(split_factor_after_filed) if split_factor_after_filed and split_factor_after_filed > 0 else 1.0
        eps = float(eps_tag) / S
        eps_method = "eps_tag_split_adjusted_after_filed"
    elif eps_ni_over_wa is not None:
        # NI dollars are split-invariant, so the per-share basis follows the picked share fact.
        split_ref_filed = _filed_date(shares_pick) or _filed_date(ni_pick) or str(end)[:10]
        split_factor_after_filed = _yf_split_factor_after(sym_u, split_ref_filed)
        S = float(split_factor_after_filed) if split_factor_after_filed and split_factor_after_filed > 0 else 1.0
        eps = float(eps_ni_over_wa) / S
        eps_method = "ni_over_wa_shares_split_adjusted_after_filed"
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
    if net_income is not None and eps is not None and abs(float(eps)) > 1e-15:
        shares = float(net_income) / float(eps)
    elif wa_dil_shares is not None:
        shares = float(wa_dil_shares)
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
        "eps_tag_filed": eps_tag_filed,
        "eps_ni_over_weighted_avg_diluted_shares": eps_ni_over_wa,
        "weighted_avg_diluted_shares": wa_dil_shares,
        "shares_filed": _filed_date(shares_pick),
        "ni_filed": _filed_date(ni_pick),
        "split_ref_filed": split_ref_filed,
        "split_factor_after_filed": split_factor_after_filed,
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
