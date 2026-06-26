#!/usr/bin/env python3
"""
Daily refresh for interesting stocks: recent prices, momentum/RSI (standard metrics),
and analyst rating snapshots.

Run after ``interesting_stocks_daily_backfill.py`` (gap fills + news) or standalone.

Usage (repo root):

  .venv/bin/python backend/daily_market_refresh.py
  .venv/bin/python backend/daily_market_refresh.py --skip-prices
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (_REPO_ROOT, _BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("daily_market_refresh")


def _load_env() -> None:
    for p in (_REPO_ROOT / ".env", _BACKEND / ".env", _REPO_ROOT / "telegram_agent" / ".env"):
        if p.is_file():
            load_dotenv(p, override=False)


def _interesting_symbols(vm_db: Path) -> list[str]:
    from value_metrics_store import connect, init_db, list_interesting_stocks

    con = connect(vm_db)
    init_db(con)
    try:
        return [str(r["symbol"]).strip().upper() for r in list_interesting_stocks(con) if r.get("symbol")]
    finally:
        con.close()


def _refresh_prices(symbols: list[str], *, days: int) -> dict:
    if not symbols:
        return {"skipped": True, "reason": "no symbols"}
    try:
        from interesting_stocks_service import _backfill_prices

        return _backfill_prices(symbols, days=int(days))
    except Exception as e:
        logger.exception("Price refresh failed")
        return {"ok": False, "error": str(e)}


def _refresh_standard_metrics(symbols: list[str], vm_db: Path) -> dict:
    from value_metrics_provider_standard import fetch_standard_metrics
    from value_metrics_store import connect, init_db, upsert_standard_metrics

    ok: list[str] = []
    failed: list[dict[str, str]] = []
    con = connect(vm_db)
    init_db(con)
    try:
        for sym in symbols:
            try:
                row = fetch_standard_metrics(sym)
                upsert_standard_metrics(con, provider="yfinance", rows=[row])
                ok.append(sym)
            except Exception as e:
                failed.append({"symbol": sym, "error": str(e)})
            time.sleep(0.35)
    finally:
        con.close()
    return {"ok": ok, "failed": failed, "n_ok": len(ok), "n_failed": len(failed)}


def _refresh_analyst_ratings(symbols: list[str], vm_db: Path) -> dict:
    from interesting_stocks_service import fetch_analyst_ratings_for_symbols

    return fetch_analyst_ratings_for_symbols(vm_db, symbols)


def main() -> int:
    _load_env()

    ap = argparse.ArgumentParser(description="Daily price/momentum/analyst refresh for interesting stocks")
    ap.add_argument(
        "--db",
        default=os.getenv("VALUE_METRICS_DB_PATH", str(_BACKEND / "data" / "value_metrics.sqlite")),
    )
    ap.add_argument("--price-days", type=int, default=7, help="Recent daily bars to (re)fetch per symbol")
    ap.add_argument("--skip-prices", action="store_true")
    ap.add_argument(
        "--skip-daily-metrics",
        action="store_true",
        help="Skip extending vm_metric_points daily rows through today",
    )
    ap.add_argument(
        "--daily-metrics-since",
        default="",
        help="Optional YYYY-MM-DD floor when extending daily metrics (catch-up runs)",
    )
    ap.add_argument("--skip-metrics", action="store_true", help="Skip momentum/RSI standard metrics")
    ap.add_argument("--skip-analyst", action="store_true")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    vm_db = Path(args.db).expanduser()
    if not vm_db.is_absolute():
        vm_db = _REPO_ROOT / vm_db
    vm_db.parent.mkdir(parents=True, exist_ok=True)

    from interesting_stocks_service import seed_interesting_stocks_from_universe

    seed_interesting_stocks_from_universe(vm_db)
    symbols = _interesting_symbols(vm_db)
    logger.info("Refreshing %s interesting stock(s)", len(symbols))

    result: dict = {"n_symbols": len(symbols), "symbols_sample": symbols[:12]}
    if not args.skip_prices:
        logger.info("Fetching recent daily prices (%s-day window)", args.price_days)
        result["prices"] = _refresh_prices(symbols, days=int(args.price_days))
    if not args.skip_daily_metrics:
        from interesting_stocks_service import extend_recent_daily_metrics

        since = str(args.daily_metrics_since or "").strip() or None
        logger.info("Extending daily vm_metric_points through today (since=%s)", since or "per-symbol last date")
        result["daily_metrics"] = extend_recent_daily_metrics(vm_db, symbols=symbols, since_date=since)
    if not args.skip_metrics:
        logger.info("Refreshing standard metrics (momentum, RSI, returns)")
        result["standard_metrics"] = _refresh_standard_metrics(symbols, vm_db)
    if not args.skip_analyst:
        logger.info("Fetching analyst rating snapshots")
        result["analyst_ratings"] = _refresh_analyst_ratings(symbols, vm_db)

    if args.json_out:
        out_path = Path(args.json_out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    print(json.dumps({k: v for k, v in result.items() if k != "symbols_sample"}, indent=2, default=str))
    failed = (
        (result.get("standard_metrics") or {}).get("n_failed", 0)
        + (result.get("daily_metrics") or {}).get("n_failed", 0)
        + len((result.get("prices") or {}).get("failed") or [])
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
