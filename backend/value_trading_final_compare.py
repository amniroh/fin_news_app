#!/usr/bin/env python3
"""
Final value-trading model shootout: Gemini (:online + thinking) vs Perplexity Sonar Pro / Pro Search.

No SQLite writes. Full pillar rationales saved to JSON.

  .venv/bin/python backend/value_trading_final_compare.py
  .venv/bin/python backend/value_trading_final_compare.py --symbols MU,GOOGL
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
logger = logging.getLogger("value_trading_final_compare")

FINAL_COMPARE_MODELS: List[Dict[str, Any]] = [
    {
        "id": "gemini-3-flash-online",
        "model": "google/gemini-3-flash-preview",
        "label": "Gemini 3 Flash Preview :online + reasoning (medium)",
        "value_trading_online": "true",
        "value_trading_reasoning_effort": "medium",
    },
    {
        "id": "gemini-25-flash-online",
        "model": "google/gemini-2.5-flash",
        "label": "Gemini 2.5 Flash :online + thinking (medium)",
        "value_trading_online": "true",
        "value_trading_reasoning_effort": "medium",
    },
    {
        "id": "sonar-pro",
        "model": "perplexity/sonar-pro",
        "label": "Perplexity Sonar Pro (built-in search)",
        "value_trading_online": "false",
        "value_trading_reasoning_effort": "",
    },
    {
        "id": "sonar-pro-search",
        "model": "perplexity/sonar-pro-search",
        "label": "Perplexity Sonar Pro Search (agentic research)",
        "value_trading_online": "false",
        "value_trading_reasoning_effort": "",
    },
]


def _load_env() -> None:
    for p in (_REPO_ROOT / ".env", _BACKEND / ".env", _REPO_ROOT / "telegram_agent" / ".env"):
        if p.is_file():
            load_dotenv(p, override=False)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cfg_for_spec(base_cfg: dict, spec: Dict[str, Any]) -> dict:
    cfg = copy.deepcopy(base_cfg)
    cfg["value_trading_model"] = str(spec["model"])
    cfg["value_trading_online"] = str(spec.get("value_trading_online", "auto"))
    cfg["value_trading_reasoning_effort"] = str(spec.get("value_trading_reasoning_effort", "") or "")
    if spec.get("value_trading_reasoning_max_tokens") is not None:
        cfg["value_trading_reasoning_max_tokens"] = int(spec["value_trading_reasoning_max_tokens"])
    return cfg


def main() -> int:
    _load_env()

    from value_trading_agent import (
        build_value_trading_openrouter_extra,
        load_value_trading_config,
        resolve_value_trading_model,
        run_value_trading_for_symbol,
    )

    ap = argparse.ArgumentParser(description="Final value-trading model comparison (no DB writes)")
    ap.add_argument(
        "--db",
        default=os.getenv("VALUE_METRICS_DB_PATH", str(_BACKEND / "data" / "value_metrics.sqlite")),
    )
    ap.add_argument("--symbols", default="MU,GOOGL")
    ap.add_argument(
        "--out",
        default=str(_BACKEND / "data" / "value_trading_final_compare_MU_GOOGL.json"),
    )
    ap.add_argument("--sleep-seconds", type=float, default=2.0)
    args = ap.parse_args()

    vm_db = Path(args.db).expanduser()
    if not vm_db.is_absolute():
        vm_db = _REPO_ROOT / vm_db

    symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    out_path = Path(args.out).expanduser()
    if not out_path.is_absolute():
        out_path = _REPO_ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    base_cfg = load_value_trading_config()
    report: Dict[str, Any] = {
        "generated_ts_utc": _utcnow_iso(),
        "symbols": symbols,
        "models": FINAL_COMPARE_MODELS,
        "persist": False,
        "runs": [],
        "summary": [],
    }

    for spec in FINAL_COMPARE_MODELS:
        cfg = _cfg_for_spec(base_cfg, spec)
        resolved = resolve_value_trading_model(cfg)
        extra = build_value_trading_openrouter_extra(cfg, model=resolved)

        for sym in symbols:
            logger.info("=== %s | %s | %s ===", spec["id"], resolved, sym)
            t0 = time.monotonic()
            err: Optional[str] = None
            result: Optional[Dict[str, Any]] = None
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
                logger.exception("%s %s failed", resolved, sym)

            elapsed = round(time.monotonic() - t0, 2)
            entry: Dict[str, Any] = {
                "id": spec["id"],
                "label": spec["label"],
                "model_requested": spec["model"],
                "model_resolved": resolved,
                "openrouter_extra": extra,
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
                        "id": spec["id"],
                        "model_resolved": resolved,
                        "symbol": sym,
                        "total_score": result.get("total_score"),
                        "pillar_scores": result.get("pillar_scores"),
                        "elapsed_seconds": elapsed,
                        "investment_name": result.get("investment_name"),
                    }
                )
            report["runs"].append(entry)
            time.sleep(float(args.sleep_seconds))

    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote final comparison to %s (%d runs)", out_path, len(report["runs"]))
    print(json.dumps({"out": str(out_path), "n_runs": len(report["runs"]), "summary": report["summary"]}, indent=2))
    failed = sum(1 for r in report["runs"] if r.get("error"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
