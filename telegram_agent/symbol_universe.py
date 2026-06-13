from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


def normalize_symbol(sym: str) -> str:
    s = (sym or "").strip().upper().replace(" ", "")
    # yfinance share-class tickers often use dashes (BRK.B -> BRK-B)
    if "." in s and len(s) <= 8:
        s = s.replace(".", "-")
    return s


def sp500_symbols_from_env(env_key: str = "SP500_SYMBOLS") -> List[str]:
    """
    Load S&P 500 constituent symbols from a comma-separated env var.
    This is primarily for CLI flags like --spy_symbols to run jobs over SP500.
    """
    raw = (os.getenv(env_key) or "").strip()
    if not raw:
        raise RuntimeError(f"{env_key} is not set")
    syms = [normalize_symbol(x) for x in raw.split(",") if str(x).strip()]
    return sorted(set([s for s in syms if s]))


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


def _load_json_symbols_with_priority(path: Path) -> List[Tuple[str, Optional[int]]]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, list):
        out: List[Tuple[str, Optional[int]]] = []
        for x in data:
            if isinstance(x, str) and x.strip():
                out.append((normalize_symbol(x), None))
            elif isinstance(x, dict):
                t = x.get("ticker") or x.get("symbol") or x.get("Ticker")
                if t is None or not str(t).strip():
                    continue
                pr = x.get("priority")
                try:
                    pri = int(pr) if pr is not None else None
                except Exception:
                    pri = None
                out.append((normalize_symbol(str(t)), pri))
        if out:
            return out
        return []
    if isinstance(data, dict):
        if "symbols" in data and isinstance(data["symbols"], list):
            out: List[Tuple[str, Optional[int]]] = []
            for x in data["symbols"]:
                if isinstance(x, str) and x.strip():
                    out.append((normalize_symbol(x), None))
                elif isinstance(x, dict):
                    t = x.get("ticker") or x.get("symbol") or x.get("Ticker")
                    if t is None or not str(t).strip():
                        continue
                    pr = x.get("priority")
                    try:
                        pri = int(pr) if pr is not None else None
                    except Exception:
                        pri = None
                    out.append((normalize_symbol(str(t)), pri))
            if out:
                return out
            return []
        for key in ("tickers", "investments", "items"):
            if key in data and isinstance(data[key], list):
                out: List[Tuple[str, Optional[int]]] = []
                for x in data[key]:
                    if isinstance(x, str) and x.strip():
                        out.append((normalize_symbol(x), None))
                    elif isinstance(x, dict):
                        t = x.get("ticker") or x.get("symbol") or x.get("Ticker")
                        if t is None or not str(t).strip():
                            continue
                        pr = x.get("priority")
                        try:
                            pri = int(pr) if pr is not None else None
                        except Exception:
                            pri = None
                        out.append((normalize_symbol(str(t)), pri))
                if out:
                    return out
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

    max_pr = cfg.get("max_priority")
    try:
        max_pr_i = int(max_pr) if max_pr is not None else None
    except Exception:
        max_pr_i = None

    env_list = (cfg.get("symbol_universe_env") or "").strip()
    if env_list:
        syms = [normalize_symbol(x) for x in env_list.split(",") if x.strip()]
        syms = [s for s in syms if s]
        # env list doesn't have priority metadata; if max_priority is set,
        # we can't filter reliably, so we keep it as-is.
        return sorted(set(syms))

    path_raw = cfg.get("symbol_universe_path")
    if not path_raw:
        raise RuntimeError("symbol_universe_enabled=true but symbol_universe_path not set")
    path = Path(path_raw).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"symbol_universe_enabled=true but universe file not found: {path}"
        )
    syms_with_pr = _load_json_symbols_with_priority(path)
    if max_pr_i is not None:
        syms = [
            s
            for s, pr in syms_with_pr
            if (pr is None) or (int(pr) <= max_pr_i)
        ]
    else:
        syms = [s for s, _pr in syms_with_pr]
    return sorted(set([s for s in syms if s]))


def symbol_universe_set(cfg: dict) -> Optional[Set[str]]:
    try:
        syms = load_symbol_universe(cfg)
        if syms is None:
            return None
        return set(syms)
    except Exception as e:
        logger.warning("Failed to load symbol universe; running without it: %s", e)
        return None


def universe_priority_map(cfg: dict) -> Optional[Dict[str, Optional[int]]]:
    """
    Map canonical symbol -> priority from the JSON universe file, if available.
    Returns None when priority metadata cannot be loaded (universe off, env-only list, missing file).
    """
    if not cfg.get("symbol_universe_enabled"):
        return None
    if (cfg.get("symbol_universe_env") or "").strip():
        return None
    path_raw = cfg.get("symbol_universe_path")
    if not path_raw:
        return None
    path = Path(path_raw).expanduser()
    if not path.exists():
        return None
    try:
        pairs = _load_json_symbols_with_priority(path)
    except Exception as e:
        logger.warning("Could not load universe priorities from %s: %s", path, e)
        return None
    return {s: pr for s, pr in pairs}


def symbols_with_exact_priority(cfg: dict, priority: int) -> List[str]:
    """Symbols whose universe JSON lists ``priority`` exactly (excludes missing/null priority)."""
    pmap = universe_priority_map(cfg)
    if pmap is None:
        return []
    return sorted(s for s, pr in pmap.items() if pr == priority)


def default_typed_universe_path() -> Path:
    """JSON list with ``ticker`` + ``type`` fields (e.g. top1000_investments.json)."""
    root = Path(__file__).resolve().parent
    for name in ("top1000_investments.json", "data/symbol_universe_top1000.json"):
        p = root / name
        if p.is_file():
            return p
    return root / "top1000_investments.json"


def load_symbol_type_map(path: Optional[Path] = None) -> Dict[str, str]:
    """
    Return {CANONICAL_SYMBOL: type} from typed universe JSON (``type`` field per entry).
    """
    p = path or default_typed_universe_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: Dict[str, str] = {}
    items: List[Any] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("symbols", "tickers", "investments", "items"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    for x in items:
        if not isinstance(x, dict):
            continue
        t = x.get("ticker") or x.get("symbol")
        typ = x.get("type")
        if t and str(t).strip() and typ:
            out[normalize_symbol(str(t))] = str(typ).strip().lower()
    return out


def crypto_symbols_from_universe(path: Optional[Path] = None) -> Set[str]:
    return {s for s, t in load_symbol_type_map(path).items() if t == "crypto"}

