#!/usr/bin/env python3
"""
Two-stage backfill to precompute daily value metrics efficiently:

1) Backfill fundamentals snapshots (quarter/annual) into vm_fundamental_points
2) Compute daily metrics for each trading day (period='daily') into vm_metric_points

Daily metrics are computed by:
- forward-filling the latest available balance sheet items (equity, debt, cash, current assets/liabilities)
- using TTM (sum of last 4 quarters) for income/cashflow items when quarterly data exists
- recomputing price-based ratios using daily close
- dividend_yield uses trailing-365d dividend-per-share / close (per-share series from yfinance)

PEG is left null (not estimated here).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
import yfinance as yf

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "backend"))

from dotenv import load_dotenv

from value_metrics_provider_yfinance_fundamentals import fetch_yfinance_fundamentals_history
from value_metrics_store import connect, init_db, query_fundamental_points, upsert_fundamental_points, upsert_metric_points


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_env() -> None:
    root_env = _REPO_ROOT / ".env"
    backend_env = _REPO_ROOT / "backend" / ".env"
    if root_env.exists():
        load_dotenv(dotenv_path=root_env, override=False)
    if backend_env.exists():
        load_dotenv(dotenv_path=backend_env, override=False)


def _load_symbols_from_json(path: Path) -> List[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: List[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("ticker"):
                out.append(str(item["ticker"]).strip().upper())
            elif isinstance(item, str):
                out.append(item.strip().upper())
    elif isinstance(raw, dict):
        out = [str(k).strip().upper() for k in raw.keys() if str(k).strip()]
    return sorted(set(s for s in out if s))


def _parse_symbols_arg(s: str) -> List[str]:
    return sorted({x.strip().upper() for x in (s or "").split(",") if x.strip()})


def _coerce_close_series(px: pd.DataFrame, sym: str) -> pd.Series:
    if px is None or px.empty:
        return pd.Series(dtype=float)
    if isinstance(px.columns, pd.MultiIndex):
        lvl0 = px.columns.get_level_values(0)
        if "Close" in lvl0 and sym in px.columns.get_level_values(1):
            ser = px.xs(sym, axis=1, level=1)["Close"]
        elif "Close" in lvl0:
            ser = px["Close"].iloc[:, 0]
        else:
            ser = px.iloc[:, 0]
    elif "Close" in px.columns:
        ser = px["Close"]
    else:
        ser = px.iloc[:, 0]
    if isinstance(ser, pd.DataFrame):
        ser = ser.iloc[:, 0]
    closes = pd.to_numeric(ser, errors="coerce").dropna()
    if closes.index.tz is not None:
        closes = closes.copy()
        closes.index = closes.index.tz_convert("UTC").tz_localize(None)
    closes.index = pd.to_datetime(closes.index).tz_localize(None).normalize()
    closes = closes[~closes.index.duplicated(keep="last")].sort_index()
    return closes


def _coerce_div_series(divs: Optional[pd.Series]) -> pd.Series:
    if divs is None or len(divs) == 0:
        return pd.Series(dtype=float)
    s = divs.astype(float)
    if s.index.tz is not None:
        s = s.copy()
        s.index = s.index.tz_convert("UTC").tz_localize(None)
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


def _ensure_fundamentals(
    con,
    *,
    symbol: str,
    start_s: str,
    end_s: str,
    provider: str = "yfinance",
) -> None:
    # buffer earlier fundamentals for TTM construction
    buf_start = (pd.Timestamp(start_s) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    for per in ("quarter", "annual"):
        existing = query_fundamental_points(
            con,
            symbols=[symbol],
            start_date=buf_start,
            end_date=end_s,
            provider=provider,
            period=per,
        )
        if existing:
            continue
        pts = fetch_yfinance_fundamentals_history(symbol=symbol, period=per, start_date=buf_start, end_date=end_s)
        upsert_fundamental_points(con, provider=provider, period=per, points=pts)


def _compute_daily_metrics_for_symbol(
    con,
    *,
    symbol: str,
    start_s: str,
    end_s: str,
    provider: str = "yfinance",
) -> int:
    sym = str(symbol).strip().upper()
    start = pd.Timestamp(start_s).tz_localize(None).normalize()
    end = pd.Timestamp(end_s).tz_localize(None).normalize()

    # fundamentals: prefer quarterly for TTM; fallback to annual if no quarter data.
    buf_start = (start - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    q = query_fundamental_points(con, symbols=[sym], start_date=buf_start, end_date=end_s, provider=provider, period="quarter")
    a = query_fundamental_points(con, symbols=[sym], start_date=buf_start, end_date=end_s, provider=provider, period="annual")

    f = pd.DataFrame(q if q else a)
    if f.empty:
        return 0

    f["asof_date"] = pd.to_datetime(f["asof_date"]).dt.tz_localize(None).dt.normalize()
    f = f.sort_values("asof_date").set_index("asof_date")

    # Build TTM for flow items if quarterly exists and has enough rows.
    is_quarter = bool(q)
    if is_quarter:
        for col in ("revenue", "operating_income", "net_income", "free_cash_flow", "ebitda", "eps"):
            if col in f.columns:
                f[f"{col}_ttm"] = pd.to_numeric(f[col], errors="coerce").rolling(4, min_periods=1).sum()
    else:
        for col in ("revenue", "operating_income", "net_income", "free_cash_flow", "ebitda", "eps"):
            if col in f.columns:
                f[f"{col}_ttm"] = pd.to_numeric(f[col], errors="coerce")

    # prices
    px = yf.download(sym, start=(start - pd.Timedelta(days=10)).strftime("%Y-%m-%d"), end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"), interval="1d", auto_adjust=True, progress=False)
    closes = _coerce_close_series(px, sym)
    closes = closes.loc[(closes.index >= start) & (closes.index <= end)]
    if closes.empty:
        return 0

    # dividends for trailing 365d dividend yield (per share)
    t = yf.Ticker(sym)
    div_series = _coerce_div_series(getattr(t, "dividends", None))
    if not div_series.empty:
        div_daily = div_series.reindex(closes.index, fill_value=0.0)
        div_1y = div_daily.rolling(365, min_periods=1).sum()
    else:
        div_1y = pd.Series(0.0, index=closes.index)

    # align fundamentals to trading days (ffill latest report)
    f_daily = f.reindex(closes.index, method="ffill")

    eps_ttm = pd.to_numeric(f_daily.get("eps_ttm"), errors="coerce")
    revenue_ttm = pd.to_numeric(f_daily.get("revenue_ttm"), errors="coerce")
    op_inc_ttm = pd.to_numeric(f_daily.get("operating_income_ttm"), errors="coerce")
    net_income_ttm = pd.to_numeric(f_daily.get("net_income_ttm"), errors="coerce")
    fcf_ttm = pd.to_numeric(f_daily.get("free_cash_flow_ttm"), errors="coerce")
    ebitda_ttm = pd.to_numeric(f_daily.get("ebitda_ttm"), errors="coerce")

    equity = pd.to_numeric(f_daily.get("equity"), errors="coerce")
    debt = pd.to_numeric(f_daily.get("debt"), errors="coerce")
    cash = pd.to_numeric(f_daily.get("cash"), errors="coerce")
    ca = pd.to_numeric(f_daily.get("current_assets"), errors="coerce")
    cl = pd.to_numeric(f_daily.get("current_liabilities"), errors="coerce")
    shares = pd.to_numeric(f_daily.get("implied_shares"), errors="coerce")

    close = closes.astype(float)
    mcap = close * shares

    def _safe_div(a, b) -> pd.Series:
        b2 = b.replace({0.0: pd.NA})
        return a / b2

    pe = _safe_div(close, eps_ttm)
    pb = _safe_div(close, _safe_div(equity, shares))
    dividend_yield = _safe_div(div_1y, close)
    free_cash_flow_yield = _safe_div(fcf_ttm, mcap)
    debt_to_equity = _safe_div(debt, equity)
    roe = _safe_div(net_income_ttm, equity)
    current_ratio = _safe_div(ca, cl)
    operating_margin = _safe_div(op_inc_ttm, revenue_ttm)
    ev_to_ebitda = _safe_div((mcap + debt - cash), ebitda_ttm)

    points: List[Dict[str, Any]] = []
    fetched = _utcnow_iso()
    for dt in close.index:
        d = dt.strftime("%Y-%m-%d")
        raw = {
            "source": "yfinance_daily_compute",
            "fundamentals_period": "quarter" if is_quarter else "annual",
            "fundamentals_asof": str(f_daily.loc[dt].name.strftime("%Y-%m-%d")) if dt in f_daily.index else None,
        }
        points.append(
            {
                "symbol": sym,
                "asof_date": d,
                "fetched_ts_utc": fetched,
                "pe": None if pd.isna(pe.loc[dt]) else float(pe.loc[dt]),
                "pb": None if pd.isna(pb.loc[dt]) else float(pb.loc[dt]),
                "peg": None,
                "dividend_yield": None if pd.isna(dividend_yield.loc[dt]) else float(dividend_yield.loc[dt]),
                "free_cash_flow_yield": None if pd.isna(free_cash_flow_yield.loc[dt]) else float(free_cash_flow_yield.loc[dt]),
                "debt_to_equity": None if pd.isna(debt_to_equity.loc[dt]) else float(debt_to_equity.loc[dt]),
                "roe": None if pd.isna(roe.loc[dt]) else float(roe.loc[dt]),
                "current_ratio": None if pd.isna(current_ratio.loc[dt]) else float(current_ratio.loc[dt]),
                "operating_margin": None if pd.isna(operating_margin.loc[dt]) else float(operating_margin.loc[dt]),
                "ev_to_ebitda": None if pd.isna(ev_to_ebitda.loc[dt]) else float(ev_to_ebitda.loc[dt]),
                "raw": raw,
            }
        )

    return upsert_metric_points(con, provider=provider, period="daily", points=points)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill fundamentals then compute daily value metrics.")
    ap.add_argument("--years", type=float, default=2.0, help="Lookback window in years (default: 2)")
    ap.add_argument("--symbols-file", type=str, default="", help="JSON universe file (default: SYMBOL_UNIVERSE_PATH or telegram_agent/top1000_investments.json)")
    ap.add_argument("--symbols", type=str, default="", help="Comma-separated tickers (overrides --symbols-file)")
    ap.add_argument("--db", type=str, default="", help="SQLite path (default: VALUE_METRICS_DB_PATH or backend/data/value_metrics.sqlite)")
    ap.add_argument("--sleep", type=float, default=0.25, help="Sleep seconds after each symbol (default: 0.25)")
    ap.add_argument("--max-symbols", type=int, default=0, help="If >0, only first N symbols (debug)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    _load_env()

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(args.years * 365.25))
    start_s = start.isoformat()
    end_s = end.isoformat()

    if args.symbols.strip():
        symbols = _parse_symbols_arg(args.symbols)
    else:
        if args.symbols_file.strip():
            sym_path = Path(args.symbols_file).expanduser()
        else:
            env_p = (os.getenv("SYMBOL_UNIVERSE_PATH") or "").strip()
            sym_path = Path(env_p).expanduser() if env_p else _REPO_ROOT / "telegram_agent" / "top1000_investments.json"
        if not sym_path.is_file():
            print(f"ERROR: symbols file not found: {sym_path}", file=sys.stderr)
            return 1
        symbols = _load_symbols_from_json(sym_path)
        print(f"Loaded {len(symbols)} symbols from {sym_path}")

    if int(args.max_symbols) > 0:
        symbols = symbols[: int(args.max_symbols)]

    db_path = Path(args.db).expanduser() if args.db.strip() else Path(
        os.getenv("VALUE_METRICS_DB_PATH", str(_REPO_ROOT / "backend" / "data" / "value_metrics.sqlite"))
    ).expanduser()

    con = connect(db_path)
    init_db(con)

    print(f"DB: {db_path}")
    print(f"Window: {start_s} .. {end_s} (UTC dates, inclusive)")
    print(f"Symbols: {len(symbols)}")

    total_daily_upserts = 0
    ok = 0
    failed = 0

    for i, sym in enumerate(symbols):
        try:
            _ensure_fundamentals(con, symbol=sym, start_s=start_s, end_s=end_s, provider="yfinance")
            n = _compute_daily_metrics_for_symbol(con, symbol=sym, start_s=start_s, end_s=end_s, provider="yfinance")
            total_daily_upserts += int(n)
            ok += 1
        except Exception as e:
            failed += 1
            print(f"  [{sym}] ERROR: {e}")
        time.sleep(max(0.0, float(args.sleep)))
        if (i + 1) % 25 == 0:
            print(f"  progress {i + 1}/{len(symbols)} symbols, daily_row_upserts≈{total_daily_upserts}")

    con.close()
    print(f"Done. symbols_ok={ok} symbols_with_errors={failed} daily_row_upserts={total_daily_upserts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

