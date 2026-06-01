#!/usr/bin/env python3
"""
Walk-forward ablation study for ML top-N strategy improvements.

Compares the pre-change baseline stack vs the current full stack, then isolates
each change (add-one-from-baseline and leave-one-out-from-full). Results are written
to ``backend/data/sp500_return_models/ablation_{cadence}.json``.

Example:
    python backend/sp500_return_model_ablation.py --cadence daily --max-folds 10
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO / "backend"))

from sp500_quality_evaluator import load_sp500_symbols  # noqa: E402
from sp500_return_model import MODEL_DIR, TrainConfig, load_prices  # noqa: E402
from sp500_return_model_walkforward import run_walk_forward  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AblationSpec:
    id: str
    label: str
    group: str  # anchor | add_one | leave_one_out | weighting
    top_n: int
    kappa_vol: float
    kappa_beta: float
    trend_filter: bool
    pred_smoothing_days: int
    train_lookback_years: int
    score_weighted_scheme: str = "rank_decay"
    weightings: Tuple[str, ...] = ("equal",)


def _specs_for_cadence(cadence: str) -> List[AblationSpec]:
    smooth_daily = 5 if cadence == "daily" else 0
    old = dict(top_n=50, kappa_vol=2.0, kappa_beta=1.0, trend_filter=False, pred_smoothing_days=0, train_lookback_years=0)
    new = dict(top_n=20, kappa_vol=0.0, kappa_beta=0.5, trend_filter=True, pred_smoothing_days=smooth_daily, train_lookback_years=5)

    def s(id_: str, label: str, group: str, base: Optional[Dict[str, Any]] = None, **kw) -> AblationSpec:
        merged = {**(base or old), **kw}
        return AblationSpec(id=id_, label=label, group=group, **merged)

    specs: List[AblationSpec] = [
        AblationSpec(id="old_baseline", label="Old baseline (pre-improvements)", group="anchor", weightings=("equal", "score_weighted"), score_weighted_scheme="linear_shifted", **old),
        AblationSpec(id="new_full", label="New full stack (all improvements)", group="anchor", weightings=("equal", "score_weighted"), score_weighted_scheme="rank_decay", **new),
        # Add-one from old baseline
        s("add_trend_filter", "+ SPY 200d trend filter only", "add_one", trend_filter=True),
        s("add_pred_smoothing", f"+ prediction smoothing (K={smooth_daily}) only", "add_one", pred_smoothing_days=smooth_daily),
        s("add_training_defaults", "+ training defaults (top_n=20, κ_vol=0, κ_β=0.5) only", "add_one", top_n=20, kappa_vol=0.0, kappa_beta=0.5),
        s("add_sliding_train", "+ 5y sliding training window only", "add_one", train_lookback_years=5),
        AblationSpec(
            id="add_rank_decay_weights",
            label="+ rank-decay score weights only (vs legacy linear)",
            group="add_one",
            weightings=("score_weighted",),
            score_weighted_scheme="rank_decay",
            **old,
        ),
        # Training sub-components
        s("add_top_n_only", "+ top_n=20 only (old κ)", "add_one_train", top_n=20),
        s("add_kappa_only", "+ κ_vol=0 / κ_β=0.5 only (old top_n)", "add_one_train", kappa_vol=0.0, kappa_beta=0.5),
        # Leave-one-out from new full
        s("drop_trend_filter", "New full − trend filter", "leave_one_out", base=new, trend_filter=False),
        s("drop_pred_smoothing", "New full − prediction smoothing", "leave_one_out", base=new, pred_smoothing_days=0),
        s(
            "drop_training_defaults",
            "New full − training defaults (revert top_n/κ)",
            "leave_one_out",
            base=new,
            top_n=50,
            kappa_vol=2.0,
            kappa_beta=1.0,
        ),
        s("drop_sliding_train", "New full − sliding train window", "leave_one_out", base=new, train_lookback_years=0),
        AblationSpec(
            id="drop_rank_decay_weights",
            label="New full − rank-decay (legacy linear weights)",
            group="leave_one_out",
            weightings=("score_weighted",),
            score_weighted_scheme="linear_shifted",
            **new,
        ),
    ]
    return specs


def _cfg_from_spec(spec: AblationSpec, *, cadence: str, symbols: List[str], years: float, seed: int) -> TrainConfig:
    return TrainConfig(
        cadence=cadence,
        symbols=symbols,
        years=years,
        split_train_frac=0.5,
        split_val_frac=0.25,
        split_test_frac=0.25,
        spy_risk_align_kappa_vol=float(spec.kappa_vol),
        spy_risk_align_kappa_beta=float(spec.kappa_beta),
        top_n=int(spec.top_n),
        seed=int(seed),
        refresh_prices=False,
        weightings=spec.weightings,
        tc_enabled=True,
        pred_smoothing_days=int(spec.pred_smoothing_days),
        trend_filter_enabled=bool(spec.trend_filter),
        trend_filter_ma_days=200,
        score_weighted_scheme=str(spec.score_weighted_scheme),
    )


def _extract_row(payload: Dict[str, Any], strategy_id: str) -> Dict[str, Any]:
    agg = (payload.get("aggregate") or {}).get(strategy_id) or {}
    row: Dict[str, Any] = {"n_folds": int(payload.get("n_folds_completed") or 0)}
    for prefix, keys in (
        ("strategy", ("total_return", "cagr", "sharpe", "max_drawdown", "turnover_avg")),
        ("baseline", ("total_return", "sharpe", "max_drawdown")),
    ):
        for k in keys:
            mk = f"{prefix}_{k}"
            block = agg.get(mk) or {}
            row[mk] = block.get("mean")
    return row


def _delta_vs(ref: Dict[str, Any], cur: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for k in ("strategy_total_return", "strategy_sharpe", "strategy_max_drawdown", "strategy_turnover_avg"):
        rv, cv = ref.get(k), cur.get(k)
        if rv is None or cv is None:
            continue
        if "drawdown" in k:
            out[f"d_{k}"] = float(cv) - float(rv)  # less negative = better
        else:
            out[f"d_{k}"] = float(cv) - float(rv)
    return out


def run_ablation(
    *,
    cadence: str,
    symbols: List[str],
    prices: Dict[str, Any],
    specs: Sequence[AblationSpec],
    max_folds: int,
    min_train_rows: int,
    years: float,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    t0 = time.time()
    for i, spec in enumerate(specs):
        logger.info("[%d/%d] %s", i + 1, len(specs), spec.id)
        cfg = _cfg_from_spec(spec, cadence=cadence, symbols=symbols, years=years, seed=7)
        try:
            payload = run_walk_forward(
                cfg,
                prices=prices,
                max_folds=max_folds,
                min_train_rows=min_train_rows,
                train_lookback_years=int(spec.train_lookback_years),
            )
        except Exception as e:
            logger.warning("ablation %s failed: %s", spec.id, e)
            results.append({"id": spec.id, "label": spec.label, "group": spec.group, "error": str(e)})
            continue
        sid = "ml_equal" if "equal" in spec.weightings else "ml_pred_weighted"
        row = _extract_row(payload, sid)
        results.append(
            {
                "id": spec.id,
                "label": spec.label,
                "group": spec.group,
                "strategy_id": sid,
                "config": {
                    "top_n": spec.top_n,
                    "kappa_vol": spec.kappa_vol,
                    "kappa_beta": spec.kappa_beta,
                    "trend_filter": spec.trend_filter,
                    "pred_smoothing_days": spec.pred_smoothing_days,
                    "train_lookback_years": spec.train_lookback_years,
                    "score_weighted_scheme": spec.score_weighted_scheme,
                    "weightings": list(spec.weightings),
                },
                "metrics": row,
            }
        )
    elapsed = time.time() - t0
    ref_old = next((r for r in results if r.get("id") == "old_baseline" and "metrics" in r), None)
    ref_new = next((r for r in results if r.get("id") == "new_full" and "metrics" in r), None)
    if ref_old and ref_new:
        for r in results:
            if "metrics" not in r:
                continue
            r["delta_vs_old_baseline"] = _delta_vs(ref_old["metrics"], r["metrics"])
            r["delta_vs_new_full"] = _delta_vs(ref_new["metrics"], r["metrics"])
    return {
        "cadence": cadence,
        "max_folds": max_folds,
        "min_train_rows": min_train_rows,
        "elapsed_seconds": elapsed,
        "variants": results,
    }


def _print_table(report: Dict[str, Any]) -> None:
    rows = [r for r in report["variants"] if "metrics" in r]
    if not rows:
        print("No results.")
        return

    def pct(v: Optional[float]) -> str:
        if v is None or v != v:
            return "n/a"
        return f"{100.0 * float(v):6.2f}%"

    def num(v: Optional[float]) -> str:
        if v is None or v != v:
            return "n/a"
        return f"{float(v):6.2f}"

    hdr = f"{'ID':<28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>8} {'Turn':>7}  {'Δret vs old':>11} {'Δsh vs old':>10}"
    print()
    print(f"=== Ablation ({report['cadence']}, {rows[0]['metrics'].get('n_folds', '?')} folds, primary: equal-weight) ===")
    print(hdr)
    print("-" * len(hdr))
    ref = next((r["metrics"] for r in rows if r["id"] == "old_baseline"), None)
    for r in rows:
        m = r["metrics"]
        d = r.get("delta_vs_old_baseline") or {}
        d_ret = d.get("d_strategy_total_return")
        d_sh = d.get("d_strategy_sharpe")
        d_ret_s = f"{100*d_ret:+6.2f}pp" if d_ret is not None else "       —"
        d_sh_s = f"{d_sh:+6.2f}" if d_sh is not None else "     —"
        print(
            f"{r['id']:<28} {pct(m.get('strategy_total_return')):>8} "
            f"{num(m.get('strategy_sharpe')):>7} {pct(m.get('strategy_max_drawdown')):>8} "
            f"{pct(m.get('strategy_turnover_avg')):>7}  {d_ret_s:>11} {d_sh_s:>10}"
        )
    print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Walk-forward ablation for ML strategy improvements.")
    ap.add_argument("--cadence", default="daily", choices=("daily", "weekly", "monthly"))
    ap.add_argument("--max-folds", type=int, default=10)
    ap.add_argument("--min-train-rows", type=int, default=2000)
    ap.add_argument("--years", type=float, default=30.0)
    args = ap.parse_args(list(argv) if argv is not None else None)

    cadence = str(args.cadence)
    syms = load_sp500_symbols()
    universe = list(set(syms) | {"SPY"})
    print(f"Loading prices for {len(universe)} symbols …", flush=True)
    prices = load_prices(universe, years=float(args.years), refresh=False)

    specs = _specs_for_cadence(cadence)
    report = run_ablation(
        cadence=cadence,
        symbols=syms,
        prices=prices,
        specs=specs,
        max_folds=int(args.max_folds),
        min_train_rows=int(args.min_train_rows),
        years=float(args.years),
    )
    out = MODEL_DIR / f"ablation_{cadence}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out}", file=sys.stderr)
    _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
