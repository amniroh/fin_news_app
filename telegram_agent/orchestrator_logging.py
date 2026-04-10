"""Append-only file logging for `agent orchestrate` (path from ORCHESTRATOR_LOG_PATH)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_LOG_HANDLER_ATTR = "_orchestrator_file_handler_path"


def orchestrator_log_file_path() -> Path:
    """
    Path to the orchestrator log file.

    ORCHESTRATOR_LOG_PATH: absolute path, or path relative to the repo root
    (parent of the `telegram_agent` package). Default: telegram_agent/data/orchestrator.log
    """
    raw = os.getenv("ORCHESTRATOR_LOG_PATH", "").strip()
    repo_root = Path(__file__).resolve().parent.parent
    if not raw:
        return Path(__file__).resolve().parent / "data" / "orchestrator.log"
    p = Path(raw)
    if p.is_absolute():
        return p
    return repo_root / p


def attach_orchestrator_file_logging(path: Optional[Path] = None) -> Path:
    """
    Append INFO+ logs from all loggers to the given file (same format as stderr).

    Idempotent per path in a single process.
    """
    path = path or orchestrator_log_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    resolved = str(path.resolve())
    if getattr(root, _LOG_HANDLER_ATTR, None) == resolved:
        return path

    fh = logging.FileHandler(path, encoding="utf-8", mode="a")
    fh.setLevel(logging.INFO)
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(fh)
    setattr(root, _LOG_HANDLER_ATTR, resolved)

    logging.getLogger(__name__).info(
        "Orchestrator file logging enabled (path=%s)", path
    )
    return path


def append_orchestrator_stdout_summary(path: Path, payload: Dict[str, Any]) -> None:
    """Append the JSON blob that is printed to stdout after a run (separate from log records)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n### orchestrator stdout summary ({stamp}) ###\n")
        f.write(json.dumps(payload, indent=2, default=str))
        f.write("\n")
