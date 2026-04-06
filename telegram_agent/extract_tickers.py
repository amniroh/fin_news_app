"""Heuristic ticker / symbol extraction from news text."""
from __future__ import annotations

import re
from typing import List, Optional, Set, Tuple

# Common English words that look like tickers (avoid false positives)
_DENY = {
    "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN", "HER", "WAS", "ONE", "OUR", "OUT", "DAY", "GET",
    "HAS", "HIM", "HIS", "HOW", "ITS", "LET", "MAY", "NEW", "NOW", "OLD", "SEE", "TWO", "WHO", "BOY", "DID",
    "CAR", "EAT", "END", "FEW", "GOT", "HAD", "OWN", "RAN", "SAW", "SAY", "SHE", "TOO", "TRY", "USE", "WAY",
    "YET", "BIG", "PUT", "SET", "RUN", "TOP", "LOW", "HIGH", "OPEN", "LONG", "HOLD", "BUY", "SELL", "EPS", "CEO",
    "IPO", "ETF", "USA", "UK", "EU", "GDP", "CPI", "FED", "SEC", "ATH", "LOL",
}

# $TICKER
# $TICKER (supports some punctuation like dot variants e.g. $BRK.B)
_RE_CASH = re.compile(r"\$([A-Z]{1,5}(?:\.[A-Z]{1})?)\b")
# Word boundary 1-5 uppercase letters with optional dot suffix (e.g. BRK.B)
_RE_UPPER = re.compile(r"\b([A-Z]{1,5}(?:\.[A-Z]{1})?)\b")
# Share-class tickers with dash (e.g. BRK-B, RDS-A)
_RE_DASH = re.compile(r"\b([A-Z]{1,5}-[A-Z]{1,2})\b")
# Crypto pairs BTC-USD, ETH-USD
_RE_CRYPTO = re.compile(r"\b([A-Z]{2,10}-USD)\b", re.I)


def normalize_symbol(raw: str) -> str:
    s = (raw or "").strip().upper().replace(" ", "")
    # yfinance share-class tickers often use dashes
    if "." in s and len(s) <= 8:
        s = s.replace(".", "-")
    return s


def extract_symbols_from_text(
    text: str, *, allowed_symbols: Optional[Set[str]] = None
) -> List[Tuple[str, str, float]]:
    """
    Returns list of (symbol, mention_type, confidence).
    """
    if not text:
        return []
    out: List[Tuple[str, str, float]] = []
    seen: Set[str] = set()

    def add(sym: str, mtype: str, conf: float) -> None:
        sym = normalize_symbol(sym)
        if not sym or sym in seen:
            return
        if sym in _DENY and mtype != "cash":
            return
        seen.add(sym)
        out.append((sym, mtype, conf))

    for m in _RE_CASH.finditer(text):
        add(m.group(1), "cash", 0.95)
    for m in _RE_CRYPTO.finditer(text):
        add(m.group(1).upper(), "crypto_pair", 0.85)
    for m in _RE_DASH.finditer(text):
        add(m.group(1), "regex", 0.40)
    for m in _RE_UPPER.finditer(text):
        add(m.group(1), "regex", 0.45)

    if allowed_symbols is not None:
        out = [(s, t, c) for (s, t, c) in out if s in allowed_symbols]
    return out
