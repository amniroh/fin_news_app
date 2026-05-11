"""
Fundamentals via RapidAPI's Macrotrends Finance wrapper (Macrotrends.net has no official public API).

Subscribe to a RapidAPI product such as ``macrotrends-finance1``, set ``RAPIDAPI_KEY`` (or
``MACROTRENDS_RAPIDAPI_KEY``), and optionally ``MACROTRENDS_RAPIDAPI_HOST`` if your subscription uses a
different host slug than the default.

Typical endpoints (GET, JSON):
  - ``/financial-statements/{TICKER}`` — income, balance sheet, cash flow (quarterly/annual as returned)
  - ``/price-history/{TICKER}`` — OHLC history (shape varies by provider version)
  - ``/earnings-estimates/{TICKER}`` — EPS / estimates where available

See your RapidAPI dashboard for the exact paths supported by your subscription.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

import requests


DEFAULT_HOST = "macrotrends-finance1.p.rapidapi.com"
DEFAULT_BASE_URL = "https://macrotrends-finance1.p.rapidapi.com"


def macrotrends_rapidapi_key() -> str:
    k = (os.getenv("RAPIDAPI_KEY") or os.getenv("MACROTRENDS_RAPIDAPI_KEY") or "").strip()
    if not k:
        raise RuntimeError("Set RAPIDAPI_KEY or MACROTRENDS_RAPIDAPI_KEY (RapidAPI subscription key).")
    return k


def macrotrends_host() -> str:
    return (os.getenv("MACROTRENDS_RAPIDAPI_HOST") or DEFAULT_HOST).strip()


def macrotrends_base_url() -> str:
    return (os.getenv("MACROTRENDS_BASE_URL") or f"https://{macrotrends_host()}").strip().rstrip("/")


def normalize_ticker(symbol: str) -> str:
    """Yahoo-style tickers: BRK.B -> BRK-B; spaces removed."""
    s = (symbol or "").strip().upper().replace(" ", "")
    if "." in s and len(s) <= 8:
        s = s.replace(".", "-")
    return s


def _headers() -> Dict[str, str]:
    return {
        "X-RapidAPI-Key": macrotrends_rapidapi_key(),
        "X-RapidAPI-Host": macrotrends_host(),
        "Accept": "application/json",
    }


def macrotrends_get(
    path: str,
    *,
    timeout_s: float = 60.0,
    max_retries: int = 3,
) -> Any:
    """
    GET ``{base_url}{path}`` with RapidAPI headers. ``path`` must start with ``/``.
    """
    p = path if str(path).startswith("/") else f"/{path}"
    url = f"{macrotrends_base_url()}{p}"
    last_err: Optional[Exception] = None
    for attempt in range(int(max_retries)):
        try:
            r = requests.get(url, headers=_headers(), timeout=timeout_s)
            if r.status_code == 429 and attempt < max_retries - 1:
                time.sleep(1.5 * (2**attempt))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(0.5 * (2**attempt))
    if last_err:
        raise last_err
    raise RuntimeError("macrotrends_get failed")


def fetch_financial_statements(symbol: str) -> Any:
    sym = normalize_ticker(symbol)
    return macrotrends_get(f"/financial-statements/{sym}")


def fetch_price_history(symbol: str) -> Any:
    sym = normalize_ticker(symbol)
    return macrotrends_get(f"/price-history/{sym}")


def fetch_earnings_estimates(symbol: str) -> Any:
    sym = normalize_ticker(symbol)
    return macrotrends_get(f"/earnings-estimates/{sym}")


def fetch_all_fundamentals_bundle(symbol: str) -> Dict[str, Any]:
    """
    Fetch all documented bundle endpoints for one ticker (3 RapidAPI calls).
    Fail-soft: if one endpoint errors, store ``{"error": "..."}`` for that key.
    """
    sym = normalize_ticker(symbol)
    out: Dict[str, Any] = {"symbol": sym}

    for key, fn in (
        ("financial_statements", fetch_financial_statements),
        ("price_history", fetch_price_history),
        ("earnings_estimates", fetch_earnings_estimates),
    ):
        try:
            out[key] = fn(sym)
        except Exception as e:
            out[key] = {"error": str(e)}

    return out


def bundle_to_json_bytes(bundle: Dict[str, Any]) -> bytes:
    return json.dumps(bundle, indent=0, default=str).encode("utf-8")
