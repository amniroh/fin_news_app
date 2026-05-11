"""
HTTP endpoints that expose snapshots from `sp500_quality_evaluator.py` (quality screen,
ML top-N equal/predicted-return-weighted, RSI mean-reversion) and the backtest metrics
saved by their training scripts.

Layout on disk (all under backend/data):
    quality_evaluator_last_run_{strategy}_{cadence}.json   — evaluator snapshots.
    sp500_return_models/sp500_return_model_{cadence}_metrics.json
    sp500_return_models/sp500_return_model_{cadence}_score_weighted_metrics.json
    rsi_mean_models/rsi_mean_model_{cadence}_metrics.json

Strategy id mapping (URL path `/strategy/snapshot/{strategy}/{cadence}`):
    quality           -> quality screen
    ml                -> ML top-N equal-weight
    ml_pred_weighted  -> ML top-N weighted linearly by predicted return
    rsi_mean          -> bottom-N by 30d mean of daily RSI(14) (mean-reversion)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

import pandas as pd

try:
    # When launched with backend/ on sys.path (most scripts).
    from sp500_return_model import _equity_curve_payload, _summary_metrics, _yahoo_symbol, load_prices
except ModuleNotFoundError:
    # When imported as a module from repo root (some environments).
    import sys

    _REPO = Path(__file__).resolve().parents[1]
    if str(_REPO / "backend") not in sys.path:
        sys.path.insert(0, str(_REPO / "backend"))
    from sp500_return_model import _equity_curve_payload, _summary_metrics, _yahoo_symbol, load_prices


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "backend" / "data"
ML_DIR = DATA_DIR / "sp500_return_models"
RSI_DIR = DATA_DIR / "rsi_mean_models"

_SNAP_RE = re.compile(r"^quality_evaluator_last_run_(?P<strategy>[a-z0-9_]+)_(?P<cadence>[a-z0-9]+)\.json$")

VALID_CADENCES = ("daily", "weekly", "monthly")
ML_STRATEGIES = ("ml", "ml_pred_weighted")
ALL_STRATEGIES = ("quality", "ml", "ml_pred_weighted", "rsi_mean")

# Human-friendly metadata used by both /list and /snapshot to keep frontend in sync.
STRATEGY_META: Dict[str, Dict[str, Any]] = {
    "quality": {
        "label": "Quality screen",
        "description": "Profitability + stability + value composite rank (ROE/op-margin/FCF + leverage/liquidity/earnings-CV/vol + P/E/P/B/EV-EBITDA).",
        "color": "#2b6cb0",
    },
    "ml": {
        "label": "ML top-N (equal-weight)",
        "description": "LightGBM regression on standard cross-sectional momentum/reversal/vol/MA/RSI/MACD/Bollinger/beta/volume features. Top-N predicted forward return, equal-weighted.",
        "color": "#38a169",
    },
    "ml_pred_weighted": {
        "label": "ML top-N (predicted-return weighted)",
        "description": "Same regression model as `ml`; weights are linear in predicted profit (shifted-positive then normalised), so the highest expected returns get the largest sleeve.",
        "color": "#805ad5",
    },
    "rsi_mean": {
        "label": "RSI mean (mean-reversion)",
        "description": "Bottom-N by 30-day rolling mean of daily RSI(14) (most oversold). Equal-weighted, rebalanced at the chosen cadence.",
        "color": "#dd6b20",
    },
}

EXTRA_BASELINE_SYMBOLS: List[str] = ["GOOGL", "AAPL", "AMZN", "MU", "NVDA"]
BASELINE_COLORS: Dict[str, str] = {
    "SPY": "#a0aec0",
    "AAPL": "#111827",
    "AMZN": "#f59e0b",
    "GOOGL": "#2563eb",
    "MU": "#10b981",
    "NVDA": "#22c55e",
}


def _load_json(p: Path) -> Optional[Dict[str, Any]]:
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _curve_or_empty(v: Any) -> List[Dict[str, Any]]:
    """Metrics JSON may omit curves (older trainer); normalize for API consumers."""
    return v if isinstance(v, list) else []


def _metrics_path_for(strategy: str, cadence: str) -> Optional[Path]:
    if strategy == "ml" and cadence in VALID_CADENCES:
        return ML_DIR / f"sp500_return_model_{cadence}_metrics.json"
    if strategy == "ml_pred_weighted" and cadence in VALID_CADENCES:
        return ML_DIR / f"sp500_return_model_{cadence}_score_weighted_metrics.json"
    if strategy == "rsi_mean" and cadence in VALID_CADENCES:
        return RSI_DIR / f"rsi_mean_model_{cadence}_metrics.json"
    return None


def _snapshot_path_for(strategy: str, cadence: str) -> Path:
    cad = cadence or "none"
    return DATA_DIR / f"quality_evaluator_last_run_{strategy}_{cad}.json"


def _list_snapshots() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not DATA_DIR.is_dir():
        return items
    for p in sorted(DATA_DIR.iterdir()):
        m = _SNAP_RE.match(p.name)
        if not m:
            continue
        snap = _load_json(p) or {}
        strat = m.group("strategy")
        items.append(
            {
                "strategy": strat,
                "cadence": m.group("cadence") if m.group("cadence") != "none" else None,
                "ts_utc": snap.get("ts_utc"),
                "top_symbols": snap.get("top_symbols", [])[:10],
                "n_top": len(snap.get("top_symbols", [])),
                "snapshot_path": str(p.relative_to(REPO_ROOT)),
                "label": STRATEGY_META.get(strat, {}).get("label", strat),
            }
        )
    return items


def _list_metrics_files() -> List[Dict[str, Any]]:
    """Every (strategy, cadence) for which a backtest metrics JSON exists."""
    out: List[Dict[str, Any]] = []
    for strat in ("ml", "ml_pred_weighted", "rsi_mean"):
        for cad in VALID_CADENCES:
            p = _metrics_path_for(strat, cad)
            if p is None or not p.is_file():
                continue
            j = _load_json(p) or {}
            out.append(
                {
                    "strategy": strat,
                    "cadence": cad,
                    "trained_at": j.get("trained_at"),
                    "test_total_return": (j.get("test_metrics") or {}).get("total_return"),
                    "baseline_test_total_return": (j.get("baseline_test_metrics") or {}).get("total_return"),
                    "test_ic": j.get("test_ic"),
                    "val_ic": j.get("val_ic"),
                    "metrics_path": str(p.relative_to(REPO_ROOT)),
                    "label": STRATEGY_META.get(strat, {}).get("label", strat),
                }
            )
    return out


def _synth_snapshot(strategy: str, cadence: str, metrics: Dict[str, Any]) -> Dict[str, Any]:
    current_top = metrics.get("current_top") or []
    note = (
        "No evaluator snapshot yet — showing the strategy's most recent top picks. "
        f"Run: python backend/sp500_quality_evaluator.py --strategy {strategy} --cadence {cadence} "
        "to add SPY risk-match (inverse-variance) weights."
    )
    return {
        "ts_utc": metrics.get("trained_at"),
        "strategy": strategy,
        "cadence": cadence,
        "synthesized": True,
        "synthesized_note": note,
        "signals_documentation": {
            STRATEGY_META[strategy]["label"]: STRATEGY_META[strategy]["description"],
        }
        if strategy in STRATEGY_META
        else None,
        "top_symbols": [r.get("symbol") for r in current_top if r.get("symbol")],
        "top_detail": current_top,
        "risk_match": None,
    }


def build_strategy_router() -> APIRouter:
    router = APIRouter(prefix="/strategy", tags=["strategy"])

    def _curve_window(curve: Optional[List[Dict[str, Any]]]) -> Optional[tuple[pd.Timestamp, pd.Timestamp]]:
        if not curve:
            return None
        try:
            d0 = str(curve[0].get("date") or "")[:10]
            d1 = str(curve[-1].get("date") or "")[:10]
            if not d0 or not d1:
                return None
            t0 = pd.Timestamp(d0).tz_localize(None)
            t1 = pd.Timestamp(d1).tz_localize(None)
            if t1 < t0:
                t0, t1 = t1, t0
            return t0, t1
        except Exception:
            return None

    def _baseline_returns(prices: Dict[str, Any], symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
        sym = _yahoo_symbol(symbol)
        df = prices.get(sym)
        if df is None or getattr(df, "empty", True):
            raise RuntimeError(f"missing prices for {symbol}")
        c = pd.to_numeric(df["Close"], errors="coerce")
        r = c.pct_change()
        return r.loc[(r.index >= start) & (r.index <= end)].dropna()

    def _build_baseline_payload(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> Dict[str, Any]:
        # Need depth to cover val+test tails when comparing baselines (default 30y history).
        prices = load_prices([symbol], years=30.0, refresh=False)
        sym = symbol.strip().upper()
        rets = _baseline_returns(prices, sym, start, end)
        return {
            "label": sym,
            "color": BASELINE_COLORS.get(sym, "#94a3b8"),
            "metrics": _summary_metrics(rets),
            "curve": _equity_curve_payload(rets),
        }

    @router.get("/list")
    def list_variants() -> Dict[str, Any]:
        return {
            "strategy_meta": STRATEGY_META,
            "snapshots": _list_snapshots(),
            "metrics_files": _list_metrics_files(),
        }

    @router.get("/snapshot/{strategy}/{cadence}")
    def get_snapshot(strategy: str, cadence: str) -> Dict[str, Any]:
        s = (strategy or "").strip().lower()
        c = (cadence or "").strip().lower() or "none"
        snap = _load_json(_snapshot_path_for(s, c))

        bt_metrics: Optional[Dict[str, Any]] = None
        mp = _metrics_path_for(s, c) if c in VALID_CADENCES else None
        if mp is not None:
            bt_metrics = _load_json(mp)

        if snap is None and bt_metrics is not None:
            snap = _synth_snapshot(s, c, bt_metrics)

        if snap is None:
            raise HTTPException(status_code=404, detail=f"no snapshot or model for {s}/{c}")

        return {
            "strategy": s,
            "cadence": None if c == "none" else c,
            "label": STRATEGY_META.get(s, {}).get("label", s),
            "color": STRATEGY_META.get(s, {}).get("color"),
            "snapshot": snap,
            "ml_metrics": bt_metrics,  # kept legacy key — frontend already reads it
        }

    @router.get("/compare/{cadence}")
    def compare(cadence: str) -> Dict[str, Any]:
        """Return all strategies' val/test backtest metrics + curves for one cadence,
        so the frontend can overlay them on a single chart and table."""
        c = (cadence or "").strip().lower()
        if c not in VALID_CADENCES:
            raise HTTPException(status_code=400, detail=f"cadence must be one of {VALID_CADENCES}")

        variants: List[Dict[str, Any]] = []
        baseline_train: Optional[List[Dict[str, Any]]] = None
        baseline_val: Optional[List[Dict[str, Any]]] = None
        baseline_test: Optional[List[Dict[str, Any]]] = None
        baseline_train_metrics: Optional[Dict[str, Any]] = None
        baseline_val_metrics: Optional[Dict[str, Any]] = None
        baseline_test_metrics: Optional[Dict[str, Any]] = None
        for strat in ("ml", "ml_pred_weighted", "rsi_mean"):
            mp = _metrics_path_for(strat, c)
            if mp is None or not mp.is_file():
                continue
            j = _load_json(mp) or {}
            variants.append(
                {
                    "strategy": strat,
                    "label": STRATEGY_META[strat]["label"],
                    "color": STRATEGY_META[strat]["color"],
                    "trained_at": j.get("trained_at"),
                    "train_metrics": j.get("train_metrics"),
                    "val_metrics": j.get("val_metrics"),
                    "test_metrics": j.get("test_metrics"),
                    "train_curve": _curve_or_empty(j.get("train_curve")),
                    "val_curve": _curve_or_empty(j.get("val_curve")),
                    "test_curve": _curve_or_empty(j.get("test_curve")),
                    "current_top": j.get("current_top"),
                    "n_train": j.get("n_train"),
                    "n_val": j.get("n_val"),
                    "n_test": j.get("n_test"),
                }
            )
            # SPY baseline curves: fill from the first metrics file that contains each segment (mixed-age JSON).
            if baseline_train is None and j.get("baseline_train_curve"):
                baseline_train = _curve_or_empty(j.get("baseline_train_curve"))
                baseline_train_metrics = j.get("baseline_train_metrics")
            if baseline_val is None and j.get("baseline_val_curve"):
                baseline_val = _curve_or_empty(j.get("baseline_val_curve"))
                baseline_val_metrics = j.get("baseline_val_metrics")
            if baseline_test is None and j.get("baseline_test_curve"):
                baseline_test = _curve_or_empty(j.get("baseline_test_curve"))
                baseline_test_metrics = j.get("baseline_test_metrics")

        # Extra baselines: buy-and-hold on the same windows as SPY baseline.
        bt_tr = _curve_or_empty(baseline_train)
        bt_va = _curve_or_empty(baseline_val)
        bt_te = _curve_or_empty(baseline_test)
        baselines: List[Dict[str, Any]] = [
            {
                "label": "SPY",
                "color": BASELINE_COLORS.get("SPY", "#a0aec0"),
                "train_curve": bt_tr,
                "val_curve": bt_va,
                "test_curve": bt_te,
                "train_metrics": baseline_train_metrics,
                "val_metrics": baseline_val_metrics,
                "test_metrics": baseline_test_metrics,
            }
        ]
        train_win = _curve_window(bt_tr)
        val_win = _curve_window(bt_va)
        test_win = _curve_window(bt_te)
        # Train-window curves may be absent on older metrics JSONs; still build val/test mega-cap baselines.
        if val_win and test_win:
            # Ensure deterministic order and skip duplicates / SPY.
            for sym in [s for s in EXTRA_BASELINE_SYMBOLS if str(s).strip().upper() != "SPY"]:
                try:
                    tr = (
                        _build_baseline_payload(sym, train_win[0], train_win[1])
                        if train_win
                        else {"curve": [], "metrics": {}}
                    )
                    v = _build_baseline_payload(sym, val_win[0], val_win[1])
                    t = _build_baseline_payload(sym, test_win[0], test_win[1])
                    baselines.append(
                        {
                            "label": sym.strip().upper(),
                            "color": BASELINE_COLORS.get(sym.strip().upper(), "#94a3b8"),
                            "train_curve": tr["curve"],
                            "val_curve": v["curve"],
                            "test_curve": t["curve"],
                            "train_metrics": tr["metrics"],
                            "val_metrics": v["metrics"],
                            "test_metrics": t["metrics"],
                        }
                    )
                except Exception:
                    # Baselines are best-effort; don't fail the endpoint if a ticker has missing prices.
                    continue

        return {
            "cadence": c,
            "variants": variants,
            "baselines": baselines,
            "baseline": {
                "label": "SPY",
                "color": BASELINE_COLORS.get("SPY", "#a0aec0"),
                "train_curve": bt_tr,
                "val_curve": bt_va,
                "test_curve": bt_te,
                "train_metrics": baseline_train_metrics,
                "val_metrics": baseline_val_metrics,
                "test_metrics": baseline_test_metrics,
            },
        }

    return router
