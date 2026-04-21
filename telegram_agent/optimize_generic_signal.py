"""
Random search for generic cross-sectional signals (`signal_strategies` + `cross_sectional_engine`).

Uses the same optimization thresholds and rolling metrics as `optimize_rsi_mean.random_search`,
but accepts a pre-built ``OptimizerContext`` (for multi-adapter pipeline runs).
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict
from datetime import datetime, timezone
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telegram_agent.cross_sectional_engine import simulate_cross_sectional_ranked
from telegram_agent.optimize_rsi_mean import (
    OptimizeResult,
    OptimizerContext,
    _compute_calmar_oos_sharpe,
    _compute_sharpe_like,
    _max_drawdown,
    _max_drawdown_recovery_seconds,
    _optimize_thresholds,
    _percentile,
    _cfg_risk_free_annual,
    symbols_with_bar_in_window,
)
from telegram_agent.rolling_window_metrics import rolling_horizon_returns, rolling_metric_key_base, rolling_window_to_timedelta
from telegram_agent.signal_strategies import get_ranker, sample_params


def evaluate_generic_config(
    cfg: dict,
    ctx: OptimizerContext,
    *,
    start: datetime,
    end: datetime,
    signal_key: str,
    params: Dict[str, Any],
    compute_secondary: bool = False,
) -> OptimizeResult:
    syms_ok = symbols_with_bar_in_window(ctx, start, end)
    top_k = int(params.get("top_k", 5))
    min_bars = int(params.get("min_bars", 50))
    exposure = float(params.get("exposure", 1.0))
    dd_stop = params.get("dd_stop", None)
    dd_resume = params.get("dd_resume", None)
    dd_stop_f = None if dd_stop in (None, "") else float(dd_stop)
    dd_resume_f = None if dd_resume in (None, "") else float(dd_resume)

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
            params={"top_k": top_k, "min_bars": min_bars, "signal": signal_key, **params},
        )

    max_eval = int(cfg.get("competitive_backtest_max_eval_points", 2000))
    if bool(cfg.get("optimize_dense_hourly_simulation", False)):
        max_eval = 10**9

    rank_fn = get_ranker(signal_key)
    curve, legs = simulate_cross_sectional_ranked(
        ctx,
        syms_ok,
        start=start,
        end=end,
        min_bars=min_bars,
        top_k=top_k,
        params=params,
        exposure=exposure,
        dd_stop=dd_stop_f,
        dd_resume=dd_resume_f,
        max_eval_points=max_eval,
        grid_offset=0,
        rank_fn=rank_fn,
        cfg=cfg,
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
            params={"top_k": top_k, "min_bars": min_bars, "symbols_used": list(syms_ok), "signal": signal_key, **params},
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
        calmar, oos_sharpe = _compute_calmar_oos_sharpe(legs, risk_free_annual=_cfg_risk_free_annual(cfg))

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

    full_params = {
        "top_k": top_k,
        "min_bars": min_bars,
        "exposure": exposure,
        "dd_stop": dd_stop_f,
        "dd_resume": dd_resume_f,
        "symbols_used": list(syms_ok),
        "signal": signal_key,
        **{k: v for k, v in params.items() if k not in ("top_k", "min_bars", "exposure", "dd_stop", "dd_resume")},
    }

    return OptimizeResult(
        ok=ok,
        objective=med,
        median_rolling_return=med,
        rolling_hit_rate=hit,
        rolling_floor_pctl=float(pctl),
        rolling_floor_return=floor,
        max_drawdown=mdd,
        calmar=float(calmar) if calmar is not None else None,
        oos_sharpe=float(oos_sharpe) if oos_sharpe is not None else None,
        n_hours=len(curve),
        n_roll_windows=len(roll),
        params=full_params,
    )


def random_search_generic_signal(
    cfg: dict,
    ctx: OptimizerContext,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    signal_key: str,
    trials: int,
    seed: int,
) -> Dict[str, Any]:
    rng = random.Random(int(seed))
    rank_fn = get_ranker(signal_key)
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
        attempts_per_param = 1

    for param_set_index in range(int(trials)):
        p = sample_params(signal_key, rng)
        top_k = int(p.get("top_k", 5))
        min_bars = int(p.get("min_bars", 50))
        exposure = float(p.get("exposure", 1.0))
        dd_stop = p.get("dd_stop", None)
        dd_resume = p.get("dd_resume", None)
        dd_stop_f = None if dd_stop in (None, "") else float(dd_stop)
        dd_resume_f = None if dd_resume in (None, "") else float(dd_resume)

        res0 = evaluate_generic_config(
            cfg,
            ctx,
            start=start,
            end=end,
            signal_key=signal_key,
            params=p,
            compute_secondary=False,
        )
        syms_used = res0.params.get("symbols_used") or list(symbols)

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
            if bool(cfg.get("optimize_deterministic_simulation", False)):
                grid_offset = 0
            else:
                grid_offset = rng.randint(0, 10_000)
            max_pts = int(cfg.get("competitive_backtest_max_eval_points", 2000))
            if bool(cfg.get("optimize_dense_hourly_simulation", False)):
                max_pts = 10**9
            curve, legs = simulate_cross_sectional_ranked(
                ctx,
                syms_used,
                start=start,
                end=end,
                min_bars=min_bars,
                top_k=top_k,
                params=p,
                exposure=exposure,
                dd_stop=dd_stop_f,
                dd_resume=dd_resume_f,
                max_eval_points=max_pts,
                grid_offset=grid_offset,
                rank_fn=rank_fn,
                cfg=cfg,
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

            attempt_rows.append(
                {
                    "run_id": run_id,
                    "param_set_index": param_set_index,
                    "attempt_index": attempt_index,
                    "signal": signal_key,
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
            "signal": signal_key,
            "attempts": int(attempts_per_param),
            "ok": bool(ok_param),
            "ok_attempts": int(ok_attempts),
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
            **{f"p_{k}": v for k, v in (res0.params or {}).items() if k != "symbols_used"},
        }
        param_rows.append(param_row)

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
        "signal": signal_key,
        "constraints": {
            "median_rolling_return_min": cfg.get("optimize_median_rolling_return_min"),
            "rolling_hit_rate_min": cfg.get("optimize_consistency_hit_rate_min"),
            "rolling_floor_pctl": cfg.get("optimize_rolling_floor_pctl"),
            "rolling_floor_return_min": cfg.get("optimize_rolling_floor_return_min"),
            "max_drawdown_max": cfg.get("optimize_max_drawdown_max"),
        },
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "symbols_requested": list(symbols),
        "symbols_with_any_data": list(ctx.symbols_with_any_data),
        "run_id": run_id,
        "attempts": attempt_rows,
        "param_sets": param_rows,
        "best_feasible": None if best_ok is None else asdict(best_ok),
        "best_overall": None if best_any is None else asdict(best_any),
    }
