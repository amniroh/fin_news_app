"""Persist per-chat digest schedule (hours between runs)."""
import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)


def load_schedules(path: Path) -> Dict[str, float]:
    """Returns { 'chat_id_str': hours_float, ... }"""
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        out: Dict[str, float] = {}
        for k, v in (data or {}).items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    except Exception as e:
        logger.warning("Could not load schedule file %s: %s", path, e)
        return {}


def save_schedules(path: Path, schedules: Dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({str(k): float(v) for k, v in schedules.items()}, f, indent=2)
