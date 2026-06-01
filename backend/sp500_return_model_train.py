#!/usr/bin/env python3
"""
Train the S&P 500 forward-return regression model for one or more cadences.

Examples:
    # Smoke test (30 tickers, short history, all cadences) — fast sanity run.
    python backend/sp500_return_model_train.py --smoke

    # Full training: default 30y history, 50%/25%/25% chronological split, SPY-risk-weighted ML loss.
    python backend/sp500_return_model_train.py --cadence all

    # Just the weekly model with explicit split fractions.
    python backend/sp500_return_model_train.py --cadence weekly --split 0.5,0.25,0.25
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "backend"))

from sp500_return_model import (
    CADENCES,
    DATA_DIR,
    TrainConfig,
    load_prices,
    save_artifacts,
    train_one_cadence,
)
from rsi_mean_strategy import (
    RsiBacktestConfig,
    evaluate_rsi_mean,
    save_artifacts as save_rsi_artifacts,
)
from sp500_return_model_walkforward import run_walk_forward, save_walk_forward_json
from sp500_quality_evaluator import load_sp500_symbols


def _parse_split_fracs(arg: str) -> Tuple[float, float, float]:
    parts = [p.strip() for p in str(arg).split(",") if p.strip()]
    if len(parts) != 3:
        raise SystemExit("--split expects three comma-separated fractions, e.g. 0.5,0.25,0.25")
    tf, vf, sf = float(parts[0]), float(parts[1]), float(parts[2])
    if abs(tf + vf + sf - 1.0) > 1e-4:
        raise SystemExit(f"--split fractions must sum to 1, got {tf + vf + sf}")
    return tf, vf, sf


def _parse_cadences(arg: str) -> List[str]:
    if not arg or arg == "all":
        return list(CADENCES)
    parts = [p.strip().lower() for p in arg.split(",") if p.strip()]
    bad = [p for p in parts if p not in CADENCES]
    if bad:
        raise SystemExit(f"unknown cadence(s) {bad}; choose from {list(CADENCES)} or 'all'")
    return parts


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Train SP500 forward-return models per cadence.")
    ap.add_argument("--cadence", default="all", help="Comma-separated subset of {daily,weekly,monthly} or 'all' (default).")
    ap.add_argument(
        "--years",
        type=float,
        default=30.0,
        help="History depth in years (default 30; use deep history for SPY-vol alignment).",
    )
    ap.add_argument("--top-n", type=int, default=50, help="Top-N portfolio size used for backtest reporting.")
    ap.add_argument(
        "--split",
        default="0.5,0.25,0.25",
        help="Train,validation,test fractions of the loaded timeline (default 50%%,25%%,25%%).",
    )
    ap.add_argument(
        "--spy-risk-kappa-vol",
        type=float,
        default=2.0,
        help="ML training: weight stocks more when trailing ann. vol is near SPY (0 disables vol term).",
    )
    ap.add_argument(
        "--spy-risk-kappa-beta",
        type=float,
        default=1.0,
        help="ML training: weight stocks more when beta is near 1 (0 disables beta term).",
    )
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--smoke", action="store_true", help="Use a small symbol subset and 2y history for quick validation.")
    ap.add_argument("--symbols", default="", help="Comma-separated override for the universe (otherwise SP500).")
    ap.add_argument("--refresh-prices", action="store_true", help="Force-refresh price cache.")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--ml-weightings",
        default="equal,score_weighted",
        help="Comma-separated weightings to backtest for the ML model (default: both equal & score_weighted).",
    )
    ap.add_argument(
        "--skip-rsi",
        action="store_true",
        help="Don't also fit the rsi_mean baseline (default: run it for every cadence).",
    )
    ap.add_argument("--rsi-window-days", type=int, default=30, help="Window for averaging daily RSI(14).")
    ap.add_argument(
        "--walkforward",
        action="store_true",
        help="After each ML cadence, run yearly walk-forward (up to 20 test years) and save walkforward_{cadence}.json.",
    )
    ap.add_argument(
        "--walkforward-no-tc",
        action="store_true",
        help="With --walkforward: disable transaction costs in walk-forward folds.",
    )
    ap.add_argument("--walkforward-max-folds", type=int, default=20)
    ap.add_argument("--walkforward-min-train-rows", type=int, default=0, help="0 = use 300 if --smoke else 2000")
    args = ap.parse_args(list(argv) if argv is not None else None)

    cadences = _parse_cadences(args.cadence)
    if args.symbols.strip():
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = load_sp500_symbols()
    tf, vf, sf = _parse_split_fracs(args.split)

    if args.smoke:
        symbols = symbols[:30]
        # Need enough history for monthly cadence (sampling ~12 obs/year/symbol).
        years = 5.0
        # Force top_n to be smaller than the universe, otherwise every strategy holds
        # the same equal-weight basket of every symbol and they all look identical.
        if int(args.top_n) >= len(symbols):
            args.top_n = max(5, len(symbols) // 3)
            print(f"[smoke] capped --top-n to {args.top_n} (universe size {len(symbols)})")
        print(f"[smoke] using {len(symbols)} symbols, {years}y history, split={tf},{vf},{sf}")
    else:
        years = float(args.years)

    universe = list(set(symbols) | {args.benchmark.upper()})
    print(f"loading prices for {len(universe)} symbols ({years}y, refresh={args.refresh_prices})…", flush=True)
    prices = load_prices(universe, years=years, refresh=args.refresh_prices)
    print(f"  loaded {len(prices)} price series", flush=True)

    weightings = tuple(w.strip() for w in args.ml_weightings.split(",") if w.strip())
    summary: Dict[str, Any] = {"trained_at": datetime.now(timezone.utc).isoformat(), "cadences": {}}
    for c in cadences:
        cad_summary: Dict[str, Any] = {"variants": {}}
        print(f"\n=== training cadence={c} ===", flush=True)
        cfg = TrainConfig(
            cadence=c,
            symbols=symbols,
            years=years,
            split_train_frac=tf,
            split_val_frac=vf,
            split_test_frac=sf,
            spy_risk_align_kappa_vol=float(args.spy_risk_kappa_vol),
            spy_risk_align_kappa_beta=float(args.spy_risk_kappa_beta),
            top_n=int(args.top_n),
            benchmark=args.benchmark.upper(),
            seed=int(args.seed),
            weightings=weightings,
        )
        try:
            results, model = train_one_cadence(cfg, prices=prices)
        except Exception as e:
            print(f"  cadence {c} ML failed: {e}", file=sys.stderr)
            cad_summary["error"] = str(e)
            summary["cadences"][c] = cad_summary
            continue
        written = save_artifacts(results, model)
        for r, p in zip(results, written[1:]):
            print(
                f"  [{r.strategy_id}] saved -> {p.name}: "
                f"val_ret={r.val_metrics.get('total_return'):.3f} "
                f"test_ret={r.test_metrics.get('total_return'):.3f} "
                f"vs SPY test={r.baseline_test_metrics.get('total_return'):.3f}  "
                f"IC val={r.val_ic:.4f} test={r.test_ic:.4f}"
            )
            cad_summary["variants"][r.strategy_id] = {
                "metrics_path": str(p),
                "weighting": r.weighting,
                "val_total_return": r.val_metrics.get("total_return"),
                "test_total_return": r.test_metrics.get("total_return"),
            }

        if not args.skip_rsi:
            print(f"  [rsi_mean] evaluating RSI mean-reversion sleeve …", flush=True)
            try:
                rsi_cfg = RsiBacktestConfig(
                    cadence=c,
                    symbols=symbols,
                    years=years,
                    split_train_frac=tf,
                    split_val_frac=vf,
                    split_test_frac=sf,
                    top_n=int(args.top_n),
                    benchmark=args.benchmark.upper(),
                    rsi_window_days=int(args.rsi_window_days),
                )
                rsi_res = evaluate_rsi_mean(rsi_cfg, prices=prices)
                rsi_path = save_rsi_artifacts(rsi_res)
                print(
                    f"  [rsi_mean] saved -> {rsi_path.name}: "
                    f"val_ret={rsi_res.val_metrics.get('total_return'):.3f} "
                    f"test_ret={rsi_res.test_metrics.get('total_return'):.3f} "
                    f"vs SPY test={rsi_res.baseline_test_metrics.get('total_return'):.3f}"
                )
                cad_summary["variants"]["rsi_mean"] = {
                    "metrics_path": str(rsi_path),
                    "val_total_return": rsi_res.val_metrics.get("total_return"),
                    "test_total_return": rsi_res.test_metrics.get("total_return"),
                }
            except Exception as e:
                print(f"  [rsi_mean] failed: {e}", file=sys.stderr)
                cad_summary["rsi_error"] = str(e)

        if args.walkforward:
            min_tr = (
                int(args.walkforward_min_train_rows)
                if int(args.walkforward_min_train_rows) > 0
                else (300 if args.smoke else 2000)
            )
            wf_cfg = TrainConfig(
                cadence=c,
                symbols=symbols,
                years=years,
                split_train_frac=tf,
                split_val_frac=vf,
                split_test_frac=sf,
                spy_risk_align_kappa_vol=float(args.spy_risk_kappa_vol),
                spy_risk_align_kappa_beta=float(args.spy_risk_kappa_beta),
                top_n=int(args.top_n),
                benchmark=args.benchmark.upper(),
                seed=int(args.seed),
                weightings=weightings,
                refresh_prices=False,
                tc_enabled=not bool(args.walkforward_no_tc),
            )
            try:
                payload = run_walk_forward(
                    wf_cfg,
                    prices=prices,
                    max_folds=int(args.walkforward_max_folds),
                    min_train_rows=min_tr,
                )
                wf_path = save_walk_forward_json(payload)
                print(f"  [walkforward] wrote {wf_path.name} folds={payload.get('n_folds_completed')}", flush=True)
                cad_summary["walkforward_path"] = str(wf_path)
            except Exception as e:
                print(f"  [walkforward] failed: {e}", file=sys.stderr)
                cad_summary["walkforward_error"] = str(e)

        summary["cadences"][c] = cad_summary

    out_path = DATA_DIR / "sp500_return_models" / "training_summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote training summary -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
