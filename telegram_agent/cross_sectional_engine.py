"""
Generic hourly cross-sectional simulator: rank symbols each bar, hold equal-weight basket.

Used by `signal_strategies` + `optimize_generic_signal` alongside `optimize_rsi_mean.simulate_fast_cross_sectional`
for RSI-specific logic.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from telegram_agent.optimize_rsi_mean import OptimizerContext
from telegram_agent.strategy_metrics import TradeLeg

RankFn = Callable[
    [OptimizerContext, int, Sequence[str], int, int, Dict[str, Any], Dict[str, Any]],
    List[Tuple[str, float]],
]


def simulate_cross_sectional_ranked(
    ctx: OptimizerContext,
    symbols: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    min_bars: int,
    top_k: int,
    params: Dict[str, Any],
    exposure: float,
    dd_stop: Optional[float],
    dd_resume: Optional[float],
    max_eval_points: int,
    grid_offset: int = 0,
    rank_fn: RankFn,
    cfg: Dict[str, Any],
) -> Tuple[List[Tuple[datetime, float]], List[TradeLeg]]:
    times = ctx.ref_times or []
    if not times:
        return [], []
    i0 = 0
    while i0 < len(times) and times[i0] < start:
        i0 += 1
    i1 = len(times) - 1
    while i1 >= 0 and times[i1] > end:
        i1 -= 1
    if i1 - i0 < min_bars + 5:
        return [], []

    start_i = max(i0 + min_bars, i0)
    last_i = min(i1 - 2, len(times) - 2)
    n_span = max(0, last_i - start_i + 1)
    stride = max(1, n_span // max(50, int(max_eval_points))) if n_span else 1
    off = int(grid_offset) % max(1, stride)

    exp = max(0.0, min(1.0, float(exposure)))
    dd_s = None if dd_stop is None else max(0.0, min(0.95, float(dd_stop)))
    dd_r = None if dd_resume is None else max(0.0, min(0.95, float(dd_resume)))
    if dd_s is not None and dd_r is not None and dd_r > dd_s:
        dd_r = dd_s * 0.75

    equity = 1.0
    peak = 1.0
    risk_off = False
    curve: List[Tuple[datetime, float]] = []
    legs: List[TradeLeg] = []
    syms = list(symbols)

    for i in range(start_i + off, last_i + 1, stride):
        t0 = times[i]
        t1 = times[i + 1]

        peak = max(peak, equity)
        dd_now = (peak - equity) / peak if peak > 0 else 0.0
        if dd_s is not None and dd_now >= dd_s:
            risk_off = True
        if risk_off and dd_r is not None and dd_now <= dd_r:
            risk_off = False
        exp_eff = 0.0 if risk_off else exp

        ranked = rank_fn(ctx, i, syms, min_bars, top_k, params, cfg)
        ranked.sort(key=lambda x: x[1], reverse=True)
        picks = [s for s, _ in ranked[: max(1, int(top_k))]]
        if not picks:
            curve.append((t0, equity))
            continue

        rets: List[float] = []
        for s in picks:
            c = ctx.closes_ffill.get(s)
            if c is None:
                continue
            p0 = float(c[i])
            p1 = float(c[i + 1])
            if not (math.isfinite(p0) and math.isfinite(p1)) or p0 <= 0:
                continue
            rets.append(p1 / p0 - 1.0)
        if not rets:
            curve.append((t0, equity))
            continue
        step_ret = float(sum(rets) / len(rets))
        equity *= 1.0 + exp_eff * step_ret
        curve.append((t1, equity))
        legs.append(
            TradeLeg(entry=t0, exit=t1, symbol="BASKET", realized_pct=(exp_eff * step_ret) * 100.0)
        )

    return curve, legs
