#!/usr/bin/env python3
"""
Run intrinsic value (6-pillar) assessments for interesting stocks.

Usage (from repo root):

  .venv/bin/python backend/value_trading_agent_run.py
  .venv/bin/python backend/value_trading_agent_run.py --symbols AAPL,MSFT
  .venv/bin/python backend/value_trading_agent_run.py --max-priority 1 --dry-run
  .venv/bin/python backend/value_trading_agent_run.py --min-reassess-days 60
  .venv/bin/python backend/value_trading_agent_run.py --skip-if-today

Default model: google/gemini-2.5-flash:online with medium reasoning.
Batches VALUE_TRADING_BATCH_SIZE tickers per LLM call (default 10).
Skips tickers assessed within VALUE_TRADING_REASSESS_DAYS (default 60).

Requires OPENROUTER_API_KEY (same as research agent).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
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
logger = logging.getLogger("value_trading_agent_run")


def _load_env() -> None:
    for p in (_REPO_ROOT / ".env", _BACKEND / ".env", _REPO_ROOT / "telegram_agent" / ".env"):
        if p.is_file():
            load_dotenv(p, override=False)


def main() -> int:
    _load_env()

    from value_trading_agent import (
        build_value_trading_prompts,
        load_value_trading_config,
        run_value_trading_for_interesting_stocks,
    )

    ap = argparse.ArgumentParser(description="Value-trading agent for interesting stocks")
    ap.add_argument(
        "--db",
        default=os.getenv("VALUE_METRICS_DB_PATH", str(_BACKEND / "data" / "value_metrics.sqlite")),
    )
    ap.add_argument("--symbols", default="", help="Comma-separated tickers (default: all interesting stocks)")
    ap.add_argument("--max-priority", type=int, default=None, help="Only universe priority <= N")
    ap.add_argument("--max-symbols", type=int, default=0, help="Cap symbols processed (0=all)")
    ap.add_argument("--dry-run", action="store_true", help="Build prompts only; no LLM or DB writes")
    ap.add_argument(
        "--dry-run-out",
        default="",
        help="With --dry-run and --symbols: write first symbol prompts to this path",
    )
    ap.add_argument(
        "--min-reassess-days",
        type=int,
        default=None,
        help="Only assess symbols with no assessment in the last N days (default: VALUE_TRADING_REASSESS_DAYS=60)",
    )
    ap.add_argument(
        "--skip-if-today",
        action="store_true",
        help="Shortcut for --min-reassess-days 1",
    )
    ap.add_argument("--model", default=None, help="OpenRouter model override (VALUE_TRADING_MODEL)")
    ap.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Tickers per LLM call (default: VALUE_TRADING_BATCH_SIZE=10)",
    )
    args = ap.parse_args()

    vm_db = Path(args.db).expanduser()
    if not vm_db.is_absolute():
        vm_db = _REPO_ROOT / vm_db

    cfg = load_value_trading_config()
    if args.model:
        cfg["value_trading_model"] = args.model.strip()
    if args.batch_size is not None:
        cfg["value_trading_batch_size"] = max(1, int(args.batch_size))

    symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]

    if args.dry_run and args.dry_run_out and symbols:
        system, user, meta = build_value_trading_prompts(vm_db, symbols[0], cfg=cfg)
        out_path = Path(args.dry_run_out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            f"=== SYSTEM ===\n\n{system}\n\n=== USER ({symbols[0]}) ===\n\n{user}\n",
            encoding="utf-8",
        )
        logger.info("Wrote dry-run prompts to %s", out_path)

    result = run_value_trading_for_interesting_stocks(
        vm_db,
        cfg=cfg,
        symbols=symbols or None,
        max_priority=args.max_priority,
        max_symbols=int(args.max_symbols),
        dry_run=bool(args.dry_run),
        skip_if_today=bool(args.skip_if_today),
        min_reassess_days=args.min_reassess_days,
    )
    print(json.dumps(result, indent=2))
    return 0 if not result.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
