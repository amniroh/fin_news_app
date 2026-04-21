"""
Configurable rolling horizon for equity-curve metrics (optimization / backtests).

``OPTIMIZE_ROLLING_WINDOW`` accepts forms like ``1y``, ``90d``, ``3m``, ``7d``, ``2w``,
or a plain integer (interpreted as days). Used to compute rolling total returns over
that calendar span along the simulated equity curve.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Sequence, Tuple


def normalize_rolling_window_spec(spec: str) -> str:
    s = (spec or "").strip().lower().replace(" ", "")
    return s or "1y"


def rolling_window_to_timedelta(spec: str) -> timedelta:
    """
    Convert a window spec to a timedelta.

    - ``42`` or ``42d`` → 42 days
    - ``2w`` → 14 days
    - ``3m`` → 90 days (30-day month approximation)
    - ``1y`` → 365.25 days
    """
    s = normalize_rolling_window_spec(spec)
    if not s:
        return timedelta(days=365)
    if s.isdigit():
        return timedelta(days=int(s))
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([dwmy])", s)
    if not m:
        raise ValueError(
            f"Invalid rolling window {spec!r}. Use e.g. 1y, 90d, 3m, 7d, 2w, or an integer (days)."
        )
    n, u = float(m.group(1)), m.group(2)
    if u == "d":
        return timedelta(days=n)
    if u == "w":
        return timedelta(days=n * 7.0)
    if u == "m":
        return timedelta(days=n * 30.0)
    if u == "y":
        return timedelta(days=n * 365.25)
    raise ValueError(f"Invalid rolling window {spec!r}")


def metric_suffix_from_rolling_spec(spec: str) -> str:
    """
    Short slug for metric keys, e.g. ``1y``, ``90d``, ``3m``.
    Must be safe as part of an identifier after ``rolling_`` / ``median_rolling_``.
    """
    s = normalize_rolling_window_spec(spec)
    if not s:
        return "1y"
    if s.isdigit():
        return f"{int(s)}d"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([dwmy])", s)
    if not m:
        return re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:48] or "horizon"
    n_raw, u = m.group(1), m.group(2)
    if "." in n_raw:
        n = n_raw.rstrip("0").rstrip(".")
    else:
        n = n_raw
    return f"{n}{u}"


def rolling_metric_key_base(metric_suffix: str) -> Dict[str, str]:
    """Keys used in trial/param JSON rows (mean/min/max appended for aggregates)."""
    suf = (metric_suffix or "1y").strip().lower()
    return {
        "median_return": f"median_rolling_{suf}_return",
        "hit_rate": f"rolling_{suf}_hit_rate",
        "floor_return": f"rolling_{suf}_floor_return",
        "floor_pctl": f"rolling_{suf}_floor_pctl",
    }


def default_median_return_objective_key(spec: str) -> str:
    suf = metric_suffix_from_rolling_spec(spec)
    return rolling_metric_key_base(suf)["median_return"]


def rolling_horizon_returns(
    ts: Sequence[datetime],
    eq: Sequence[float],
    horizon: timedelta,
) -> List[float]:
    """Rolling multiplicative total returns over ``horizon`` forward along the curve (aligned with optimize_rsi_mean)."""
    if len(ts) != len(eq) or len(ts) < 10:
        return []
    out: List[float] = []
    j = 0
    for i in range(len(ts)):
        t_end = ts[i] + horizon
        if ts[i].tzinfo is None:
            t_end = t_end.replace(tzinfo=timezone.utc)
        if j < i:
            j = i
        while j < len(ts) and ts[j] < t_end:
            j += 1
        if j >= len(ts):
            break
        e0 = float(eq[i])
        e1 = float(eq[j])
        if e0 <= 0:
            continue
        out.append(e1 / e0 - 1.0)
    return out
