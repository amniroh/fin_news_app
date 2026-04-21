"""
Optimize an RSI mean-reversion strategy on COMPETITIVE_BACKTEST_SYMBOLS using the
workspace optimization defaults (see OPTIMIZE_* env vars).

This module intentionally does NOT overwrite DB prices; it only reads from SQLite.

Use ``--sources`` (comma-separated, e.g. ``yfinance,alpaca``) to restrict hourly/5m
rows to those ``source`` values (see ``agent_db.get_full_adj_close_series_asc``).

We simulate a simple hourly rebalanced portfolio:
- At each hour t, rank symbols by a parameterized RSI-mean score (or skip if no signal).
- Buy equal-weight top-K for the next bar (t -> t_next) and compound equity.

We then compute (horizon from ``OPTIMIZE_ROLLING_WINDOW``, default ``1y``):
- median rolling return over that horizon (objective; metric key suffix matches the window, e.g. ``median_rolling_90d_return``)
- rolling hit rate P(R>0) over that horizon
- rolling return floor at the configured percentile
- max drawdown
and report secondary metrics (calmar, oos_sharpe) using existing strategy_metrics helpers.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telegram_agent.agent_db import connect, get_full_adj_close_series_asc, init_db
from telegram_agent.config import DATA_DIR, load_config
from telegram_agent.rsi_mean_optimizer_canvas import build_rsi_mean_optimizer_canvas_tsx
from telegram_agent.rolling_window_metrics import (
    default_median_return_objective_key,
    rolling_horizon_returns,
    rolling_metric_key_base,
    rolling_window_to_timedelta,
)
from telegram_agent.strategy_metrics import TradeLeg, compute_aggregate_metrics
from telegram_agent.symbol_universe import load_symbol_universe, normalize_symbol

import numpy as np


def _load_env_file(path: Path, *, override: bool = False) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if not k:
            continue
        # Prefer the repo's .env to be the source of truth for runs initiated from this project.
        # Only keep existing values when override=False.
        if override or k not in os.environ:
            os.environ[k] = v


def _load_dotenv_like_other_modules() -> None:
    root = Path(__file__).resolve().parents[1]
    _load_env_file(root / ".env", override=True)
    _load_env_file(Path(__file__).resolve().parent / ".env", override=True)
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env", override=True)
        load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
    except Exception:
        pass


def _parse_iso_or_date(s: str, *, is_end: bool) -> datetime:
    raw = (s or "").strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        # Date-only
        d = datetime.fromisoformat(raw)
        if is_end:
            return d.replace(tzinfo=timezone.utc) + timedelta(days=1) - timedelta(seconds=1)
        return d.replace(tzinfo=timezone.utc)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _floor_to_hour_utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    t = t.astimezone(timezone.utc)
    return t.replace(minute=0, second=0, microsecond=0)


def _resample_5m_to_hourly_close(series_5m: Sequence[Tuple[datetime, float]]) -> List[Tuple[datetime, float]]:
    if not series_5m:
        return []
    out: List[Tuple[datetime, float]] = []
    cur_h: Optional[datetime] = None
    last_px: Optional[float] = None
    for ts, px in series_5m:
        h = _floor_to_hour_utc(ts)
        if cur_h is None:
            cur_h = h
        if h != cur_h:
            if last_px is not None:
                out.append((cur_h, float(last_px)))
            cur_h = h
            last_px = None
        if px is not None and float(px) > 0:
            last_px = float(px)
    if cur_h is not None and last_px is not None:
        out.append((cur_h, float(last_px)))
    return out


def _merge_hourly_prefer_native(
    native_1h: Sequence[Tuple[datetime, float]],
    derived_1h: Sequence[Tuple[datetime, float]],
) -> List[Tuple[datetime, float]]:
    by_ts: Dict[datetime, float] = {}
    for ts, px in derived_1h:
        by_ts[_floor_to_hour_utc(ts)] = float(px)
    for ts, px in native_1h:
        by_ts[_floor_to_hour_utc(ts)] = float(px)
    return sorted(by_ts.items(), key=lambda x: x[0])


def _pct_change(closes: Sequence[float], lookback_bars: int) -> Optional[float]:
    if len(closes) < lookback_bars + 1:
        return None
    c0 = float(closes[-1])
    c1 = float(closes[-1 - lookback_bars])
    if c1 <= 0:
        return None
    return (c0 / c1 - 1.0) * 100.0


def _rsi(closes: Sequence[float], period: int) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        ch = float(closes[i]) - float(closes[i - 1])
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    avg_g = gains / period
    avg_l = losses / period
    if avg_l <= 1e-12:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


@dataclass(frozen=True)
class RsiMeanParams:
    rsi_period: int = 14
    rsi_lo: float = 18.0
    rsi_hi: float = 42.0
    mom_lookback: int = 5
    mom_max: float = 2.0
    rsi_target: float = 40.0
    mom_scale: float = 5.0


def rsi_mean_score(closes: Sequence[float], p: RsiMeanParams) -> Optional[float]:
    if len(closes) < max(25, p.rsi_period + 2, p.mom_lookback + 2):
        return None
    r = _rsi(closes, p.rsi_period)
    if r is None:
        return None
    mom = _pct_change(closes, p.mom_lookback)
    if mom is None:
        return None
    if r > p.rsi_hi or r < p.rsi_lo:
        return None
    if mom > p.mom_max:
        return None
    w = 1.0 + min(float(p.mom_scale), abs(float(mom))) / max(1e-9, float(p.mom_scale))
    return float((float(p.rsi_target) - float(r)) * w)


@dataclass(frozen=True)
class OptimizeResult:
    ok: bool
    objective: Optional[float]
    median_rolling_return: Optional[float]
    rolling_hit_rate: Optional[float]
    rolling_floor_pctl: float
    rolling_floor_return: Optional[float]
    max_drawdown: Optional[float]
    calmar: Optional[float]
    oos_sharpe: Optional[float]
    n_hours: int
    n_roll_windows: int
    params: Dict[str, Any]


def _max_drawdown(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = float(equity[0])
    mdd = 0.0
    for x in equity:
        peak = max(peak, float(x))
        if peak > 0:
            mdd = max(mdd, (peak - float(x)) / peak)
    return float(mdd)


def _max_drawdown_recovery_seconds(ts: Sequence[datetime], equity: Sequence[float]) -> Optional[float]:
    """
    Longest peak-to-recovery time in seconds.
    Recovery is the first time equity >= prior peak after a drawdown begins.
    """
    if not ts or len(ts) != len(equity) or len(ts) < 3:
        return None
    peak = float(equity[0])
    peak_t = ts[0]
    in_dd = False
    worst: float = 0.0
    for t, e in zip(ts, equity):
        e = float(e)
        if e >= peak:
            if in_dd:
                worst = max(worst, (t - peak_t).total_seconds())
                in_dd = False
            peak = e
            peak_t = t
            continue
        # below peak => drawdown ongoing
        in_dd = True
    return float(worst) if worst > 0 else 0.0


def _compute_sharpe_like(legs: Sequence[TradeLeg], *, risk_free_annual: float = 0.0) -> Optional[float]:
    # Use existing helper; it doesn't require DB when alpha isn't enabled.
    enabled = {"sharpe"}
    metrics = compute_aggregate_metrics(None, legs, enabled=enabled, risk_free_annual=risk_free_annual)  # type: ignore[arg-type]
    v = metrics.get("sharpe")
    return float(v) if v is not None else None


def _compute_calmar_oos_sharpe(legs: Sequence[TradeLeg], *, risk_free_annual: float = 0.0) -> Tuple[Optional[float], Optional[float]]:
    enabled = {"max_drawdown", "calmar", "oos_sharpe"}
    metrics = compute_aggregate_metrics(None, legs, enabled=enabled, risk_free_annual=risk_free_annual)  # type: ignore[arg-type]
    cm = metrics.get("calmar")
    oos = metrics.get("sharpe_out_of_sample")
    return (float(cm) if cm is not None else None, float(oos) if oos is not None else None)


def _optimize_thresholds(cfg: dict) -> Tuple[float, float, float, float]:
    """(median_return_min, hit_rate_min, floor_return_min, floor_pctl)."""
    med = cfg.get("optimize_median_rolling_return_min", 3.0)
    fl = cfg.get("optimize_rolling_floor_return_min", -0.10)
    pc = cfg.get("optimize_rolling_floor_pctl", 0.05)
    return (
        float(med),
        float(cfg.get("optimize_consistency_hit_rate_min", 0.75)),
        float(fl),
        float(pc),
    )


def _percentile(xs: Sequence[float], p: float) -> Optional[float]:
    if not xs:
        return None
    a = sorted(float(x) for x in xs)
    q = max(0.0, min(1.0, float(p)))
    k = q * (len(a) - 1)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return float(a[lo])
    w = k - lo
    return float(a[lo] * (1.0 - w) + a[hi] * w)


def _build_hourly_cache(
    con,
    symbols: Sequence[str],
    *,
    sources: Optional[Sequence[str]] = None,
) -> Dict[str, List[Tuple[datetime, float]]]:
    cache: Dict[str, List[Tuple[datetime, float]]] = {}
    for sym in symbols:
        ser_1h = get_full_adj_close_series_asc(con, sym, "1h", sources=sources)
        ser_5m = get_full_adj_close_series_asc(con, sym, "5m", sources=sources)
        derived = _resample_5m_to_hourly_close(ser_5m) if ser_5m else []
        merged = _merge_hourly_prefer_native(ser_1h, derived) if (ser_1h or derived) else []
        if merged:
            cache[sym] = merged
    return cache


@dataclass
class OptimizerContext:
    symbols_requested: List[str]
    symbols_with_any_data: List[str]
    cache: Dict[str, List[Tuple[datetime, float]]]
    sources_filter: Optional[List[str]] = None
    # Fast-path precomputations (populated by build_context)
    ref_times: List[datetime] = None  # type: ignore[assignment]
    closes_ffill: Dict[str, "np.ndarray"] = None  # type: ignore[assignment]
    gains_ps: Dict[str, "np.ndarray"] = None  # type: ignore[assignment]
    losses_ps: Dict[str, "np.ndarray"] = None  # type: ignore[assignment]


def build_context(
    cfg: dict,
    *,
    symbols: Sequence[str],
    sources: Optional[Sequence[str]] = None,
) -> OptimizerContext:
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    cache = _build_hourly_cache(con, symbols, sources=sources)
    con.close()
    any_data = [s for s in symbols if (cache.get(s) or [])]
    src_list = sorted({str(x).strip() for x in (sources or []) if str(x).strip()}) or None
    ctx = OptimizerContext(
        symbols_requested=list(symbols),
        symbols_with_any_data=any_data,
        cache=cache,
        sources_filter=src_list,
    )
    # Precompute a reference hourly timeline (densest symbol overall) and per-symbol
    # forward-filled closes aligned to that timeline, plus prefix sums of gains/losses.
    densest = [(s, len(cache.get(s) or [])) for s in any_data]
    densest.sort(key=lambda x: x[1], reverse=True)
    ref_sym = densest[0][0] if densest else None
    ref_series = cache.get(ref_sym) if ref_sym else None
    ref_times = [t for t, _ in (ref_series or [])]
    ctx.ref_times = ref_times

    closes_ffill: Dict[str, np.ndarray] = {}
    gains_ps: Dict[str, np.ndarray] = {}
    losses_ps: Dict[str, np.ndarray] = {}

    for s in any_data:
        ser = cache.get(s) or []
        out = np.full(len(ref_times), np.nan, dtype=np.float64)
        p = 0
        last = np.nan
        for i, tt in enumerate(ref_times):
            while p < len(ser) and ser[p][0] <= tt:
                last = float(ser[p][1])
                p += 1
            out[i] = last
        closes_ffill[s] = out
        # diff arrays (nan-safe): treat nan diffs as 0 for prefix sums; validity is checked later
        d = np.diff(out)
        d = np.where(np.isfinite(d), d, 0.0)
        g = np.maximum(d, 0.0)
        l = np.maximum(-d, 0.0)
        gains_ps[s] = np.concatenate([[0.0], np.cumsum(g)])
        losses_ps[s] = np.concatenate([[0.0], np.cumsum(l)])

    ctx.closes_ffill = closes_ffill
    ctx.gains_ps = gains_ps
    ctx.losses_ps = losses_ps
    return ctx


def _fast_rsi_at(
    closes: np.ndarray,
    gains_ps: np.ndarray,
    losses_ps: np.ndarray,
    i: int,
    period: int,
) -> Optional[float]:
    if period < 1:
        return None
    if i <= period:
        return None
    # Need valid endpoints
    if not (math.isfinite(float(closes[i])) and math.isfinite(float(closes[i - period]))):
        return None
    gs = float(gains_ps[i] - gains_ps[i - period])
    ls = float(losses_ps[i] - losses_ps[i - period])
    avg_g = gs / period
    avg_l = ls / period
    if avg_l <= 1e-12:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _fast_mom_pct_at(closes: np.ndarray, i: int, lookback: int) -> Optional[float]:
    if lookback < 1:
        return None
    if i <= lookback:
        return None
    c0 = float(closes[i])
    c1 = float(closes[i - lookback])
    if not (math.isfinite(c0) and math.isfinite(c1)) or c1 <= 0:
        return None
    return (c0 / c1 - 1.0) * 100.0


def simulate_fast_cross_sectional(
    ctx: OptimizerContext,
    symbols: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    min_bars: int,
    top_k: int,
    params: RsiMeanParams,
    exposure: float,
    dd_stop: Optional[float],
    dd_resume: Optional[float],
    max_eval_points: int,
    grid_offset: int = 0,
) -> Tuple[List[Tuple[datetime, float]], List[TradeLeg]]:
    times = ctx.ref_times or []
    if not times:
        return [], []
    # window indices on ref timeline
    i0 = 0
    while i0 < len(times) and times[i0] < start:
        i0 += 1
    i1 = len(times) - 1
    while i1 >= 0 and times[i1] > end:
        i1 -= 1
    if i1 - i0 < min_bars + 5:
        return [], []

    start_i = max(i0 + min_bars, i0)
    last_i = min(i1 - 2, len(times) - 2)  # need i+1
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

        ranked: List[Tuple[str, float]] = []
        P = int(params.rsi_period)
        L = int(params.mom_lookback)
        for s in syms:
            c = ctx.closes_ffill.get(s)
            if c is None:
                continue
            if i < min_bars:
                continue
            r = _fast_rsi_at(c, ctx.gains_ps[s], ctx.losses_ps[s], i, P)
            if r is None:
                continue
            mom = _fast_mom_pct_at(c, i, L)
            if mom is None:
                continue
            if r > params.rsi_hi or r < params.rsi_lo:
                continue
            if mom > params.mom_max:
                continue
            w = 1.0 + min(float(params.mom_scale), abs(float(mom))) / max(1e-9, float(params.mom_scale))
            sc = (float(params.rsi_target) - float(r)) * w
            if math.isfinite(sc):
                ranked.append((s, float(sc)))
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
        legs.append(TradeLeg(entry=t0, exit=t1, symbol="BASKET", realized_pct=(exp_eff * step_ret) * 100.0))

    return curve, legs


def _series_in_window(
    series: Sequence[Tuple[datetime, float]],
    *,
    start: datetime,
    end: datetime,
) -> List[Tuple[datetime, float]]:
    return [(t, float(p)) for (t, p) in series if t >= start and t <= end]


def simulate_hourly_rebalance(
    cache: Dict[str, List[Tuple[datetime, float]]],
    symbols: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    min_bars: int,
    top_k: int,
    params: RsiMeanParams,
    exposure: float,
    dd_stop: Optional[float],
    dd_resume: Optional[float],
) -> Tuple[List[Tuple[datetime, float]], List[TradeLeg]]:
    # Reference timeline: densest symbol within the window.
    densest = []
    for s in symbols:
        ser = cache.get(s) or []
        in_w = [x for x in ser if x[0] >= start and x[0] <= end]
        densest.append((s, len(in_w)))
    densest.sort(key=lambda x: x[1], reverse=True)
    if not densest or densest[0][1] < (min_bars + 10):
        return [], []
    ref_sym = densest[0][0]
    ref = [x for x in (cache.get(ref_sym) or []) if x[0] >= start and x[0] <= end]
    times = [t for t, _ in ref]
    if len(times) < min_bars + 2:
        return [], []

    # Pre-align each symbol's last-known close to the reference timeline (UTC hour timestamps).
    # This makes forward returns O(1) per pick instead of scanning the whole series.
    aligned: Dict[str, List[Optional[float]]] = {}
    for s in symbols:
        ser = [(t, float(px)) for (t, px) in (cache.get(s) or []) if t >= start and t <= end]
        if not ser:
            continue
        out: List[Optional[float]] = [None] * len(times)
        p = 0
        last: Optional[float] = None
        for i, tt in enumerate(times):
            while p < len(ser) and ser[p][0] <= tt:
                last = float(ser[p][1])
                p += 1
            out[i] = last
        aligned[s] = out

    closes_hist: Dict[str, List[float]] = {s: [] for s in symbols}

    equity = 1.0
    peak = 1.0
    risk_off = False
    curve: List[Tuple[datetime, float]] = []
    legs: List[TradeLeg] = []
    exp = max(0.0, min(1.0, float(exposure)))
    dd_s = None if dd_stop is None else max(0.0, min(0.95, float(dd_stop)))
    dd_r = None if dd_resume is None else max(0.0, min(0.95, float(dd_resume)))
    if dd_s is not None and dd_r is not None and dd_r > dd_s:
        dd_r = dd_s * 0.75

    for i in range(len(times) - 1):
        t0 = times[i]
        t1 = times[i + 1]
        # Advance histories through t0 using aligned prices.
        for s in symbols:
            a = aligned.get(s)
            if not a:
                continue
            px0 = a[i]
            if px0 is None:
                continue
            hist = closes_hist.get(s)
            if hist is not None:
                hist.append(float(px0))

        if i < min_bars:
            curve.append((t0, equity))
            continue

        ranked: List[Tuple[str, float]] = []
        for s in symbols:
            closes = closes_hist[s]
            if len(closes) < min_bars:
                continue
            sc = rsi_mean_score(closes, params)
            if sc is None or not math.isfinite(sc):
                continue
            ranked.append((s, float(sc)))
        ranked.sort(key=lambda x: x[1], reverse=True)
        picks = [s for s, _ in ranked[: max(1, int(top_k))]]
        if not picks:
            curve.append((t0, equity))
            continue

        # Risk overlay: go to cash on drawdown, re-enter on recovery.
        peak = max(peak, equity)
        dd_now = (peak - equity) / peak if peak > 0 else 0.0
        if dd_s is not None and dd_now >= dd_s:
            risk_off = True
        if risk_off and dd_r is not None and dd_now <= dd_r:
            risk_off = False
        exp_eff = 0.0 if risk_off else exp

        # One-bar forward return, equal-weight.
        rets: List[float] = []
        for s in picks:
            a = aligned.get(s)
            if not a:
                continue
            p0 = a[i]
            p1 = a[i + 1]
            if p0 is None or p1 is None or p0 <= 0:
                continue
            r = p1 / p0 - 1.0
            rets.append(float(r))
        if not rets:
            curve.append((t0, equity))
            continue

        step_ret = float(sum(rets) / len(rets))
        equity *= 1.0 + exp_eff * step_ret
        curve.append((t1, equity))
        legs.append(
            TradeLeg(
                entry=t0,
                exit=t1,
                symbol="BASKET",
                realized_pct=(exp_eff * step_ret) * 100.0,
            )
        )

    return curve, legs


def symbols_with_bar_in_window(
    ctx: OptimizerContext,
    start: datetime,
    end: datetime,
) -> List[str]:
    """
    Symbols with at least one *observed* hourly print in [start, end] (raw cache timestamps).

    This must match `evaluate_config` / `random_search` simulation universes. Do not substitute
    `ctx.symbols_with_any_data` alone: forward-filled prices can make illiquid names look tradable
    in a window where they never actually printed.
    """
    out: List[str] = []
    for s in ctx.symbols_with_any_data:
        ser = ctx.cache.get(s) or []
        if any(t >= start and t <= end for t, _ in ser):
            out.append(s)
    return out


def _cfg_risk_free_annual(cfg: dict) -> float:
    """Sharpe helpers: prefer pipeline/tester key used by `training_pipeline` / `agent_tester`."""
    v = cfg.get("test_risk_free_annual", cfg.get("tester_risk_free_annual", 0.0))
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def evaluate_config(
    cfg: dict,
    *,
    ctx: OptimizerContext,
    start: datetime,
    end: datetime,
    min_bars: int,
    top_k: int,
    params: RsiMeanParams,
    exposure: float,
    dd_stop: Optional[float],
    dd_resume: Optional[float],
    compute_secondary: bool = False,
) -> OptimizeResult:
    syms_ok = symbols_with_bar_in_window(ctx, start, end)
    if not syms_ok:
        _, _, _, pc0 = _optimize_thresholds(cfg)
        return OptimizeResult(
            ok=False,
            objective=None,
            median_rolling_return=None,
            rolling_hit_rate=None,
            rolling_floor_pctl=float(pc0),
            rolling_floor_return=None,
            max_drawdown=None,
            calmar=None,
            oos_sharpe=None,
            n_hours=0,
            n_roll_windows=0,
            params={"top_k": top_k, "min_bars": min_bars, **asdict(params)},
        )

    max_eval = int(cfg.get("competitive_backtest_max_eval_points", 2000))
    if bool(cfg.get("optimize_dense_hourly_simulation", False)):
        # stride becomes 1 when max_eval_points >= n_span (see simulate_fast_cross_sectional)
        max_eval = 10**9
    curve, legs = simulate_fast_cross_sectional(
        ctx,
        syms_ok,
        start=start,
        end=end,
        min_bars=min_bars,
        top_k=top_k,
        params=params,
        exposure=exposure,
        dd_stop=dd_stop,
        dd_resume=dd_resume,
        max_eval_points=max_eval,
    )
    if not curve or len(curve) < 100:
        _, _, _, pc0 = _optimize_thresholds(cfg)
        return OptimizeResult(
            ok=False,
            objective=None,
            median_rolling_return=None,
            rolling_hit_rate=None,
            rolling_floor_pctl=float(pc0),
            rolling_floor_return=None,
            max_drawdown=None,
            calmar=None,
            oos_sharpe=None,
            n_hours=len(curve),
            n_roll_windows=0,
            params={"top_k": top_k, "min_bars": min_bars, "symbols_used": syms_ok, **asdict(params)},
        )

    ts = [t for t, _ in curve]
    eq = [float(x) for _, x in curve]
    rw = rolling_window_to_timedelta(str(cfg.get("optimize_rolling_window", "1y")))
    roll = rolling_horizon_returns(ts, eq, rw)
    obj_min, hit_min, floor_min, pctl = _optimize_thresholds(cfg)
    med = float(median(roll)) if roll else None
    hit = float(sum(1 for x in roll if x > 0) / len(roll)) if roll else None
    floor = _percentile(roll, pctl) if roll else None
    mdd = _max_drawdown(eq) if eq else None
    mdd_rec_s = _max_drawdown_recovery_seconds(ts, eq) if eq else None

    calmar = None
    oos_sharpe = None
    if compute_secondary:
        calmar, oos_sharpe = _compute_calmar_oos_sharpe(
            legs, risk_free_annual=_cfg_risk_free_annual(cfg)
        )

    mdd_max = float(cfg.get("optimize_max_drawdown_max", 0.10))

    ok = True
    if med is None or med < obj_min:
        ok = False
    if hit is None or hit < hit_min:
        ok = False
    if floor is None or floor < floor_min:
        ok = False
    if mdd is None or mdd > mdd_max:
        ok = False

    objective = med
    return OptimizeResult(
        ok=ok,
        objective=objective,
        median_rolling_return=med,
        rolling_hit_rate=hit,
        rolling_floor_pctl=float(pctl),
        rolling_floor_return=floor,
        max_drawdown=mdd,
        calmar=float(calmar) if calmar is not None else None,
        oos_sharpe=float(oos_sharpe) if oos_sharpe is not None else None,
        n_hours=len(curve),
        n_roll_windows=len(roll),
        params={
            "top_k": int(top_k),
            "min_bars": int(min_bars),
            "exposure": float(exposure),
            "dd_stop": None if dd_stop is None else float(dd_stop),
            "dd_resume": None if dd_resume is None else float(dd_resume),
            "symbols_used": list(syms_ok),
            **asdict(params),
        },
    )


def _symbols_from_competitive_env(cfg: dict) -> List[str]:
    raw = (cfg.get("competitive_backtest_symbols") or "").strip()
    if not raw:
        return []
    syms = [normalize_symbol(x) for x in raw.split(",") if x.strip()]
    return sorted({s for s in syms if s})


def _symbols_from_p0p1_universe(cfg: dict) -> List[str]:
    """
    P0 and P1 tickers from the configured JSON universe (priority <= 1).
    """
    cfg2 = {**cfg, "max_priority": 1}
    syms = load_symbol_universe(cfg2) or []
    return sorted({normalize_symbol(s) for s in syms if normalize_symbol(s)})


def _emit_trials_canvas(trials: List[Dict[str, Any]], report: Dict[str, Any]) -> None:
    """
    Write a Cursor Canvas dashboard embedding all trial rows.
    Note: canvases must be written under the managed canvases directory.
    """
    canvas_path = Path(
        "/Users/aliyadollahi/.cursor/projects/Users-aliyadollahi-Projects-market-analysis/canvases/rsi-mean-optimizer-results.canvas.tsx"
    )
    # Keep payload bounded in case of huge runs.
    payload = trials[:5000]
    meta = {
        k: report.get(k)
        for k in (
            "run_id",
            "window",
            "constraints",
            "objective_name",
            "rolling_window",
            "rolling_metric_suffix",
            "rolling_metric_keys",
        )
    }
    # Embed as JSON in TSX.
    s_trials = json.dumps(payload, default=str)
    s_meta = json.dumps(meta, default=str)
    mk = report.get("rolling_metric_keys")
    if not isinstance(mk, dict):
        mk = rolling_metric_key_base(str(report.get("rolling_metric_suffix") or "1y"))
    canvas_path.write_text(
        build_rsi_mean_optimizer_canvas_tsx(s_meta, s_trials, metric_keys=mk),
        encoding="utf-8",
    )


def _load_jsonl_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out

def random_search(
    cfg: dict,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    trials: int,
    seed: int,
    sources: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    rng = random.Random(int(seed))
    ctx = build_context(cfg, symbols=symbols, sources=sources)
    mk = rolling_metric_key_base(str(cfg.get("optimize_rolling_metric_suffix", "1y")))
    rw_td = rolling_window_to_timedelta(str(cfg.get("optimize_rolling_window", "1y")))
    km, kh, kf = mk["median_return"], mk["hit_rate"], mk["floor_return"]
    _, _, _, _pctl_cfg = _optimize_thresholds(cfg)
    best_ok: Optional[OptimizeResult] = None
    best_any: Optional[OptimizeResult] = None
    attempt_rows: List[Dict[str, Any]] = []
    param_rows: List[Dict[str, Any]] = []
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    attempts_per_param = int(cfg.get("optimize_attempts_per_param", 5))
    attempts_per_param = max(1, min(50, attempts_per_param))
    if bool(cfg.get("optimize_dense_hourly_simulation", False)) and bool(
        cfg.get("optimize_deterministic_simulation", False)
    ):
        # With dense hourly + fixed phase, repeated attempts are identical work.
        attempts_per_param = 1

    for param_set_index in range(int(trials)):
        p = RsiMeanParams(
            rsi_period=rng.randint(6, 28),
            rsi_lo=float(rng.randint(10, 25)),
            rsi_hi=float(rng.randint(32, 60)),
            mom_lookback=rng.randint(3, 80),
            mom_max=float(rng.uniform(-0.5, 4.0)),
            rsi_target=float(rng.uniform(35.0, 55.0)),
            mom_scale=float(rng.uniform(1.5, 10.0)),
        )
        # Ensure sensible ordering.
        if p.rsi_lo >= p.rsi_hi - 2:
            continue
        top_k = rng.randint(1, 15)
        min_bars = rng.randint(25, 180)
        exposure = float(rng.uniform(0.10, 1.0))
        # Explore both "no overlay" and "risk-off overlay" regimes.
        use_overlay = rng.random() < 0.65
        dd_stop = float(rng.uniform(0.06, 0.10)) if use_overlay else None
        dd_resume = float(rng.uniform(0.02, float(dd_stop))) if dd_stop is not None else None

        # Build symbol set once per param set.
        res0 = evaluate_config(
            cfg,
            ctx=ctx,
            start=start,
            end=end,
            min_bars=min_bars,
            top_k=top_k,
            params=p,
            exposure=exposure,
            dd_stop=dd_stop,
            dd_resume=dd_resume,
            compute_secondary=False,
        )
        syms_used = res0.params.get("symbols_used") or ctx.symbols_with_any_data

        # Multiple attempts for the same parameter point (jitter evaluation grid).
        vals: Dict[str, List[Optional[float]]] = {
            "objective": [],
            km: [],
            kh: [],
            kf: [],
            "max_drawdown": [],
            "max_drawdown_recovery_seconds": [],
            "sharpe": [],
            "calmar": [],
            "oos_sharpe": [],
        }
        ok_attempts = 0

        for attempt_index in range(attempts_per_param):
            # IMPORTANT: random grid offsets make the simulated path materially different for the same params.
            # Default remains randomized for broad exploration, but callers can force deterministic dense eval via cfg.
            if bool(cfg.get("optimize_deterministic_simulation", False)):
                grid_offset = 0
            else:
                grid_offset = rng.randint(0, 10_000)
            max_pts = int(cfg.get("competitive_backtest_max_eval_points", 2000))
            if bool(cfg.get("optimize_dense_hourly_simulation", False)):
                # stride becomes 1 when max_eval_points >= n_span (see simulate_fast_cross_sectional)
                max_pts = 10**9
            curve, legs = simulate_fast_cross_sectional(
                ctx,
                syms_used,
                start=start,
                end=end,
                min_bars=min_bars,
                top_k=top_k,
                params=p,
                exposure=exposure,
                dd_stop=dd_stop,
                dd_resume=dd_resume,
                max_eval_points=max_pts,
                grid_offset=grid_offset,
            )
            if not curve:
                continue
            ts = [t for t, _ in curve]
            eq = [float(x) for _, x in curve]
            roll = rolling_horizon_returns(ts, eq, rw_td)
            obj_min, hit_min, floor_min, pctl = _optimize_thresholds(cfg)
            med = float(median(roll)) if roll else None
            hit = float(sum(1 for x in roll if x > 0) / len(roll)) if roll else None
            floor = _percentile(roll, pctl) if roll else None
            mdd = _max_drawdown(eq) if eq else None
            mdd_rec_s = _max_drawdown_recovery_seconds(ts, eq) if eq else None
            sharpe = _compute_sharpe_like(legs, risk_free_annual=_cfg_risk_free_annual(cfg))
            calmar_t, oos_t = _compute_calmar_oos_sharpe(legs, risk_free_annual=_cfg_risk_free_annual(cfg))

            mdd_max = float(cfg.get("optimize_max_drawdown_max", 0.10))
            ok_a = True
            if med is None or med < obj_min:
                ok_a = False
            if hit is None or hit < hit_min:
                ok_a = False
            if floor is None or floor < floor_min:
                ok_a = False
            if mdd is None or mdd > mdd_max:
                ok_a = False
            if ok_a:
                ok_attempts += 1

            # raw attempt row
            attempt_rows.append(
                {
                    "run_id": run_id,
                    "param_set_index": param_set_index,
                    "attempt_index": attempt_index,
                    "grid_offset": int(grid_offset),
                    "ok": bool(ok_a),
                    "objective": med,
                    km: med,
                    kh: hit,
                    mk["floor_pctl"]: pctl,
                    kf: floor,
                    "max_drawdown": mdd,
                    "max_drawdown_recovery_seconds": mdd_rec_s,
                    "sharpe": sharpe,
                    "calmar": calmar_t,
                    "oos_sharpe": oos_t,
                    "n_hours": len(curve),
                    "n_roll_windows": len(roll),
                    **{f"p_{k}": v for k, v in (res0.params or {}).items() if k != "symbols_used"},
                }
            )

            # collect for aggregation
            vals["objective"].append(med)
            vals[km].append(med)
            vals[kh].append(hit)
            vals[kf].append(floor)
            vals["max_drawdown"].append(mdd)
            vals["max_drawdown_recovery_seconds"].append(mdd_rec_s)
            vals["sharpe"].append(sharpe)
            vals["calmar"].append(calmar_t)
            vals["oos_sharpe"].append(oos_t)

        def _agg(xs: List[Optional[float]]) -> Dict[str, Optional[float]]:
            ys = [float(x) for x in xs if x is not None and math.isfinite(float(x))]
            if not ys:
                return {"mean": None, "median": None, "min": None, "max": None}
            ys.sort()
            mid = ys[len(ys) // 2]
            return {
                "mean": float(sum(ys) / len(ys)),
                "median": float(mid),
                "min": float(ys[0]),
                "max": float(ys[-1]),
            }

        a_obj = _agg(vals["objective"])
        a_hit = _agg(vals[kh])
        a_floor = _agg(vals[kf])
        a_mdd = _agg(vals["max_drawdown"])
        a_sh = _agg(vals["sharpe"])
        a_rec = _agg(vals["max_drawdown_recovery_seconds"])
        a_cal = _agg(vals["calmar"])
        a_oos = _agg(vals["oos_sharpe"])

        # Param-set level feasibility: require worst-case constraints across attempts.
        obj_min, hit_min, floor_min, _pctl_thr = _optimize_thresholds(cfg)
        mdd_max = float(cfg.get("optimize_max_drawdown_max", 0.10))
        ok_param = True
        if a_obj["min"] is None or a_obj["min"] < obj_min:
            ok_param = False
        if a_hit["min"] is None or a_hit["min"] < hit_min:
            ok_param = False
        if a_floor["min"] is None or a_floor["min"] < floor_min:
            ok_param = False
        if a_mdd["max"] is None or a_mdd["max"] > mdd_max:
            ok_param = False

        param_row = {
            "run_id": run_id,
            "param_set_index": param_set_index,
            "attempts": int(attempts_per_param),
            "ok": bool(ok_param),
            "ok_attempts": int(ok_attempts),
            # objective/constraints metrics (aggregated)
            "objective_mean": a_obj["mean"],
            "objective_median": a_obj["median"],
            "objective_min": a_obj["min"],
            "objective_max": a_obj["max"],
            f"{km}_mean": a_obj["mean"],
            f"{km}_median": a_obj["median"],
            f"{km}_min": a_obj["min"],
            f"{km}_max": a_obj["max"],
            f"{kh}_mean": a_hit["mean"],
            f"{kh}_median": a_hit["median"],
            f"{kh}_min": a_hit["min"],
            f"{kh}_max": a_hit["max"],
            f"{kf}_mean": a_floor["mean"],
            f"{kf}_median": a_floor["median"],
            f"{kf}_min": a_floor["min"],
            f"{kf}_max": a_floor["max"],
            "max_drawdown_mean": a_mdd["mean"],
            "max_drawdown_median": a_mdd["median"],
            "max_drawdown_min": a_mdd["min"],
            "max_drawdown_max": a_mdd["max"],
            "max_drawdown_recovery_seconds_mean": a_rec["mean"],
            "sharpe_mean": a_sh["mean"],
            "calmar_mean": a_cal["mean"],
            "oos_sharpe_mean": a_oos["mean"],
            # Params (flattened)
            **{f"p_{k}": v for k, v in (res0.params or {}).items() if k != "symbols_used"},
        }
        param_rows.append(param_row)

        # Track winners using aggregated objective (median across attempts).
        obj_val = a_obj["median"]
        if best_any is None or (obj_val is not None and (best_any.objective or -1e9) < obj_val):
            best_any = OptimizeResult(
                ok=bool(ok_param),
                objective=obj_val,
                median_rolling_return=obj_val,
                rolling_hit_rate=a_hit["median"],
                rolling_floor_pctl=float(_pctl_cfg),
                rolling_floor_return=a_floor["median"],
                max_drawdown=a_mdd["median"],
                calmar=a_cal["mean"],
                oos_sharpe=a_oos["mean"],
                n_hours=0,
                n_roll_windows=0,
                params={k[2:]: v for k, v in param_row.items() if k.startswith("p_")},
            )
        if ok_param and obj_val is not None:
            if best_ok is None or (best_ok.objective or -1e9) < obj_val:
                best_ok = OptimizeResult(
                    ok=True,
                    objective=obj_val,
                    median_rolling_return=obj_val,
                    rolling_hit_rate=a_hit["median"],
                    rolling_floor_pctl=float(_pctl_cfg),
                    rolling_floor_return=a_floor["median"],
                    max_drawdown=a_mdd["median"],
                    calmar=a_cal["mean"],
                    oos_sharpe=a_oos["mean"],
                    n_hours=0,
                    n_roll_windows=0,
                    params={k[2:]: v for k, v in param_row.items() if k.startswith("p_")},
                )

    return {
        "objective_name": cfg.get("optimize_objective", km),
        "rolling_window": cfg.get("optimize_rolling_window", "1y"),
        "rolling_metric_suffix": cfg.get("optimize_rolling_metric_suffix", "1y"),
        "rolling_metric_keys": dict(mk),
        "constraints": {
            "median_rolling_return_min": cfg.get("optimize_median_rolling_return_min"),
            "rolling_hit_rate_min": cfg.get("optimize_consistency_hit_rate_min"),
            "rolling_floor_pctl": cfg.get("optimize_rolling_floor_pctl"),
            "rolling_floor_return_min": cfg.get("optimize_rolling_floor_return_min"),
            "max_drawdown_max": cfg.get("optimize_max_drawdown_max"),
        },
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "sources_filter": list(ctx.sources_filter) if ctx.sources_filter else None,
        "symbols_requested": list(ctx.symbols_requested),
        "symbols_with_any_data": list(ctx.symbols_with_any_data),
        "run_id": run_id,
        "attempts": attempt_rows,
        "param_sets": param_rows,
        "best_feasible": None if best_ok is None else asdict(best_ok),
        "best_overall": None if best_any is None else asdict(best_any),
    }


def main() -> None:
    _load_dotenv_like_other_modules()
    cfg = load_config()

    p = argparse.ArgumentParser(description="Optimize RSI mean-reversion on COMPETITIVE_BACKTEST_SYMBOLS")
    p.add_argument("--start", type=str, default="2020-01-01", help="UTC start (YYYY-MM-DD or ISO)")
    p.add_argument("--end", type=str, default="2024-01-01", help="UTC end (YYYY-MM-DD or ISO)")
    p.add_argument("--trials", type=int, default=200, help="Random search trials")
    p.add_argument("--seed", type=int, default=7, help="RNG seed")
    p.add_argument(
        "--symbol-mode",
        type=str,
        default="competitive",
        help="competitive=COMPETITIVE_BACKTEST_SYMBOLS; p0p1=priority<=1 symbols from SYMBOL_UNIVERSE_PATH",
    )
    p.add_argument("--out", type=str, default=str(DATA_DIR / "optimize_rsi_mean.json"), help="Output JSON path")
    p.add_argument(
        "--trials-csv",
        type=str,
        default=str(DATA_DIR / "optimize_rsi_mean_trials.csv"),
        help="Write all trial rows to this CSV",
    )
    p.add_argument(
        "--trials-jsonl",
        type=str,
        default=str(DATA_DIR / "optimize_rsi_mean_trials.jsonl"),
        help="Write all trial rows to this JSONL",
    )
    p.add_argument(
        "--emit-canvas",
        action="store_true",
        help="Generate a Canvas dashboard file embedding all trials (open beside chat).",
    )
    p.add_argument(
        "--attempts-per-param",
        type=int,
        default=5,
        help="How many repeated attempts per parameter set (aggregated into one search-space point).",
    )
    p.add_argument(
        "--visualize-from",
        type=str,
        default="",
        help="Load aggregated param-set rows from an existing *.jsonl file and (optionally) emit the canvas without re-running optimization.",
    )
    p.add_argument(
        "--sources",
        type=str,
        default="",
        help=(
            "Comma-separated price ``source`` values to include (e.g. yfinance,alpaca). "
            "If omitted, all sources are used when building the hourly cache."
        ),
    )
    args = p.parse_args()

    start = _parse_iso_or_date(args.start, is_end=False)
    end = _parse_iso_or_date(args.end, is_end=True)

    if str(args.visualize_from or "").strip():
        src = Path(str(args.visualize_from)).expanduser()
        rows = _load_jsonl_rows(src)
        _mk_vis = rolling_metric_key_base(str(cfg.get("optimize_rolling_metric_suffix", "1y")))
        report = {
            "run_id": (rows[0].get("run_id") if rows else None),
            "objective_name": cfg.get(
                "optimize_objective",
                default_median_return_objective_key(str(cfg.get("optimize_rolling_window", "1y"))),
            ),
            "rolling_window": cfg.get("optimize_rolling_window", "1y"),
            "rolling_metric_suffix": cfg.get("optimize_rolling_metric_suffix", "1y"),
            "rolling_metric_keys": dict(_mk_vis),
            "constraints": {
                "median_rolling_return_min": cfg.get("optimize_median_rolling_return_min"),
                "rolling_hit_rate_min": cfg.get("optimize_consistency_hit_rate_min"),
                "rolling_floor_pctl": cfg.get("optimize_rolling_floor_pctl"),
                "rolling_floor_return_min": cfg.get("optimize_rolling_floor_return_min"),
                "max_drawdown_max": cfg.get("optimize_max_drawdown_max"),
            },
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "param_sets_loaded_from": str(src),
        }
        if bool(args.emit_canvas):
            _emit_trials_canvas(rows, report)
        print(json.dumps({"ok": True, "loaded_rows": len(rows), **report}, indent=2, default=str))
        return

    mode = (args.symbol_mode or "competitive").strip().lower()
    if mode in ("p0p1", "p0_p1", "universe", "universe_p0p1"):
        syms = _symbols_from_p0p1_universe(cfg)
    else:
        syms = _symbols_from_competitive_env(cfg)
    if not syms:
        raise SystemExit("No symbols resolved for the selected --symbol-mode")

    cfg = {**cfg, "optimize_attempts_per_param": int(args.attempts_per_param)}
    source_filter = [x.strip() for x in (args.sources or "").split(",") if x and str(x).strip()]
    report = random_search(
        cfg,
        symbols=syms,
        start=start,
        end=end,
        trials=int(args.trials),
        seed=int(args.seed),
        sources=source_filter if source_filter else None,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    # Write table outputs: attempts + aggregated param sets
    attempts = report.get("attempts") or []
    param_sets = report.get("param_sets") or []

    def _write_table(rows: List[Dict[str, Any]], csv_path: Path, jsonl_path: Path) -> None:
        if not rows:
            return
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        keys: List[str] = []
        for r in rows:
            if isinstance(r, dict):
                for k in r.keys():
                    if k not in keys:
                        keys.append(k)
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for r in rows:
                if isinstance(r, dict):
                    w.writerow(r)
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")

    base_csv = Path(args.trials_csv)
    base_jsonl = Path(args.trials_jsonl)
    # Back-compat names become "attempts"
    _write_table(attempts, base_csv, base_jsonl)
    # Aggregated param-set table
    agg_csv = base_csv.with_name(base_csv.stem.replace("trials", "param_sets") + base_csv.suffix)
    agg_jsonl = base_jsonl.with_name(base_jsonl.stem.replace("trials", "param_sets") + base_jsonl.suffix)
    _write_table(param_sets, agg_csv, agg_jsonl)

    if bool(args.emit_canvas) and isinstance(param_sets, list) and param_sets:
        _emit_trials_canvas(param_sets, report)

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()

