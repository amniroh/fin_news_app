#!/usr/bin/env python3
"""
S&P 500 quality factor screener: rank ~500 names by profitability + stability + value,
select top N, and compare portfolio volatility to SPY for risk-matching guidance.

Data sources (public, via yfinance):
  - Ratios/margins from ``Ticker.info`` (Yahoo-derived from filings; same universe most retail tools use).
  - Optional earnings stability: coefficient of variation of recent quarterly net income from
    ``quarterly_income_stmt`` when available.
  - Realized return volatility from adjusted daily closes.

Cadence: use ``--cache-hours`` or ``--cadence`` so full 500-ticker pulls are not repeated more
often than daily / weekly / monthly intent (cached snapshot JSON).

Environment:
  SP500_SYMBOLS — optional comma-separated override if Wikipedia fetch fails.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "backend"))

from value_metrics_provider_yfinance import fetch_value_metrics


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.floating, np.integer)):
        return float(o) if isinstance(o, np.floating) else int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


def normalize_symbol(sym: str) -> str:
    s = (sym or "").strip().upper().replace(" ", "")
    if "." in s and len(s) <= 8:
        s = s.replace(".", "-")
    return s


def load_sp500_symbols(*, cache_path: Optional[Path] = None, refresh: bool = False) -> List[str]:
    """Return sorted unique S&P 500 tickers (Yahoo-style: BRK-B not BRK.B)."""
    env = (os.getenv("SP500_SYMBOLS") or "").strip()
    if env:
        return sorted({normalize_symbol(x) for x in env.split(",") if x.strip()})

    path = cache_path or (_REPO_ROOT / "backend" / "data" / "sp500_symbols.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.is_file() and not refresh:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            syms = data.get("symbols") if isinstance(data, dict) else data
            if isinstance(syms, list) and len(syms) >= 400:
                return sorted({normalize_symbol(str(x)) for x in syms if str(x).strip()})
        except Exception:
            pass

    # Wikipedia S&P 500 table (Symbol column)
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    try:
        tables = pd.read_html(url)
    except ImportError as e:
        raise RuntimeError(
            "pandas.read_html needs an HTML parser (e.g. pip install lxml). "
            "Or set SP500_SYMBOLS to a comma-separated list."
        ) from e
    sym_col = None
    table = None
    for t in tables:
        cols = [str(c).lower() for c in t.columns]
        if "symbol" in cols:
            sym_col = t.columns[list(cols).index("symbol")]
            table = t
            break
    if sym_col is None or table is None:
        raise RuntimeError("Could not parse Wikipedia S&P 500 table; set SP500_SYMBOLS in .env")
    raw = table[sym_col].astype(str).tolist()
    out = sorted({normalize_symbol(x) for x in raw if x and x != "nan"})
    payload = {"asof_utc": _utcnow_iso(), "source": "wikipedia", "symbols": out}
    path.write_text(json.dumps(payload, indent=0), encoding="utf-8")
    return out


def _quarterly_net_income_cv(symbol: str, max_q: int = 8) -> Optional[float]:
    """Lower CV = more stable earnings (relative to scale). Returns None if unavailable."""
    sym = normalize_symbol(symbol)
    t = yf.Ticker(sym)
    stmt = getattr(t, "quarterly_income_stmt", None)
    if stmt is None or (hasattr(stmt, "empty") and stmt.empty):
        try:
            stmt = t.get_income_stmt(freq="quarterly")  # type: ignore[attr-defined]
        except Exception:
            stmt = None
    if stmt is None or hasattr(stmt, "empty") and stmt.empty:
        return None
    row = None
    for key in ("Net Income", "NetIncomeCommonStockholders", "Net Income Common Stockholders"):
        if key in stmt.index:
            row = stmt.loc[key]
            break
    if row is None:
        return None
    vals = pd.to_numeric(row, errors="coerce").dropna().iloc[:max_q].values.astype(float)
    if vals.size < 4:
        return None
    m = float(np.mean(np.abs(vals)))
    if m < 1e-9:
        return None
    return float(np.std(vals, ddof=1) / m)


def _price_vol_beta(symbol: str, benchmark: str, lookback_days: int) -> Tuple[Optional[float], Optional[float]]:
    """Annualized vol of daily returns, and OLS beta vs benchmark."""
    sym = normalize_symbol(symbol)
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(lookback_days) + 40)
    px = yf.download(
        [sym, benchmark],
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if px is None or px.empty:
        return None, None

    def _close(df: pd.DataFrame, name: str) -> pd.Series:
        if isinstance(df.columns, pd.MultiIndex):
            if name in df.columns.get_level_values(1):
                return pd.to_numeric(df.xs(name, axis=1, level=1)["Close"], errors="coerce")
            return pd.to_numeric(df["Close"].iloc[:, 0], errors="coerce")
        return pd.to_numeric(df["Close"], errors="coerce")

    try:
        c_s = _close(px, sym)
        c_b = _close(px, benchmark)
    except Exception:
        return None, None
    r_s = c_s.pct_change().dropna()
    r_b = c_b.pct_change().dropna()
    joined = pd.concat([r_s, r_b], axis=1, join="inner").dropna()
    joined.columns = ["s", "b"]
    if len(joined) < 60:
        return None, None
    vol = float(joined["s"].std(ddof=1) * np.sqrt(252))
    cov = float(np.cov(joined["s"], joined["b"], ddof=1)[0, 1])
    var_b = float(np.var(joined["b"], ddof=1))
    beta = (cov / var_b) if var_b > 1e-18 else None
    return vol, beta


@dataclass
class SymbolScore:
    symbol: str
    roe: Optional[float]
    operating_margin: Optional[float]
    fcf_yield: Optional[float]
    debt_to_equity: Optional[float]
    current_ratio: Optional[float]
    pe: Optional[float]
    pb: Optional[float]
    ev_to_ebitda: Optional[float]
    earnings_cv: Optional[float]
    vol_252d: Optional[float]
    beta: Optional[float]
    profitability_rank: float
    stability_rank: float
    value_rank: float
    composite_rank: float


def _percentile_rank_higher_better(values: np.ndarray) -> np.ndarray:
    """NaN-preserving 0..100 rank; higher raw value => higher score."""
    x = np.asarray(values, dtype=float)
    out = np.full_like(x, np.nan, dtype=float)
    mask = np.isfinite(x)
    if mask.sum() == 0:
        return out
    ranked = pd.Series(x[mask]).rank(pct=True, method="average").values * 100.0
    out[mask] = ranked
    return out


def _percentile_rank_lower_better(values: np.ndarray) -> np.ndarray:
    return _percentile_rank_higher_better(-values)


def score_universe(
    symbols: Sequence[str],
    *,
    benchmark: str = "SPY",
    lookback_days: int = 252,
    sleep_s: float = 0.12,
) -> List[SymbolScore]:
    rows: List[SymbolScore] = []
    n = len(symbols)
    for i, sym in enumerate(symbols):
        sym = normalize_symbol(sym)
        try:
            vm = fetch_value_metrics(sym)
        except Exception:
            rows.append(
                SymbolScore(
                    symbol=sym,
                    roe=None,
                    operating_margin=None,
                    fcf_yield=None,
                    debt_to_equity=None,
                    current_ratio=None,
                    pe=None,
                    pb=None,
                    ev_to_ebitda=None,
                    earnings_cv=_quarterly_net_income_cv(sym),
                    vol_252d=None,
                    beta=None,
                    profitability_rank=0.0,
                    stability_rank=0.0,
                    value_rank=0.0,
                    composite_rank=0.0,
                )
            )
            time.sleep(max(0.0, sleep_s))
            continue
        ni_cv = _quarterly_net_income_cv(sym)
        vol, beta = _price_vol_beta(sym, benchmark, lookback_days)

        roe = vm.roe
        opm = vm.operating_margin
        fcf_y = vm.free_cash_flow_yield
        dte = vm.debt_to_equity
        cur = vm.current_ratio
        pe = vm.pe
        pb = vm.pb
        ev_e = vm.ev_to_ebitda

        rows.append(
            SymbolScore(
                symbol=sym,
                roe=roe,
                operating_margin=opm,
                fcf_yield=fcf_y,
                debt_to_equity=dte,
                current_ratio=cur,
                pe=pe,
                pb=pb,
                ev_to_ebitda=ev_e,
                earnings_cv=ni_cv,
                vol_252d=vol,
                beta=beta,
                profitability_rank=0.0,
                stability_rank=0.0,
                value_rank=0.0,
                composite_rank=0.0,
            )
        )
        time.sleep(max(0.0, sleep_s))
        if (i + 1) % 50 == 0:
            print(f"  fetched {i + 1}/{n} …", file=sys.stderr)

    # Cross-sectional ranks
    def arr(attr: str) -> np.ndarray:
        return np.array([getattr(r, attr) for r in rows], dtype=float)

    prof = (
        _percentile_rank_higher_better(arr("roe"))
        + _percentile_rank_higher_better(arr("operating_margin"))
        + _percentile_rank_higher_better(arr("fcf_yield"))
    ) / 3.0

    # Stability: low leverage, adequate liquidity, smooth earnings, lower realized vol
    stab = (
        _percentile_rank_lower_better(arr("debt_to_equity"))
        + _percentile_rank_higher_better(arr("current_ratio"))
        + _percentile_rank_lower_better(arr("earnings_cv"))
        + _percentile_rank_lower_better(arr("vol_252d"))
    ) / 4.0

    # Value (for value-tilted quality): cheaper on multiples
    val = (
        _percentile_rank_lower_better(arr("pe"))
        + _percentile_rank_lower_better(arr("pb"))
        + _percentile_rank_lower_better(arr("ev_to_ebitda"))
    ) / 3.0

    for i, r in enumerate(rows):
        r.profitability_rank = float(prof[i]) if np.isfinite(prof[i]) else 0.0
        r.stability_rank = float(stab[i]) if np.isfinite(stab[i]) else 0.0
        r.value_rank = float(val[i]) if np.isfinite(val[i]) else 0.0
        # Emphasize profitability + stability; value is explicit tilt
        r.composite_rank = 0.35 * r.profitability_rank + 0.35 * r.stability_rank + 0.30 * r.value_rank

    return rows


def _returns_matrix(
    symbols: Sequence[str],
    *,
    benchmark: str,
    lookback_days: int,
) -> Tuple[pd.DataFrame, pd.Series]:
    syms = [normalize_symbol(s) for s in symbols]
    tickers = syms + [benchmark.strip().upper()]
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(lookback_days) + 40)
    px = yf.download(
        tickers,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    if px is None or px.empty:
        raise RuntimeError("no price data for risk comparison")

    def extract(sym: str) -> pd.Series:
        if isinstance(px.columns, pd.MultiIndex):
            if sym in px.columns.get_level_values(1):
                return pd.to_numeric(px.xs(sym, axis=1, level=1)["Close"], errors="coerce")
        c = px["Close"]
        return pd.to_numeric(c[sym] if isinstance(c, pd.DataFrame) and sym in c.columns else c, errors="coerce")

    rets = pd.DataFrame({s: extract(s).pct_change() for s in syms}).dropna(how="all")
    bench = extract(benchmark.strip().upper()).pct_change()
    bench = bench.reindex(rets.index).dropna()
    rets = rets.loc[bench.index].dropna(axis=0, how="all")
    # align
    idx = rets.index.intersection(bench.index)
    rets = rets.loc[idx].dropna(how="any")
    bench = bench.loc[rets.index]
    return rets, bench


def portfolio_vol_equal_weight(returns: pd.DataFrame) -> float:
    ew = returns.mean(axis=1)
    return float(ew.std(ddof=1) * np.sqrt(252))


def portfolio_vol_inverse_variance(returns: pd.DataFrame) -> Tuple[np.ndarray, float]:
    """Weights ∝ 1/annualized variance of each name; vol of weighted portfolio."""
    var_d = returns.var(ddof=1)
    inv = 1.0 / var_d.replace(0.0, np.nan)
    inv = inv.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    s = inv.sum()
    if s <= 0:
        w = np.ones(len(returns.columns)) / len(returns.columns)
    else:
        w = (inv / s).values
    port = returns.values @ w
    vol = float(np.std(port, ddof=1) * np.sqrt(252))
    return w, vol


def risk_match_report(
    selected: Sequence[str],
    *,
    benchmark: str = "SPY",
    lookback_days: int = 252,
) -> Dict[str, Any]:
    rets, bench_r = _returns_matrix(selected, benchmark=benchmark, lookback_days=lookback_days)
    vol_idx = float(bench_r.std(ddof=1) * np.sqrt(252))
    vol_ew = portfolio_vol_equal_weight(rets)
    w_iv, vol_iv = portfolio_vol_inverse_variance(rets)

    # Scale equity sleeve to match index vol (cash-like residual)
    scale_ew = (vol_idx / vol_ew) if vol_ew > 1e-12 else None
    scale_iv = (vol_idx / vol_iv) if vol_iv > 1e-12 else None

    return {
        "benchmark": benchmark.strip().upper(),
        "lookback_trading_days": int(len(rets)),
        "vol_benchmark_annual": vol_idx,
        "vol_portfolio_equal_weight_annual": vol_ew,
        "vol_portfolio_inv_variance_weight_annual": vol_iv,
        "suggested_equity_fraction_equal_weight_vs_cash": scale_ew,
        "suggested_equity_fraction_inv_var_vs_cash": scale_iv,
        "inverse_variance_weights": {c: float(w) for c, w in zip(rets.columns, w_iv)},
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="S&P 500 quality screen + top 50 + SPY vol match")
    ap.add_argument("--top", type=int, default=50, help="How many names to select (default 50)")
    ap.add_argument("--benchmark", type=str, default="SPY", help="Index ETF for beta/vol (default SPY)")
    ap.add_argument("--lookback-days", type=int, default=252, help="Risk / vol window (~1y)")
    ap.add_argument("--sleep", type=float, default=0.12, help="Delay between Yahoo calls when screening")
    ap.add_argument("--refresh-sp500", action="store_true", help="Re-fetch S&P 500 list from Wikipedia")
    ap.add_argument("--cache-hours", type=float, default=-1.0, help="Skip full run if snapshot younger than this (use with cadence)")
    ap.add_argument(
        "--cadence",
        type=str,
        default="",
        choices=["", "daily", "weekly", "monthly"],
        help="Shorthand for cache: daily≈20h, weekly=168h, monthly=720h (only if --cache-hours unset)",
    )
    ap.add_argument("--output", type=str, default="", help="Write JSON results to this path")
    args = ap.parse_args(list(argv) if argv is not None else None)

    cache_h = float(args.cache_hours)
    if cache_h < 0 and args.cadence:
        cache_h = {"daily": 20.0, "weekly": 168.0, "monthly": 720.0}[args.cadence]
    elif cache_h < 0:
        cache_h = 0.0

    snap_path = _REPO_ROOT / "backend" / "data" / "quality_evaluator_last_run.json"

    if cache_h > 0 and snap_path.is_file():
        try:
            prev = json.loads(snap_path.read_text(encoding="utf-8"))
            ts = prev.get("ts_utc") or ""
            then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - then.astimezone(timezone.utc)).total_seconds() / 3600.0
            if age_h < float(cache_h):
                print(json.dumps(prev, indent=2, default=_json_default))
                print(f"\n(cached run {age_h:.2f}h old; use --cache-hours 0 to force)", file=sys.stderr)
                return 0
        except Exception:
            pass

    symbols = load_sp500_symbols(refresh=bool(args.refresh_sp500))
    print(f"Loaded {len(symbols)} S&P 500 symbols. Scoring (this may take several minutes)…", file=sys.stderr)

    scored = score_universe(symbols, benchmark=args.benchmark.strip().upper(), lookback_days=args.lookback_days, sleep_s=args.sleep)
    scored.sort(key=lambda r: r.composite_rank, reverse=True)

    top = scored[: int(args.top)]

    risk = risk_match_report([r.symbol for r in top], benchmark=args.benchmark, lookback_days=args.lookback_days)

    out: Dict[str, Any] = {
        "ts_utc": _utcnow_iso(),
        "cadence_cache_hours": cache_h,
        "universe_size": len(symbols),
        "signals_documentation": {
            "profitability": "Average percentile rank of ROE, operating margin, FCF yield (higher better).",
            "stability": "Average rank of low debt/equity, higher current ratio, low quarterly NI CV, low 252d vol.",
            "value": "Average rank of low P/E, P/B, EV/EBITDA (value tilt among large caps).",
            "composite": "0.35*profitability + 0.35*stability + 0.30*value",
        },
        "top_symbols": [r.symbol for r in top],
        "top_detail": [asdict(r) for r in top],
        "risk_match": risk,
    }

    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(json.dumps(out, indent=2, default=_json_default), encoding="utf-8")

    txt = json.dumps(out, indent=2, default=_json_default)
    print(txt)

    if args.output.strip():
        Path(args.output).expanduser().write_text(txt, encoding="utf-8")

    print(
        "\n---\nInterpretation: If vol_portfolio_equal_weight_annual > vol_benchmark_annual, "
        "hold only suggested_equity_fraction_equal_weight_vs_cash of the stock sleeve in stocks "
        "and the rest in cash/T-bills to approximate index volatility (simple scaling heuristic).\n"
        "Inverse-variance weights reduce concentration risk and often lower portfolio vol vs equal weight.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
