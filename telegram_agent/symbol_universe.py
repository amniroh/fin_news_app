from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, List, Optional, Sequence, Set

logger = logging.getLogger(__name__)


def normalize_symbol(sym: str) -> str:
    s = (sym or "").strip().upper().replace(" ", "")
    # yfinance share-class tickers often use dashes (BRK.B -> BRK-B)
    if "." in s and len(s) <= 8:
        s = s.replace(".", "-")
    return s


def _symbols_from_mixed_list(items: Sequence[Any]) -> List[str]:
    """List of ticker strings and/or objects like {\"ticker\": \"AAPL\"}."""
    out: List[str] = []
    for x in items:
        if isinstance(x, str) and x.strip():
            out.append(normalize_symbol(x))
        elif isinstance(x, dict):
            t = x.get("ticker") or x.get("symbol") or x.get("Ticker")
            if t is not None and str(t).strip():
                out.append(normalize_symbol(str(t)))
    return out


def _load_json_symbols(path: Path) -> List[str]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, list):
        syms = _symbols_from_mixed_list(data)
        if syms:
            return syms
        return [normalize_symbol(x) for x in data if isinstance(x, str) and str(x).strip()]
    if isinstance(data, dict):
        if "symbols" in data and isinstance(data["symbols"], list):
            syms = _symbols_from_mixed_list(data["symbols"])
            if syms:
                return syms
            return [normalize_symbol(x) for x in data["symbols"] if isinstance(x, str) and str(x).strip()]
        for key in ("tickers", "investments", "items"):
            if key in data and isinstance(data[key], list):
                syms = _symbols_from_mixed_list(data[key])
                if syms:
                    return syms
    raise ValueError(f"Unsupported universe JSON format in {path}")


def load_symbol_universe(cfg: dict) -> Optional[List[str]]:
    """
    Load fixed symbol universe (top-1000 list).

    Supported sources (checked in order):
    1) SYMBOL_UNIVERSE_ENV: comma-separated symbols
    2) SYMBOL_UNIVERSE_PATH: JSON file with either a list or {"symbols":[...]}
    """
    enabled = cfg.get("symbol_universe_enabled", False)
    if not enabled:
        return None

    env_list = (cfg.get("symbol_universe_env") or "").strip()
    if env_list:
        syms = [normalize_symbol(x) for x in env_list.split(",") if x.strip()]
        syms = [s for s in syms if s]
        return sorted(set(syms))

    path_raw = cfg.get("symbol_universe_path")
    if not path_raw:
        raise RuntimeError("symbol_universe_enabled=true but symbol_universe_path not set")
    path = Path(path_raw).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"symbol_universe_enabled=true but universe file not found: {path}"
        )
    return sorted(set(_load_json_symbols(path)))


def symbol_universe_set(cfg: dict) -> Optional[Set[str]]:
    try:
        syms = load_symbol_universe(cfg)
        if syms is None:
            return None
        return set(syms)
    except Exception as e:
        logger.warning("Failed to load symbol universe; running without it: %s", e)
        return None

