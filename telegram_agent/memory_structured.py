"""Structured agent memory: capped trends + dated suggestions (merged in Python)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MEMORY_VERSION = 3

# Legacy v2 used plain strings for trends.
_LEGACY_V2 = 2

TrendItem = Dict[str, Any]
SuggestionLogItem = Dict[str, Any]


def empty_memory_state() -> Dict[str, Any]:
    return {
        "version": MEMORY_VERSION,
        "strongest_trends": [],  # list of {text, confidence 0-10}
        "recent_trends": [],
        "suggestions_log": [],  # list of {text, confidence 0-10}
    }


def _clamp_confidence_int(x: Any, default: int = 3) -> int:
    try:
        v = int(float(x))
    except (TypeError, ValueError):
        v = default
    return max(0, min(10, v))


def _trend_text_key(text: str) -> str:
    t = (text or "").strip()
    return t[:160] if t else ""


def _normalize_trend_item(x: Any) -> TrendItem:
    if isinstance(x, dict):
        raw = x.get("text") or x.get("trend") or x.get("summary")
        text = (str(raw) if raw is not None else "").strip()
        conf = _clamp_confidence_int(x.get("confidence"), default=3)
        return {"text": text, "confidence": conf}
    s = str(x).strip() if x is not None else ""
    return {"text": s, "confidence": 3}


def _normalize_suggestion_log_item(x: Any) -> SuggestionLogItem:
    if isinstance(x, dict):
        raw = x.get("text") or x.get("line")
        text = (str(raw) if raw is not None else "").strip()
        conf = _clamp_confidence_int(x.get("confidence"), default=3)
        return {"text": text, "confidence": conf}
    s = str(x).strip() if x is not None else ""
    return {"text": s, "confidence": 3}


def _migrate_v2_to_v3(d: Dict[str, Any]) -> Dict[str, Any]:
    out = empty_memory_state()
    for x in d.get("strongest_trends") or []:
        t = _normalize_trend_item(x)
        t["confidence"] = max(1, min(5, t["confidence"]))  # legacy text → cautious default
        out["strongest_trends"].append(t)
    for x in d.get("recent_trends") or []:
        t = _normalize_trend_item(x)
        t["confidence"] = max(1, min(5, t["confidence"]))
        out["recent_trends"].append(t)
    for x in d.get("suggestions_log") or []:
        out["suggestions_log"].append(_normalize_suggestion_log_item(x))
    return out


def _ensure_v3_structured(d: Dict[str, Any]) -> Dict[str, Any]:
    if not d:
        return empty_memory_state()
    v = d.get("version")
    if v == MEMORY_VERSION:
        st = [_normalize_trend_item(x) for x in (d.get("strongest_trends") or [])]
        rt = [_normalize_trend_item(x) for x in (d.get("recent_trends") or [])]
        sg = [_normalize_suggestion_log_item(x) for x in (d.get("suggestions_log") or [])]
        return {
            "version": MEMORY_VERSION,
            "strongest_trends": st,
            "recent_trends": rt,
            "suggestions_log": sg,
        }
    if v == _LEGACY_V2:
        return _migrate_v2_to_v3(d)
    # Unknown version but dict-shaped: try v2 migration
    if "strongest_trends" in d or "recent_trends" in d:
        return _migrate_v2_to_v3({**d, "version": _LEGACY_V2})
    return empty_memory_state()


def parse_memory_payload(text: str, meta_json: Optional[str]) -> Dict[str, Any]:
    """Load structured memory from memories row; fall back to plain text wrapper."""
    if meta_json:
        try:
            meta = json.loads(meta_json)
            if isinstance(meta, dict) and meta.get("structured"):
                return _ensure_v3_structured(meta["structured"])
        except json.JSONDecodeError:
            pass
    t = (text or "").strip()
    if t.startswith("{"):
        try:
            d = json.loads(t)
            if isinstance(d, dict):
                if d.get("version") == MEMORY_VERSION:
                    return _ensure_v3_structured(d)
                if d.get("version") == _LEGACY_V2:
                    return _migrate_v2_to_v3(d)
                if "strongest_trends" in d or "recent_trends" in d:
                    return _ensure_v3_structured(d)
        except json.JSONDecodeError:
            pass
    return empty_memory_state()


def format_memory_for_prompt(structured: Dict[str, Any], *, max_chars: int = 6000) -> str:
    """Compact text for LLM user prompt."""
    s = _ensure_v3_structured(structured)
    lines: List[str] = []
    st = s.get("strongest_trends") or []
    rt = s.get("recent_trends") or []
    sg = s.get("suggestions_log") or []
    lines.append("=== STRONGEST TRENDS (capped list; confidence 0–10 per item) ===")
    for i, item in enumerate(st[:25], 1):
        it = _normalize_trend_item(item)
        lines.append(f"{i}. [{it['confidence']}/10] {it['text']}")
    lines.append("=== MOST RECENT TRENDS (capped list; confidence 0–10 per item) ===")
    for i, item in enumerate(rt[:25], 1):
        it = _normalize_trend_item(item)
        lines.append(f"{i}. [{it['confidence']}/10] {it['text']}")
    lines.append("=== RECENT SUGGESTION LOG (last entries; confidence 0–10) ===")
    for i, item in enumerate(sg[-40:], 1):
        it = _normalize_suggestion_log_item(item)
        lines.append(f"{i}. [{it['confidence']}/10] {it['text']}")
    out = "\n".join(lines)
    if len(out) > max_chars:
        return out[: max_chars - 20] + "\n... [truncated]"
    return out


def _as_str(x: Any) -> str:
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, dict):
        return json.dumps(x, ensure_ascii=False)[:500]
    return str(x)[:500]


def _merge_trend_lists(
    base: List[Any],
    incoming: List[Any],
    cap: int,
) -> List[TrendItem]:
    llm_n = [_normalize_trend_item(x) for x in incoming]
    prev_n = [_normalize_trend_item(x) for x in base]
    seen: set[str] = set()
    out: List[TrendItem] = []
    for t in llm_n + prev_n:
        k = _trend_text_key(t["text"])
        if not k:
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out[:cap]


def _suggestion_text_for_cutoff(s: str) -> str:
    return (s or "").strip()


def merge_memory_state(
    prev: Dict[str, Any],
    llm_memory: Dict[str, Any],
    *,
    now: datetime,
    cap_strongest: int,
    cap_recent: int,
    suggestion_days: int,
) -> Dict[str, Any]:
    """
    Merge LLM `memory_update` into previous state and apply caps.
    Each trend line: { "text": "...", "confidence": 0..10 } (strings still accepted).
    """
    base = _ensure_v3_structured(prev if isinstance(prev, dict) else empty_memory_state())

    st = list(llm_memory.get("strongest_trends") or [])
    rt = list(llm_memory.get("recent_trends") or [])
    sg = list(llm_memory.get("suggestions_log") or [])

    merged_st = _merge_trend_lists(base.get("strongest_trends") or [], st, cap_strongest)
    merged_rt = _merge_trend_lists(base.get("recent_trends") or [], rt, cap_recent)

    old_sg = base.get("suggestions_log") or []
    cutoff = now - timedelta(days=suggestion_days)
    kept_old: List[SuggestionLogItem] = []
    for e in old_sg:
        it = _normalize_suggestion_log_item(e)
        s = _suggestion_text_for_cutoff(it["text"])
        if s[:10].isdigit() or "T" in s[:25]:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00")[:25])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= cutoff:
                    kept_old.append(it)
            except Exception:
                kept_old.append(it)
        else:
            kept_old.append(it)

    new_sg = [_normalize_suggestion_log_item(x) for x in sg]
    combined = (kept_old + new_sg)[-200:]
    combined = combined[-80:]

    return {
        "version": MEMORY_VERSION,
        "strongest_trends": merged_st,
        "recent_trends": merged_rt,
        "suggestions_log": combined,
    }


def memory_meta_wrapper(structured: Dict[str, Any]) -> Dict[str, Any]:
    return {"structured": _ensure_v3_structured(structured), "format": "structured_v3"}
