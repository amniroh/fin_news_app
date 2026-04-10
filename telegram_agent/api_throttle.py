"""Small utilities for batching + rate limiting + retry/backoff for HTTP API collectors."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


@dataclass
class RateLimiter:
    """
    Simple wall-clock limiter.

    - min_interval_seconds: minimum time between calls (global for this limiter instance)
    - jitter_seconds: adds random jitter to avoid thundering herd
    """

    min_interval_seconds: float = 0.0
    jitter_seconds: float = 0.0
    _last_ts: float = 0.0

    def wait(self) -> None:
        now = time.time()
        dt = now - (self._last_ts or 0.0)
        need = max(0.0, float(self.min_interval_seconds) - dt)
        if need > 0:
            time.sleep(need)
        if self.jitter_seconds and self.jitter_seconds > 0:
            time.sleep(random.random() * float(self.jitter_seconds))
        self._last_ts = time.time()


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_retries: int = 3,
    base_sleep_seconds: float = 1.0,
    max_sleep_seconds: float = 30.0,
    should_retry: Optional[Callable[[Exception], bool]] = None,
) -> T:
    """
    Retry `fn` with exponential backoff.

    `should_retry` decides whether an exception is retryable.
    """
    tries = 0
    while True:
        try:
            return fn()
        except Exception as e:
            tries += 1
            if tries > int(max_retries):
                raise
            if should_retry is not None and not should_retry(e):
                raise
            sleep_s = min(float(max_sleep_seconds), float(base_sleep_seconds) * (2 ** (tries - 1)))
            # jitter
            sleep_s *= 0.7 + 0.6 * random.random()
            time.sleep(sleep_s)


def chunked(xs: list[str], n: int) -> list[list[str]]:
    n = max(1, int(n))
    return [xs[i : i + n] for i in range(0, len(xs), n)]

