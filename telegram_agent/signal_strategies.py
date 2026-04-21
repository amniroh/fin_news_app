"""
Standard cross-sectional investment signals (price-based + optional fundamentals).

Each signal exposes ``rank_symbols(...) -> List[Tuple[str, float]]`` (higher score = prefer in basket)
and ``sample_params(rng)`` for random search.

Signals (keys match adapter names):
  signal_macd, signal_bollinger, signal_sma_cross, signal_stochastic, signal_williams_r,
  signal_roc, signal_cci, signal_atr_momentum, signal_adx, signal_pe_ratio
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from telegram_agent.optimize_rsi_mean import OptimizerContext

# --- price helpers (closes: ndarray or sequence) ---


def _fv(c, j: int) -> Optional[float]:
    try:
        x = float(c[j])
    except (IndexError, TypeError, ValueError):
        return None
    if not math.isfinite(x) or x <= 0:
        return None
    return x


def _sma(c, i: int, L: int) -> Optional[float]:
    if i < L - 1:
        return None
    s = 0.0
    for j in range(i - L + 1, i + 1):
        v = _fv(c, j)
        if v is None:
            return None
        s += v
    return s / float(L)


def _stdev(c, i: int, L: int) -> Optional[float]:
    if i < L - 1:
        return None
    m = _sma(c, i, L)
    if m is None:
        return None
    acc = 0.0
    for j in range(i - L + 1, i + 1):
        v = _fv(c, j)
        if v is None:
            return None
        d = v - m
        acc += d * d
    return math.sqrt(acc / float(L))


def _true_range_proxy(c, i: int) -> Optional[float]:
    if i < 1:
        return None
    a = _fv(c, i)
    b = _fv(c, i - 1)
    if a is None or b is None:
        return None
    return abs(a - b)


def _atr_wilder(c, i: int, period: int) -> Optional[float]:
    if i < period:
        return None
    trs: List[float] = []
    for j in range(i - period + 1, i + 1):
        tr = _true_range_proxy(c, j)
        if tr is None:
            return None
        trs.append(tr)
    # simple SMA of TR for ATR proxy
    return sum(trs) / len(trs)


def _load_pe_map(cfg: Dict[str, Any]) -> Dict[str, float]:
    raw = cfg.get("pipeline_value_metrics_pe")
    if isinstance(raw, dict):
        return {str(k).upper(): float(v) for k, v in raw.items() if v is not None}
    path = (cfg.get("pipeline_value_metrics_path") or "").strip()
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, float] = {}
    for k, v in obj.items():
        if isinstance(v, dict) and "pe" in v:
            try:
                out[str(k).upper()] = float(v["pe"])
            except (TypeError, ValueError):
                continue
        else:
            try:
                out[str(k).upper()] = float(v)
            except (TypeError, ValueError):
                continue
    return out


# --- rankers: return (symbol, score) unsorted ---


def rank_macd(
    ctx: OptimizerContext,
    i: int,
    syms: Sequence[str],
    min_bars: int,
    top_k: int,
    p: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Tuple[str, float]]:
    """MACD-style spread: fast SMA minus slow SMA (same information direction as classic MACD line)."""
    fast = int(p.get("macd_fast", 12))
    slow = int(p.get("macd_slow", 26))
    if i < min_bars or slow < 2 or fast >= slow:
        return []
    out: List[Tuple[str, float]] = []
    for s in syms:
        c = ctx.closes_ffill.get(s)
        if c is None or i < slow:
            continue
        ema_f = _sma(c, i, fast)
        ema_sl = _sma(c, i, slow)
        if ema_f is None or ema_sl is None:
            continue
        line = (ema_f - ema_sl) / max(ema_sl, 1e-12)
        out.append((s, line))
    return out


def rank_bollinger(
    ctx: OptimizerContext,
    i: int,
    syms: Sequence[str],
    min_bars: int,
    top_k: int,
    p: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Tuple[str, float]]:
    L = int(p.get("bb_period", 20))
    nstd = float(p.get("bb_std", 2.0))
    if i < min_bars or L < 5:
        return []
    out: List[Tuple[str, float]] = []
    for s in syms:
        c = ctx.closes_ffill.get(s)
        if c is None:
            continue
        mid = _sma(c, i, L)
        sd = _stdev(c, i, L)
        px = _fv(c, i)
        if mid is None or sd is None or px is None or sd <= 0:
            continue
        upper = mid + nstd * sd
        lower = mid - nstd * sd
        if upper <= lower:
            continue
        pct_b = (px - lower) / (upper - lower)
        # mean reversion: prefer low %B → higher score when buying oversold
        score = float(p.get("bb_mode", 1.0)) * (0.5 - pct_b)
        out.append((s, score))
    return out


def rank_sma_cross(
    ctx: OptimizerContext,
    i: int,
    syms: Sequence[str],
    min_bars: int,
    top_k: int,
    p: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Tuple[str, float]]:
    short = int(p.get("sma_short", 10))
    long = int(p.get("sma_long", 50))
    if i < min_bars or short >= long:
        return []
    out: List[Tuple[str, float]] = []
    for s in syms:
        c = ctx.closes_ffill.get(s)
        if c is None:
            continue
        a = _sma(c, i, short)
        b = _sma(c, i, long)
        if a is None or b is None or b <= 0:
            continue
        score = (a - b) / b
        out.append((s, score))
    return out


def rank_stochastic(
    ctx: OptimizerContext,
    i: int,
    syms: Sequence[str],
    min_bars: int,
    top_k: int,
    p: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Tuple[str, float]]:
    N = int(p.get("stoch_k_period", 14))
    if i < min_bars or N < 3:
        return []
    out: List[Tuple[str, float]] = []
    for s in syms:
        c = ctx.closes_ffill.get(s)
        if c is None or i < N - 1:
            continue
        lows = []
        highs = []
        for j in range(i - N + 1, i + 1):
            v = _fv(c, j)
            if v is None:
                lows = []
                break
            lows.append(v)
            highs.append(v)
        if not lows:
            continue
        lo = min(lows)
        hi = max(highs)
        ci = _fv(c, i)
        if ci is None or hi <= lo:
            continue
        k = (ci - lo) / (hi - lo) * 100.0
        # mean reversion: prefer low K
        score = 50.0 - k
        out.append((s, score))
    return out


def rank_williams_r(
    ctx: OptimizerContext,
    i: int,
    syms: Sequence[str],
    min_bars: int,
    top_k: int,
    p: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Tuple[str, float]]:
    N = int(p.get("williams_period", 14))
    if i < min_bars or N < 3:
        return []
    out: List[Tuple[str, float]] = []
    for s in syms:
        c = ctx.closes_ffill.get(s)
        if c is None or i < N - 1:
            continue
        chunk = [_fv(c, j) for j in range(i - N + 1, i + 1)]
        if any(x is None for x in chunk):
            continue
        lo = min(chunk)  # type: ignore
        hi = max(chunk)  # type: ignore
        ci = chunk[-1]
        if hi <= lo:
            continue
        wr = (hi - ci) / (hi - lo) * -100.0
        out.append((s, wr))
    return out


def rank_roc(
    ctx: OptimizerContext,
    i: int,
    syms: Sequence[str],
    min_bars: int,
    top_k: int,
    p: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Tuple[str, float]]:
    n = int(p.get("roc_period", 10))
    if i < min_bars or n < 1:
        return []
    out: List[Tuple[str, float]] = []
    for s in syms:
        c = ctx.closes_ffill.get(s)
        if c is None or i < n:
            continue
        c0 = _fv(c, i)
        c1 = _fv(c, i - n)
        if c0 is None or c1 is None or c1 <= 0:
            continue
        roc = (c0 / c1 - 1.0) * 100.0
        out.append((s, roc))
    return out


def rank_cci(
    ctx: OptimizerContext,
    i: int,
    syms: Sequence[str],
    min_bars: int,
    top_k: int,
    p: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Tuple[str, float]]:
    N = int(p.get("cci_period", 20))
    if i < min_bars or N < 5:
        return []
    out: List[Tuple[str, float]] = []
    for s in syms:
        c = ctx.closes_ffill.get(s)
        if c is None or i < N - 1:
            continue
        tp = [_fv(c, j) for j in range(i - N + 1, i + 1)]
        if any(x is None for x in tp):
            continue
        sm = sum(tp) / len(tp)  # type: ignore
        mad = sum(abs(float(tp[j]) - sm) for j in range(len(tp))) / len(tp)
        if mad <= 1e-12:
            continue
        cci = (float(tp[-1]) - sm) / (0.015 * mad)
        out.append((s, cci))
    return out


def rank_atr_momentum(
    ctx: OptimizerContext,
    i: int,
    syms: Sequence[str],
    min_bars: int,
    top_k: int,
    p: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Tuple[str, float]]:
    atr_p = int(p.get("atr_period", 14))
    roc_n = int(p.get("atr_roc_period", 10))
    if i < min_bars:
        return []
    out: List[Tuple[str, float]] = []
    for s in syms:
        c = ctx.closes_ffill.get(s)
        if c is None or i < atr_p + roc_n:
            continue
        atr = _atr_wilder(c, i, atr_p)
        c0 = _fv(c, i)
        c1 = _fv(c, i - roc_n)
        if atr is None or c0 is None or c1 is None or atr <= 1e-12:
            continue
        mom = (c0 - c1) / atr
        out.append((s, mom))
    return out


def rank_adx(
    ctx: OptimizerContext,
    i: int,
    syms: Sequence[str],
    min_bars: int,
    top_k: int,
    p: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Tuple[str, float]]:
    """Close-only ADX proxy: strength of one-bar directional movement over smoothed range."""
    period = int(p.get("adx_period", 14))
    if i < min_bars or i < period + 2:
        return []
    out: List[Tuple[str, float]] = []
    for s in syms:
        c = ctx.closes_ffill.get(s)
        if c is None:
            continue
        ups = 0.0
        downs = 0.0
        trs = 0.0
        for j in range(i - period + 1, i + 1):
            if j < 1:
                continue
            ch = _fv(c, j)
            ch1 = _fv(c, j - 1)
            tr = _true_range_proxy(c, j)
            if ch is None or ch1 is None or tr is None:
                ups = -1.0
                break
            ups += max(ch - ch1, 0.0)
            downs += max(ch1 - ch, 0.0)
            trs += tr
        if ups < 0 or trs <= 1e-12:
            continue
        di = abs(ups - downs) / trs * 100.0
        out.append((s, di))
    return out


def rank_pe_ratio(
    ctx: OptimizerContext,
    i: int,
    syms: Sequence[str],
    min_bars: int,
    top_k: int,
    p: Dict[str, Any],
    cfg: Dict[str, Any],
) -> List[Tuple[str, float]]:
    """Value: higher score = lower P/E (higher earnings yield). Time-invariant per symbol for the run."""
    pe_map = _load_pe_map(cfg)
    if not pe_map:
        return []
    out: List[Tuple[str, float]] = []
    for s in syms:
        pe = pe_map.get(s.upper())
        if pe is None or pe <= 0 or not math.isfinite(pe):
            continue
        # earnings yield proxy
        score = 1.0 / pe
        out.append((s, score))
    return out


RANKERS: Dict[str, Callable[..., List[Tuple[str, float]]]] = {
    "signal_macd": rank_macd,
    "signal_bollinger": rank_bollinger,
    "signal_sma_cross": rank_sma_cross,
    "signal_stochastic": rank_stochastic,
    "signal_williams_r": rank_williams_r,
    "signal_roc": rank_roc,
    "signal_cci": rank_cci,
    "signal_atr_momentum": rank_atr_momentum,
    "signal_adx": rank_adx,
    "signal_pe_ratio": rank_pe_ratio,
}


def sample_params(signal_key: str, rng: random.Random) -> Dict[str, Any]:
    """Random hyperparameters + execution knobs shared with RSI pipeline."""
    base = {
        "top_k": rng.randint(1, 12),
        "min_bars": rng.randint(25, 120),
        "exposure": float(rng.uniform(0.2, 1.0)),
        "dd_stop": None,
        "dd_resume": None,
    }
    if rng.random() < 0.65:
        base["dd_stop"] = float(rng.uniform(0.06, 0.10))
        base["dd_resume"] = float(rng.uniform(0.02, float(base["dd_stop"])))

    if signal_key == "signal_macd":
        base.update(
            {
                "macd_fast": rng.randint(8, 16),
                "macd_slow": rng.randint(20, 35),
                "macd_signal": rng.randint(5, 12),
            }
        )
        if base["macd_fast"] >= base["macd_slow"]:
            base["macd_slow"] = base["macd_fast"] + rng.randint(5, 15)
    elif signal_key == "signal_bollinger":
        base.update(
            {
                "bb_period": rng.randint(15, 40),
                "bb_std": float(rng.uniform(1.5, 2.5)),
                "bb_mode": float(rng.choice([-1.0, 1.0])),
            }
        )
    elif signal_key == "signal_sma_cross":
        base.update(
            {
                "sma_short": rng.randint(5, 20),
                "sma_long": rng.randint(30, 100),
            }
        )
        if base["sma_short"] >= base["sma_long"]:
            base["sma_long"] = base["sma_short"] + rng.randint(10, 40)
    elif signal_key == "signal_stochastic":
        base["stoch_k_period"] = rng.randint(8, 21)
    elif signal_key == "signal_williams_r":
        base["williams_period"] = rng.randint(8, 21)
    elif signal_key == "signal_roc":
        base["roc_period"] = rng.randint(5, 30)
    elif signal_key == "signal_cci":
        base["cci_period"] = rng.randint(10, 30)
    elif signal_key == "signal_atr_momentum":
        base.update({"atr_period": rng.randint(10, 21), "atr_roc_period": rng.randint(5, 20)})
    elif signal_key == "signal_adx":
        base["adx_period"] = rng.randint(10, 21)
    elif signal_key == "signal_pe_ratio":
        base["pe_dummy"] = rng.randint(0, 1)  # no-op; PE comes from cfg
    return base


def get_ranker(signal_key: str):
    fn = RANKERS.get(signal_key)
    if fn is None:
        raise KeyError(f"Unknown signal: {signal_key}")
    return fn


SIGNAL_DOCS = {
    "signal_macd": "MACD line minus signal (histogram), trend strength.",
    "signal_bollinger": "Bollinger %B mean-reversion (configurable direction via bb_mode).",
    "signal_sma_cross": "SMA short vs long spread (momentum / trend).",
    "signal_stochastic": "Stochastic %K from close range (mean-reversion score).",
    "signal_williams_r": "Williams %R oscillator.",
    "signal_roc": "Rate of change over N bars.",
    "signal_cci": "Commodity Channel Index (close-only typical price).",
    "signal_atr_momentum": "Price change over ATR (vol-normalized momentum).",
    "signal_adx": "Close-only directional strength proxy (DI spread).",
    "signal_pe_ratio": "Value rank by earnings yield 1/PE from pipeline_value_metrics_pe or JSON file.",
}
