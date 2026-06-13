#!/usr/bin/env python3
"""
Compare value-trading output across tier A/B/C models without persisting to SQLite.

Example (from repo root):

  .venv/bin/python backend/value_trading_model_compare.py
  .venv/bin/python backend/value_trading_model_compare.py --symbols MU,GOOGL --out backend/data/vt_compare.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

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
logger = logging.getLogger("value_trading_model_compare")

# One representative model per tier from the OpenRouter research ranking.
TIER_MODELS: List[Dict[str, str]] = [
    {
        "tier": "A",
        "model": "perplexity/sonar-reasoning-pro",
        "label": "Tier A — Perplexity Sonar Reasoning Pro (search + CoT, bulk sweet spot)",
    },
    {
        "tier": "B",
        "model": "perplexity/sonar-pro",
        "label": "Tier B — Perplexity Sonar Pro (deeper search, more citations)",
    },
    {
        "tier": "C",
        "model": "perplexity/sonar-pro-search",
        "label": "Tier C — Perplexity Sonar Pro Search (agentic multi-step research)",
    },
]


def _load_env() -> None:
    for p in (_REPO_ROOT / ".env", _BACKEND / ".env", _REPO_ROOT / "telegram_agent" / ".env"):
        if p.is_file():
            load_dotenv(p, override=False)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    _load_env()

    from value_trading_agent import load_value_trading_config, run_value_trading_for_symbol

    ap = argparse.ArgumentParser(description="Compare value-trading models (no DB writes)")
    ap.add_argument(
        "--db",
        default=os.getenv("VALUE_METRICS_DB_PATH", str(_BACKEND / "data" / "value_metrics.sqlite")),
    )
    ap.add_argument("--symbols", default="MU,GOOGL", help="Comma-separated tickers")
    ap.add_argument(
        "--out",
        default=str(_BACKEND / "data" / "value_trading_model_compare_MU_GOOGL.json"),
        help="Output JSON path",
    )
    ap.add_argument(
        "--sleep-seconds",
        type=float,
        default=2.0,
        help="Pause between LLM calls",
    )
    args = ap.parse_args()

    vm_db = Path(args.db).expanduser()
    if not vm_db.is_absolute():
        vm_db = _REPO_ROOT / vm_db

    symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    out_path = Path(args.out).expanduser()
    if not out_path.is_absolute():
        out_path = _REPO_ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    report: Dict[str, Any] = {
        "generated_ts_utc": _utcnow_iso(),
        "symbols": symbols,
        "models": TIER_MODELS,
        "persist": False,
        "runs": [],
        "summary": [],
    }

    for tier_spec in TIER_MODELS:
        model = tier_spec["model"]
        cfg = load_value_trading_config()
        cfg["value_trading_model"] = model

        for sym in symbols:
            logger.info("=== %s | %s | %s ===", tier_spec["tier"], model, sym)
            t0 = time.monotonic()
            err: str | None = None
            result: Dict[str, Any] | None = None
            try:
                result = run_value_trading_for_symbol(
                    vm_db,
                    sym,
                    cfg=cfg,
                    persist=False,
                )
                if result is None:
                    err = "LLM returned no parseable JSON"
            except Exception as e:
                err = str(e)
                logger.exception("%s %s failed", model, sym)

            elapsed = round(time.monotonic() - t0, 2)
            entry: Dict[str, Any] = {
                "tier": tier_spec["tier"],
                "tier_label": tier_spec["label"],
                "model": model,
                "symbol": sym,
                "elapsed_seconds": elapsed,
                "error": err,
            }
            if result:
                entry["assessment"] = {
                    "investment_name": result.get("investment_name"),
                    "overall_summary": result.get("overall_summary"),
                    "total_score": result.get("total_score"),
                    "pillar_scores": result.get("pillar_scores"),
                    "pillars": result.get("pillars"),
                    "produced_ts_utc": result.get("produced_ts_utc"),
                }
                report["summary"].append(
                    {
                        "tier": tier_spec["tier"],
                        "model": model,
                        "symbol": sym,
                        "total_score": result.get("total_score"),
                        "elapsed_seconds": elapsed,
                        "investment_name": result.get("investment_name"),
                    }
                )
            report["runs"].append(entry)
            time.sleep(float(args.sleep_seconds))

    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote comparison report to %s (%d runs)", out_path, len(report["runs"]))
    print(json.dumps({"out": str(out_path), "n_runs": len(report["runs"]), "summary": report["summary"]}, indent=2))
    failed = sum(1 for r in report["runs"] if r.get("error"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
