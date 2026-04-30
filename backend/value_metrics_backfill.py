#!/usr/bin/env python3
"""
Backfill historical value metrics (10 canonical fields) into SQLite (vm_metric_points).

Providers:
  - fmp: Financial Modeling Prep ratio history (requires FMP_API_KEY; plan may limit access).
  - yfinance: derived from Yahoo Finance statements + daily closes (no FMP key).
  - auto: try fmp first; if the first request yields no rows or errors, use yfinance for all.

Run from repo root:

  .venv/bin/python backend/value_metrics_backfill.py --years 2 --provider yfinance

Optional SYMBOL_UNIVERSE_PATH or --symbols-file (default: telegram_agent/top1000_investments.json).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "backend"))

from dotenv import load_dotenv

from value_metrics_provider_fmp import fetch_ratios_history
from value_metrics_provider_yfinance_history import fetch_yfinance_metrics_history
from value_metrics_store import connect, init_db, upsert_metric_points


def _load_env() -> None:
    """Load repo-root `.env` first, then `backend/.env` (without overriding set keys)."""
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


def _fmp_probe(api_key: str, symbol: str, period: str, start_s: str, end_s: str) -> bool:
    """Return True if FMP returns at least one row for this probe."""
    try:
        pts = fetch_ratios_history(
            api_key=api_key,
            symbol=symbol,
            period=period,
            start_date=start_s,
            end_date=end_s,
        )
        return len(pts) > 0
    except Exception:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill value metric history into value_metrics SQLite.")
    ap.add_argument("--years", type=float, default=2.0, help="Lookback window in years (default: 2)")
    ap.add_argument(
        "--provider",
        type=str,
        default="yfinance",
        choices=("fmp", "yfinance", "auto"),
        help="Data source (default: yfinance)",
    )
    ap.add_argument(
        "--symbols-file",
        type=str,
        default="",
        help="JSON: list of {ticker} or dict ticker->priority (default: SYMBOL_UNIVERSE_PATH or telegram_agent/top1000_investments.json)",
    )
    ap.add_argument("--symbols", type=str, default="", help="Comma-separated tickers (overrides --symbols-file)")
    ap.add_argument(
        "--db",
        type=str,
        default="",
        help="SQLite path (default: VALUE_METRICS_DB_PATH or backend/data/value_metrics.sqlite)",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=-1.0,
        help="Sleep seconds after each symbol (default: 0.2 fmp, 0.35 yfinance)",
    )
    ap.add_argument("--periods", type=str, default="quarter,annual", help="Comma-separated: quarter,annual")
    ap.add_argument("--max-symbols", type=int, default=0, help="If >0, only first N symbols (debug)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    _load_env()
    api_key = (os.getenv("FMP_API_KEY") or "").strip()

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

    periods = [x.strip().lower() for x in args.periods.split(",") if x.strip()]
    for per in periods:
        if per not in ("quarter", "annual"):
            print(f"ERROR: invalid period {per!r}", file=sys.stderr)
            return 1

    provider = str(args.provider).strip().lower()
    use_yfinance = provider == "yfinance"
    if provider == "fmp":
        if not api_key:
            print("ERROR: FMP_API_KEY is not set (add to .env or export).", file=sys.stderr)
            return 1
    elif provider == "auto":
        if not api_key:
            use_yfinance = True
            print("auto: no FMP_API_KEY, using yfinance")
        else:
            probe_sym = symbols[0] if symbols else "AAPL"
            probe_per = periods[0] if periods else "quarter"
            if _fmp_probe(api_key, probe_sym, probe_per, start_s, end_s):
                use_yfinance = False
                print(f"auto: FMP returned data for probe {probe_sym}/{probe_per}, using fmp")
            else:
                use_yfinance = True
                print(f"auto: FMP probe failed or empty for {probe_sym}/{probe_per}, using yfinance")
    elif provider != "yfinance":
        print(f"ERROR: unknown provider {provider!r}", file=sys.stderr)
        return 1

    if float(args.sleep) < 0:
        sleep_s = 0.35 if use_yfinance else 0.2
    else:
        sleep_s = float(args.sleep)

    con = connect(db_path)
    init_db(con)

    total_upserted = 0
    ok = 0
    failed = 0

    print(f"DB: {db_path}")
    print(f"Window: {start_s} .. {end_s} (UTC dates, inclusive)")
    print(f"Periods: {periods}")
    print(f"Effective provider: {'yfinance' if use_yfinance else 'fmp'}")
    print(f"Symbols: {len(symbols)}")

    for i, sym in enumerate(symbols):
        sym_ok = True
        for per in periods:
            try:
                if use_yfinance:
                    pts = fetch_yfinance_metrics_history(
                        symbol=sym,
                        period=per,
                        start_date=start_s,
                        end_date=end_s,
                    )
                    prov = "yfinance"
                else:
                    pts = fetch_ratios_history(
                        api_key=api_key,
                        symbol=sym,
                        period=per,
                        start_date=start_s,
                        end_date=end_s,
                    )
                    prov = "fmp"
                n = upsert_metric_points(con, provider=prov, period=per, points=pts)
                total_upserted += n
            except Exception as e:
                sym_ok = False
                print(f"  [{sym}] {per} ERROR: {e}")
            time.sleep(max(0.0, sleep_s))
        if sym_ok:
            ok += 1
        else:
            failed += 1
        if (i + 1) % 25 == 0:
            print(f"  progress {i + 1}/{len(symbols)} symbols, upserted_rows≈{total_upserted}")

    con.close()
    print(f"Done. symbols_ok={ok} symbols_with_errors={failed} total_row_upserts={total_upserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
