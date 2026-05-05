#!/usr/bin/env python3
"""
Coverage report for daily value metrics in vm_metric_points.

Reports, per symbol:
- number of daily rows in window
- number of rows with any missing metric (null)

This is meant to be used after running backend/value_metrics_daily_backfill.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

from value_metrics_store import connect, init_db

_REPO_ROOT = Path(__file__).resolve().parents[1]


METRIC_COLS = [
    "pe",
    "pb",
    "peg",
    "dividend_yield",
    "free_cash_flow_yield",
    "debt_to_equity",
    "roe",
    "current_ratio",
    "operating_margin",
    "ev_to_ebitda",
]


def _load_sp500_symbols_from_env() -> List[str]:
    raw = (os.getenv("SP500_SYMBOLS") or "").strip()
    if not raw:
        raise RuntimeError("SP500_SYMBOLS env var is not set")
    out = []
    for x in raw.split(","):
        s = str(x).strip().upper()
        if not s:
            continue
        out.append(s.replace(".", "-"))
    return sorted(set(out))


def _parse_symbols_arg(s: str) -> List[str]:
    return sorted({x.strip().upper() for x in (s or "").split(",") if x.strip()})


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Coverage report for vm_metric_points (daily)")
    ap.add_argument("--years", type=float, default=20.0, help="Lookback window in years (default: 20)")
    ap.add_argument("--spy-symbols", action="store_true", help="Use SP500_SYMBOLS from env")
    ap.add_argument("--symbols", type=str, default="", help="Comma-separated tickers (optional)")
    ap.add_argument("--db", type=str, default="", help="SQLite path (default: VALUE_METRICS_DB_PATH or backend/data/value_metrics.sqlite)")
    ap.add_argument("--provider", type=str, default="yfinance", help="Provider name in vm_metric_points (default yfinance)")
    ap.add_argument("--out", type=str, default="", help="Optional path to write JSON report")
    args = ap.parse_args(list(argv) if argv is not None else None)

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(float(args.years) * 365.25))
    start_s = start.isoformat()
    end_s = end.isoformat()

    if args.spy_symbols:
        symbols = _load_sp500_symbols_from_env()
    elif args.symbols.strip():
        symbols = _parse_symbols_arg(args.symbols)
    else:
        print("ERROR: pass --spy-symbols or --symbols", file=sys.stderr)
        return 2

    db_path = Path(args.db).expanduser() if args.db.strip() else Path(
        os.getenv("VALUE_METRICS_DB_PATH", str(_REPO_ROOT / "backend" / "data" / "value_metrics.sqlite"))
    ).expanduser()
    prov = str(args.provider).strip().lower()

    con = connect(db_path)
    init_db(con)
    try:
        out_rows: List[Dict[str, Any]] = []
        for sym in symbols:
            row = con.execute(
                f"""
                SELECT
                  COUNT(*) AS n_rows,
                  SUM(CASE WHEN ({' OR '.join([f'{c} IS NULL' for c in METRIC_COLS])}) THEN 1 ELSE 0 END) AS n_partial
                FROM vm_metric_points
                WHERE symbol = ?
                  AND provider = ?
                  AND period = 'daily'
                  AND asof_date >= ?
                  AND asof_date <= ?
                """,
                (sym, prov, start_s, end_s),
            ).fetchone()
            n_rows = int(row["n_rows"] or 0)
            n_partial = int(row["n_partial"] or 0)
            out_rows.append(
                {
                    "symbol": sym,
                    "rows": n_rows,
                    "partial_rows": n_partial,
                    "partial_frac": (n_partial / n_rows) if n_rows else None,
                }
            )
        report = {
            "db": str(db_path),
            "provider": prov,
            "period": "daily",
            "window": {"start": start_s, "end": end_s},
            "symbols": len(symbols),
            "rows_total": sum(int(r["rows"]) for r in out_rows),
            "partial_total": sum(int(r["partial_rows"]) for r in out_rows),
            "by_symbol": out_rows,
        }
    finally:
        con.close()

    txt = json.dumps(report, indent=2, sort_keys=False)
    if args.out.strip():
        Path(args.out).expanduser().write_text(txt, encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

