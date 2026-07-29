"""Configurable category allowlist + matching for prediction-market signals."""

from __future__ import annotations

import os
import re
from typing import Iterable, List, Optional, Sequence, Set

# Canonical buckets the product cares about (case-insensitive matching).
DEFAULT_CATEGORY_ALLOWLIST: tuple[str, ...] = (
    "politics",
    "finance",
    "crypto",
    "economy",
    "tech",
    "elections",
    "iran",
)

# Map noisy exchange labels / keywords → canonical bucket.
_ALIAS_TO_CANONICAL: dict[str, str] = {
    "politics": "politics",
    "political": "politics",
    "us-current-affairs": "politics",
    "world": "politics",
    "geopolitics": "politics",
    "international": "politics",
    "elections": "elections",
    "election": "elections",
    "main election": "elections",
    "global elections": "elections",
    "crypto": "crypto",
    "cryptocurrency": "crypto",
    "bitcoin": "crypto",
    "ethereum": "crypto",
    "defi": "crypto",
    "finance": "finance",
    "financials": "finance",
    "financial": "finance",
    "markets": "finance",
    "economy": "economy",
    "economics": "economy",
    "macro": "economy",
    "fed": "economy",
    "inflation": "economy",
    "tech": "tech",
    "technology": "tech",
    "science and technology": "tech",
    "ai": "tech",
    "iran": "iran",
}

# Title/tag keyword hints when exchange category is missing or generic ("binary").
_TITLE_HINTS: tuple[tuple[str, str], ...] = (
    ("iran", "iran"),
    ("tehran", "iran"),
    ("election", "elections"),
    ("president", "elections"),
    ("prime minister", "elections"),
    ("senate", "elections"),
    ("congress", "elections"),
    ("ballot", "elections"),
    ("vote", "elections"),
    ("bitcoin", "crypto"),
    (" eth ", "crypto"),
    ("ethereum", "crypto"),
    ("crypto", "crypto"),
    ("btc", "crypto"),
    ("solana", "crypto"),
    ("defi", "crypto"),
    ("fed ", "economy"),
    ("fomc", "economy"),
    ("interest rate", "economy"),
    ("inflation", "economy"),
    ("gdp", "economy"),
    ("recession", "economy"),
    ("cpi", "economy"),
    ("nasdaq", "finance"),
    ("s&p", "finance"),
    ("stock", "finance"),
    ("treasury", "finance"),
    ("ai ", "tech"),
    ("openai", "tech"),
    ("nvidia", "tech"),
    ("apple", "tech"),
    ("google", "tech"),
    ("microsoft", "tech"),
    ("trump", "politics"),
    ("biden", "politics"),
    ("nato", "politics"),
    ("ukraine", "politics"),
    ("china", "politics"),
    ("congress", "politics"),
    ("white house", "politics"),
)


def load_category_allowlist(raw: Optional[str] = None) -> Optional[List[str]]:
    """
    Parse allowlist from arg or ``PM_CATEGORY_ALLOWLIST`` env (comma-separated).

    Returns ``None`` when unset / empty / ``all`` — meaning fetch every category.
    """
    text = (raw if raw is not None else os.getenv("PM_CATEGORY_ALLOWLIST", "")).strip()
    if not text or text.lower() in ("*", "all", "any"):
        return None
    out: List[str] = []
    seen: Set[str] = set()
    for part in text.split(","):
        c = _canonicalize_token(part, keep_unknown=True)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out or None


# Labels that are not useful as category buckets.
_IGNORE_UNKNOWN: frozenset[str] = frozenset(
    {
        "binary",
        "unknown",
        "other",
        "misc",
        "miscellaneous",
        "all",
        "markets",
        "market",
        "yes",
        "no",
        "open",
        "closed",
        "settled",
    }
)


def _canonicalize_token(token: str, *, keep_unknown: bool = False) -> Optional[str]:
    t = re.sub(r"\s+", " ", (token or "").strip().lower())
    if not t:
        return None
    if t in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[t]
    # slug-style
    t2 = t.replace("-", " ").replace("_", " ")
    if t2 in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[t2]
    # already a known canonical
    if t in DEFAULT_CATEGORY_ALLOWLIST or t2 in DEFAULT_CATEGORY_ALLOWLIST:
        return t if t in DEFAULT_CATEGORY_ALLOWLIST else t2
    if keep_unknown:
        slug = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
        if not slug or slug in _IGNORE_UNKNOWN:
            return None
        return slug
    return None


def infer_categories(
    *,
    title: str = "",
    category: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    extra_labels: Optional[Sequence[str]] = None,
) -> List[str]:
    """Return sorted unique canonical categories for a market."""
    found: Set[str] = set()
    for label in (category, *(tags or ()), *(extra_labels or ())):
        if label is None:
            continue
        # Split comma-separated stored values.
        parts = [p for p in str(label).split(",")] if "," in str(label) else [str(label)]
        for part in parts:
            c = _canonicalize_token(part, keep_unknown=True)
            if c:
                found.add(c)
    blob = f" {(title or '').lower()} "
    for needle, canon in _TITLE_HINTS:
        if needle in blob:
            found.add(canon)
    return sorted(found)


def matches_allowlist(categories: Iterable[str], allowlist: Sequence[str]) -> bool:
    allow = {a.lower() for a in allowlist}
    return any(c.lower() in allow for c in categories)


def primary_category(categories: Sequence[str], allowlist: Sequence[str]) -> Optional[str]:
    """Prefer an allowlisted category for storage/display."""
    allow = [a.lower() for a in allowlist]
    cats = [c.lower() for c in categories]
    for a in allow:
        if a in cats:
            return a
    return cats[0] if cats else None
