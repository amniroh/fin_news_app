"""
Portfolio-style metrics from a chronological series of completed trade returns.

Designed for any strategy stored as timed legs (e.g. `recommendations` rows with
entry/exit and realized_pct), or future sources that produce the same shape.

Assumptions (documented):
- Trades are processed in **entry_ts** order; equity compounds sequentially (no overlap model).
- Sharpe / significance use **per-trade returns**; annualization uses calendar span of the sample.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean, stdev
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telegram_agent.agent_db import get_close_at_or_before


@dataclass(frozen=True)
class TradeLeg:
    """One completed round-trip with a scalar return (already realized)."""

    entry: datetime
    exit: datetime
    symbol: str
    realized_pct: float  # percent, e.g. 3.2 for +3.2%
    leg_id: Optional[int] = None


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _decimal_returns(legs: Sequence[TradeLeg]) -> List[float]:
    return [float(r.realized_pct) / 100.0 for r in legs]


def equity_curve_compound(decimal_returns: Sequence[float]) -> List[float]:
    """Cumulative equity starting at 1.0."""
    eq = 1.0
    out: List[float] = []
    for r in decimal_returns:
        eq *= 1.0 + float(r)
        out.append(eq)
    return out


def max_drawdown_from_equity(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for x in equity:
        peak = max(peak, x)
        if peak > 0:
            dd = (peak - x) / peak
            mdd = max(mdd, dd)
    return float(mdd)


def _annualization_factor_from_calendar(legs: Sequence[TradeLeg]) -> float:
    if len(legs) < 2:
        return 1.0
    t0 = min(_utc(l.entry) for l in legs)
    t1 = max(_utc(l.exit) for l in legs)
    days = max(1.0, (t1 - t0).total_seconds() / 86400.0)
    years = days / 365.25
    n = len(legs)
    if years <= 0:
        return 1.0
    # Trades per year (intensity) used to scale per-trade vol to annual-ish Sharpe heuristic.
    return math.sqrt(max(1.0, n / years))


def sharpe_from_trade_returns(
    decimal_returns: Sequence[float],
    *,
    legs: Sequence[TradeLeg],
    risk_free_annual: float = 0.0,
) -> Optional[float]:
    if len(decimal_returns) < 2:
        return None
    rf_per_trade = (1.0 + risk_free_annual) ** (1.0 / max(1.0, len(legs))) - 1.0
    excess = [float(r) - rf_per_trade for r in decimal_returns]
    m = mean(excess)
    s = stdev(excess)
    if s <= 0 or s < 1e-12:
        return None
    ann = _annualization_factor_from_calendar(legs)
    return float((m / s) * ann)


def calmar_ratio(
    decimal_returns: Sequence[float],
    legs: Sequence[TradeLeg],
    max_dd: float,
) -> Optional[float]:
    if max_dd <= 0:
        return None
    if not legs:
        return None
    t0 = min(_utc(l.entry) for l in legs)
    t1 = max(_utc(l.exit) for l in legs)
    years = max(1e-9, (t1 - t0).total_seconds() / 86400.0 / 365.25)
    eq = equity_curve_compound(decimal_returns)
    total_ret = eq[-1] - 1.0 if eq else 0.0
    cagr = (1.0 + total_ret) ** (1.0 / years) - 1.0 if years > 0 else total_ret
    return float(cagr / max_dd)


def mean_alpha_vs_benchmark(
    con,
    legs: Sequence[TradeLeg],
    benchmark_symbol: str,
) -> Tuple[Optional[float], int]:
    """Mean (asset_return - benchmark_return) per leg; asset uses stored realized_pct, benchmark from prices."""
    diffs: List[float] = []
    bench = benchmark_symbol.strip().upper()
    for leg in legs:
        entry = _utc(leg.entry)
        exit_ = _utc(leg.exit)
        if exit_ <= entry:
            continue
        r_a = float(leg.realized_pct) / 100.0
        b0 = get_close_at_or_before(con, bench, entry)
        b1 = get_close_at_or_before(con, bench, exit_)
        if not b0 or not b1 or b0 <= 0:
            continue
        r_b = (b1 - b0) / b0
        diffs.append(r_a - r_b)
    if not diffs:
        return None, 0
    return float(mean(diffs)), len(diffs)


def t_test_mean_nonzero_p_value(decimal_returns: Sequence[float]) -> Optional[float]:
    """Two-sided p-value for H0: mean return == 0 (normal approx; ok for n>=~20)."""
    xs = [float(x) for x in decimal_returns]
    n = len(xs)
    if n < 2:
        return None
    m = mean(xs)
    s = stdev(xs)
    if s <= 1e-12:
        return None
    t = m / (s / math.sqrt(n))
    # Normal approx (avoids scipy)
    try:
        from statistics import NormalDist

        p = 2.0 * (1.0 - NormalDist().cdf(abs(t)))
    except Exception:
        p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))
    return float(max(0.0, min(1.0, p)))


def split_oos_legs(legs: Sequence[TradeLeg], fraction_first_in_sample: float) -> Tuple[List[TradeLeg], List[TradeLeg]]:
    """Split by entry time: first fraction 'in-sample', remainder 'out-of-sample'."""
    if not legs:
        return [], []
    if len(legs) < 2:
        return list(legs), []
    fr = max(0.05, min(0.95, float(fraction_first_in_sample)))
    ordered = sorted(legs, key=lambda l: _utc(l.entry))
    k = int(round(len(ordered) * fr))
    k = max(1, min(len(ordered) - 1, k))
    return ordered[:k], ordered[k:]


def compute_aggregate_metrics(
    con,
    legs: Sequence[TradeLeg],
    *,
    benchmark_symbol: str = "SPY",
    risk_free_annual: float = 0.04,
    oos_split: float = 0.5,
    enabled: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """
    Returns a JSON-serializable dict of metrics. Keys match config slugs:
    sharpe, alpha, max_drawdown, oos_sharpe, calmar, significance
    """
    all_keys = {"sharpe", "alpha", "max_drawdown", "oos_sharpe", "calmar", "significance"}
    en = enabled if enabled is not None else all_keys

    out: Dict[str, Any] = {
        "n_legs": len(legs),
        "benchmark_symbol": benchmark_symbol.strip().upper(),
        "risk_free_annual": risk_free_annual,
        "oos_split": oos_split,
    }
    if len(legs) < 2:
        out["note"] = "need_at_least_two_completed_trades_with_realized_returns"
        return out

    ordered = sorted(legs, key=lambda l: _utc(l.entry))
    dec = _decimal_returns(ordered)
    eq = equity_curve_compound(dec)

    need_mdd = "max_drawdown" in en or "calmar" in en
    mdd_val: Optional[float] = None
    if need_mdd:
        mdd_val = max_drawdown_from_equity(eq)
        if "max_drawdown" in en:
            out["max_drawdown"] = round(mdd_val, 6)

    if "sharpe" in en:
        sh = sharpe_from_trade_returns(dec, legs=ordered, risk_free_annual=risk_free_annual)
        out["sharpe"] = None if sh is None else round(sh, 6)

    if "calmar" in en and mdd_val is not None:
        cm = calmar_ratio(dec, ordered, float(mdd_val))
        out["calmar"] = None if cm is None else round(cm, 6)

    if "alpha" in en:
        a_mean, n_a = mean_alpha_vs_benchmark(con, ordered, benchmark_symbol)
        out["alpha_vs_benchmark_mean"] = None if a_mean is None else round(a_mean, 6)
        out["alpha_n_legs_with_benchmark"] = n_a

    if "significance" in en:
        p = t_test_mean_nonzero_p_value(dec)
        out["mean_return_per_trade_pct"] = round(mean(dec) * 100.0, 6)
        out["significance_p_value_mean_return"] = None if p is None else round(p, 6)

    if "oos_sharpe" in en:
        is_legs, oos_legs = split_oos_legs(ordered, oos_split)
        out["oos_split_used"] = oos_split
        out["oos_n_is"] = len(is_legs)
        out["oos_n_oos"] = len(oos_legs)
        dis = _decimal_returns(is_legs)
        doos = _decimal_returns(oos_legs)
        sh_is = sharpe_from_trade_returns(dis, legs=is_legs, risk_free_annual=risk_free_annual)
        sh_oos = sharpe_from_trade_returns(doos, legs=oos_legs, risk_free_annual=risk_free_annual)
        out["sharpe_in_sample"] = None if sh_is is None else round(sh_is, 6)
        out["sharpe_out_of_sample"] = None if sh_oos is None else round(sh_oos, 6)

    return out


def pick_optimization_value(metrics: Dict[str, Any], metric_key: str) -> Optional[float]:
    """Map aggregate metrics dict to a single scalar for feedback / optimization."""
    k = (metric_key or "sharpe").strip().lower()
    if k == "sharpe":
        v = metrics.get("sharpe")
        return float(v) if v is not None else None
    if k == "alpha":
        v = metrics.get("alpha_vs_benchmark_mean")
        return float(v) if v is not None else None
    if k == "max_drawdown":
        v = metrics.get("max_drawdown")
        # Lower is better — negate for "higher is better" convention in feedback
        return -float(v) if v is not None else None
    if k == "oos_sharpe":
        v = metrics.get("sharpe_out_of_sample")
        return float(v) if v is not None else None
    if k == "calmar":
        v = metrics.get("calmar")
        return float(v) if v is not None else None
    if k in ("significance", "p_value", "statistical_significance"):
        v = metrics.get("significance_p_value_mean_return")
        # Lower p-value is "more significant" — use 1-p as higher-is-better score
        return (1.0 - float(v)) if v is not None else None
    return None
