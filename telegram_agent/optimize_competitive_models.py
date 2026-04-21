"""
Optimize alternative cross-sectional models on COMPETITIVE_BACKTEST_SYMBOLS under the
same OPTIMIZE_* objective/constraints used for strategy optimization.

Models included:
- momentum: rank by lookback % change
- donchian_breakout: rank by (close / max(N-1)) - 1 with an optional proximity threshold

This reuses the same hourly cache + configurable rolling-horizon metrics as optimize_rsi_mean.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telegram_agent.config import DATA_DIR, load_config
from telegram_agent.optimize_rsi_mean import (
    OptimizerContext,
    OptimizeResult,
    TradeLeg,
    _load_dotenv_like_other_modules,
    _optimize_thresholds,
    _parse_iso_or_date,
    _percentile,
    _symbols_from_competitive_env,
    build_context,
    evaluate_config,
)
from telegram_agent.rolling_window_metrics import (
    rolling_horizon_returns,
    rolling_metric_key_base,
    rolling_window_to_timedelta,
)


@dataclass(frozen=True)
class MomentumParams:
    lookback: int


def momentum_score(closes: Sequence[float], p: MomentumParams) -> Optional[float]:
    lb = int(p.lookback)
    if lb < 1 or len(closes) < lb + 1:
        return None
    c0 = float(closes[-1])
    c1 = float(closes[-1 - lb])
    if c1 <= 0:
        return None
    return float((c0 / c1 - 1.0) * 100.0)


@dataclass(frozen=True)
class DonchianParams:
    window: int
    proximity: float  # e.g. 0.002 means within 0.2% of the high


def donchian_score(closes: Sequence[float], p: DonchianParams) -> Optional[float]:
    w = int(p.window)
    if w < 5 or len(closes) < w + 1:
        return None
    body = list(map(float, closes[-w:-1]))
    last = float(closes[-1])
    mx = max(body) if body else None
    if mx is None or mx <= 0:
        return None
    prox = max(0.0, float(p.proximity))
    if last < mx * (1.0 - prox):
        return None
    return float((last / mx - 1.0) * 100.0 + 2.0)


def _override_rsi_scoring_to_custom(
    ctx: OptimizerContext,
    *,
    scorer_name: str,
    scorer_params: Dict[str, Any],
) -> OptimizerContext:
    # We piggyback on evaluate_config's infrastructure by temporarily swapping out
    # the RSI scorer in optimize_rsi_mean at runtime is not clean; instead we use a
    # trick: encode a "custom scorer" into cfg and have a small wrapper in this file.
    # For simplicity (and speed), we just re-run a local evaluation loop here.
    raise NotImplementedError


def _simulate_with_custom_scorer(
    cfg: dict,
    *,
    ctx: OptimizerContext,
    start: datetime,
    end: datetime,
    min_bars: int,
    top_k: int,
    exposure: float,
    dd_stop: Optional[float],
    dd_resume: Optional[float],
    scorer_name: str,
    scorer_fn,
    scorer_params,
) -> OptimizeResult:
    """
    Same evaluation pipeline as optimize_rsi_mean, but with a custom scorer.
    We inline a minimal evaluation loop to avoid refactoring the RSI optimizer further.
    """
    from telegram_agent.optimize_rsi_mean import _max_drawdown

    # Filter symbols with any data in window.
    syms_ok = []
    for s in ctx.symbols_with_any_data:
        ser = ctx.cache.get(s) or []
        if any(t >= start and t <= end for t, _ in ser):
            syms_ok.append(s)
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
            params={"top_k": top_k, "min_bars": min_bars},
        )

    # Copy of simulate_hourly_rebalance with injected scorer (kept small).
    # Reference timeline: densest symbol within window.
    densest = []
    for s in syms_ok:
        ser = ctx.cache.get(s) or []
        in_w = [x for x in ser if x[0] >= start and x[0] <= end]
        densest.append((s, len(in_w)))
    densest.sort(key=lambda x: x[1], reverse=True)
    if not densest or densest[0][1] < (min_bars + 10):
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
            params={"top_k": top_k, "min_bars": min_bars},
        )
    ref_sym = densest[0][0]
    times = [t for t, _ in (ctx.cache.get(ref_sym) or []) if t >= start and t <= end]
    if len(times) < min_bars + 2:
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
            params={"top_k": top_k, "min_bars": min_bars},
        )

    # Align prices to reference timeline.
    aligned: Dict[str, List[Optional[float]]] = {}
    for s in syms_ok:
        ser = [(t, float(px)) for (t, px) in (ctx.cache.get(s) or []) if t >= start and t <= end]
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

    closes_hist: Dict[str, List[float]] = {s: [] for s in syms_ok}
    equity = 1.0
    peak = 1.0
    risk_off = False
    exp = max(0.0, min(1.0, float(exposure)))
    dd_s = None if dd_stop is None else max(0.0, min(0.95, float(dd_stop)))
    dd_r = None if dd_resume is None else max(0.0, min(0.95, float(dd_resume)))
    if dd_s is not None and dd_r is not None and dd_r > dd_s:
        dd_r = dd_s * 0.75

    curve: List[Tuple[datetime, float]] = []
    legs: List[TradeLeg] = []

    for i in range(len(times) - 1):
        t0 = times[i]
        t1 = times[i + 1]
        for s in syms_ok:
            a = aligned.get(s)
            if not a:
                continue
            px0 = a[i]
            if px0 is None:
                continue
            closes_hist[s].append(float(px0))

        if i < min_bars:
            curve.append((t0, equity))
            continue

        ranked: List[Tuple[str, float]] = []
        for s in syms_ok:
            closes = closes_hist[s]
            if len(closes) < min_bars:
                continue
            sc = scorer_fn(closes, scorer_params)
            if sc is None or not math.isfinite(sc):
                continue
            ranked.append((s, float(sc)))
        ranked.sort(key=lambda x: x[1], reverse=True)
        picks = [s for s, _ in ranked[: max(1, int(top_k))]]
        if not picks:
            curve.append((t0, equity))
            continue

        peak = max(peak, equity)
        dd_now = (peak - equity) / peak if peak > 0 else 0.0
        if dd_s is not None and dd_now >= dd_s:
            risk_off = True
        if risk_off and dd_r is not None and dd_now <= dd_r:
            risk_off = False
        exp_eff = 0.0 if risk_off else exp

        rets: List[float] = []
        for s in picks:
            a = aligned.get(s)
            if not a:
                continue
            p0 = a[i]
            p1 = a[i + 1]
            if p0 is None or p1 is None or p0 <= 0:
                continue
            rets.append(float(p1 / p0 - 1.0))
        if not rets:
            curve.append((t0, equity))
            continue
        step_ret = float(sum(rets) / len(rets))
        equity *= 1.0 + exp_eff * step_ret
        curve.append((t1, equity))
        legs.append(TradeLeg(entry=t0, exit=t1, symbol="BASKET", realized_pct=(exp_eff * step_ret) * 100.0))

    if len(curve) < 100:
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
            params={},
        )

    ts = [t for t, _ in curve]
    eq = [float(x) for _, x in curve]
    rw_td = rolling_window_to_timedelta(str(cfg.get("optimize_rolling_window", "1y")))
    roll = rolling_horizon_returns(ts, eq, rw_td)
    obj_min, hit_min, floor_min, pctl = _optimize_thresholds(cfg)
    med = float(sorted(roll)[len(roll) // 2]) if roll else None
    hit = float(sum(1 for x in roll if x > 0) / len(roll)) if roll else None
    floor = _percentile(roll, pctl) if roll else None
    mdd = _max_drawdown(eq) if eq else None

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

    return OptimizeResult(
        ok=ok,
        objective=med,
        median_rolling_return=med,
        rolling_hit_rate=hit,
        rolling_floor_pctl=float(pctl),
        rolling_floor_return=floor,
        max_drawdown=mdd,
        calmar=None,
        oos_sharpe=None,
        n_hours=len(curve),
        n_roll_windows=len(roll),
        params={
            "model": scorer_name,
            "top_k": int(top_k),
            "min_bars": int(min_bars),
            "exposure": float(exposure),
            "dd_stop": dd_stop,
            "dd_resume": dd_resume,
            **(scorer_params if isinstance(scorer_params, dict) else asdict(scorer_params)),
        },
    )


def search_models(cfg: dict, *, symbols: Sequence[str], start: datetime, end: datetime, trials: int, seed: int) -> Dict[str, Any]:
    rng = random.Random(int(seed))
    ctx = build_context(cfg, symbols=symbols)

    best_by_model: Dict[str, Optional[OptimizeResult]] = {"momentum": None, "donchian": None}

    for _ in range(int(trials)):
        top_k = rng.randint(1, 15)
        min_bars = rng.randint(25, 220)
        exposure = float(rng.uniform(0.15, 1.0))
        dd_stop = float(rng.uniform(0.06, 0.10))
        dd_resume = float(rng.uniform(0.02, dd_stop))

        # Momentum
        mp = MomentumParams(lookback=rng.randint(3, 200))
        res_m = _simulate_with_custom_scorer(
            cfg,
            ctx=ctx,
            start=start,
            end=end,
            min_bars=min_bars,
            top_k=top_k,
            exposure=exposure,
            dd_stop=dd_stop,
            dd_resume=dd_resume,
            scorer_name="momentum",
            scorer_fn=momentum_score,
            scorer_params=mp,
        )
        cur = best_by_model["momentum"]
        if cur is None or (res_m.objective is not None and (cur.objective or -1e9) < res_m.objective):
            best_by_model["momentum"] = res_m

        # Donchian
        dp = DonchianParams(window=rng.randint(10, 220), proximity=float(rng.uniform(0.0, 0.01)))
        res_d = _simulate_with_custom_scorer(
            cfg,
            ctx=ctx,
            start=start,
            end=end,
            min_bars=min_bars,
            top_k=top_k,
            exposure=exposure,
            dd_stop=dd_stop,
            dd_resume=dd_resume,
            scorer_name="donchian_breakout",
            scorer_fn=donchian_score,
            scorer_params=dp,
        )
        cur = best_by_model["donchian"]
        if cur is None or (res_d.objective is not None and (cur.objective or -1e9) < res_d.objective):
            best_by_model["donchian"] = res_d

    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "rolling_window": cfg.get("optimize_rolling_window", "1y"),
        "rolling_metric_suffix": cfg.get("optimize_rolling_metric_suffix", "1y"),
        "rolling_metric_keys": dict(
            rolling_metric_key_base(str(cfg.get("optimize_rolling_metric_suffix", "1y")))
        ),
        "symbols_requested": list(ctx.symbols_requested),
        "symbols_with_any_data": list(ctx.symbols_with_any_data),
        "best_by_model": {k: (None if v is None else asdict(v)) for k, v in best_by_model.items()},
    }


def main() -> None:
    _load_dotenv_like_other_modules()
    cfg = load_config()

    p = argparse.ArgumentParser(description="Optimize momentum/breakout models on COMPETITIVE_BACKTEST_SYMBOLS")
    p.add_argument("--start", type=str, default="2024-06-01")
    p.add_argument("--end", type=str, default="2026-04-01")
    p.add_argument("--trials", type=int, default=300)
    p.add_argument("--seed", type=int, default=9)
    p.add_argument("--out", type=str, default=str(DATA_DIR / "optimize_competitive_models.json"))
    args = p.parse_args()

    start = _parse_iso_or_date(args.start, is_end=False)
    end = _parse_iso_or_date(args.end, is_end=True)
    syms = _symbols_from_competitive_env(cfg)
    if not syms:
        raise SystemExit("COMPETITIVE_BACKTEST_SYMBOLS is empty; set it in .env")

    report = search_models(cfg, symbols=syms, start=start, end=end, trials=int(args.trials), seed=int(args.seed))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()

