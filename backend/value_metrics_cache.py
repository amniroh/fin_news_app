from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from value_metrics_provider_standard import fetch_standard_metrics
from value_metrics_store import query_standard_metrics, upsert_standard_metrics


class InMemoryTTLCache:
    def __init__(self, ttl_seconds: int = 1800) -> None:
        self.ttl_seconds = int(ttl_seconds)
        self._store: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        k = str(key).strip().upper()
        item = self._store.get(k)
        if not item:
            return None
        ts, val = item
        if (time.time() - ts) > float(self.ttl_seconds):
            self._store.pop(k, None)
            return None
        return val

    def set(self, key: str, val: Dict[str, Any]) -> None:
        k = str(key).strip().upper()
        self._store[k] = (time.time(), dict(val))

    def clear(self) -> None:
        self._store.clear()


def get_or_fetch_metrics(cache: InMemoryTTLCache, symbol: str, con: Any = None) -> Dict[str, Any]:
    sym = str(symbol).strip().upper()
    hit = cache.get(sym)
    if hit is not None:
        return dict(hit)

    if con is not None:
        rows = query_standard_metrics(con, symbols=[sym], provider="yfinance")
        if rows:
            out = dict(rows[0])
            cache.set(sym, out)
            return out

    out = fetch_standard_metrics(sym)
    if con is not None:
        upsert_standard_metrics(con, provider="yfinance", rows=[out])
    cache.set(sym, out)
    return out

