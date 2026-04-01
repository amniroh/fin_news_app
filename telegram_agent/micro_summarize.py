"""Batched cheap LLM pass: condense long items to 1–2 sentences for the digest prompt."""
import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .models import NewsItem

logger = logging.getLogger(__name__)

MICRO_SYSTEM = """You compress news items. For each object in the input JSON array, write one neutral summary of 1–2 sentences in the same language as that item's text.
Return ONLY a JSON array of objects with keys "id" and "summary" (string). Same order and length as input. No markdown fences, no extra text."""

# ~50 tokens per summary line for cost estimate
ESTIMATED_OUTPUT_TOKENS_PER_ITEM = 55


def _micro_enabled(config: dict) -> bool:
    return bool(config.get("micro_summarize")) or os.getenv("MICRO_SUMMARIZE", "").lower() == "true"


def needs_micro(item: NewsItem, config: dict) -> bool:
    if not _micro_enabled(config):
        return False
    if item.condensed:
        return False
    min_tg = int(config.get("micro_min_chars_telegram", 400))
    min_rss = int(config.get("micro_min_chars_rss", 280))
    min_tw = int(config.get("micro_min_chars_twitter", 220))
    n = len((item.title or "")) + len((item.content or ""))
    if item.source_type == "telegram":
        return n >= min_tg
    if item.source_type == "twitter":
        return n >= min_tw
    return n >= min_rss


def _truncate(s: str, max_chars: int) -> str:
    """Cap input to micro model per item."""
    if not s:
        return ""
    s = s.strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rsplit(" ", 1)[0] + "…"


def build_micro_payload(
    items: List[NewsItem], config: dict
) -> List[Dict[str, str]]:
    max_body = int(config.get("micro_max_input_chars", 3500))
    out = []
    for it in items:
        out.append(
            {
                "id": it.id,
                "title": _truncate(it.title or "", 400),
                "text": _truncate(it.content or "", max_body),
            }
        )
    return out


def _parse_json_response(raw: str) -> List[Dict[str, Any]]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("expected JSON array")
    return data


async def _gemini_micro_batch(
    payload: List[Dict[str, str]], model: str, config: dict
) -> str:
    import google.generativeai as genai

    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing for micro summarizer")
    genai.configure(api_key=key)
    gen = genai.GenerativeModel(model)
    user = json.dumps(payload, ensure_ascii=False)
    full = f"{MICRO_SYSTEM}\n\nInput JSON:\n{user}"
    loop = asyncio.get_event_loop()

    def _call():
        return gen.generate_content(
            full,
            generation_config={
                "max_output_tokens": min(8192, 120 * len(payload) + 200),
                "temperature": 0.2,
            },
        )

    return (await loop.run_in_executor(None, _call)).text or "[]"


async def _openrouter_micro_batch(
    payload: List[Dict[str, str]], model: str, config: dict
) -> str:
    from openai import OpenAI

    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY missing for micro summarizer")
    client = OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1")
    user = json.dumps(payload, ensure_ascii=False)
    loop = asyncio.get_event_loop()

    def _call():
        return client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": MICRO_SYSTEM},
                {"role": "user", "content": f"Input JSON:\n{user}"},
            ],
            temperature=0.2,
            max_tokens=min(8192, 120 * len(payload) + 200),
        )

    resp = await loop.run_in_executor(None, _call)
    return resp.choices[0].message.content or "[]"


def _micro_provider(config: dict) -> str:
    p = (config.get("micro_provider") or os.getenv("MICRO_PROVIDER", "auto")).strip().lower()
    if p in ("gemini", "openrouter"):
        return p
    use_gemini = config.get("use_gemini") or os.getenv("USE_GEMINI", "").lower() == "true"
    if use_gemini and os.getenv("GEMINI_API_KEY", "").strip():
        return "gemini"
    if os.getenv("OPENROUTER_API_KEY", "").strip():
        return "openrouter"
    return ""


def _micro_model(config: dict, provider: str) -> str:
    m = os.getenv("MICRO_MODEL", "").strip()
    if m:
        return m
    if provider == "gemini":
        return config.get("micro_model_gemini", "gemini-1.5-flash")
    return config.get("micro_model_openrouter", "anthropic/claude-3-haiku")


def estimate_micro_batches_plan(items: List[NewsItem], config: dict) -> List[Dict[str, Any]]:
    """
    Cost estimate only: same batching as micro_summarize_items without calling APIs.
    """
    if not _micro_enabled(config):
        return []

    batch_size = int(config.get("micro_batch_size", 12))
    max_micro = int(config.get("max_micro_items_per_run", 80))
    to_process: List[NewsItem] = [it for it in items if needs_micro(it, config)][:max_micro]
    if not to_process:
        return []

    provider = _micro_provider(config)
    if not provider:
        return []

    model = _micro_model(config, provider)
    from .cost_estimate import estimate_micro_batch_cost

    out: List[Dict[str, Any]] = []
    for i in range(0, len(to_process), batch_size):
        batch = to_process[i : i + batch_size]
        payload = build_micro_payload(batch, config)
        user_json = json.dumps(payload, ensure_ascii=False)
        out_est = len(batch) * ESTIMATED_OUTPUT_TOKENS_PER_ITEM
        out.append(
            estimate_micro_batch_cost(
                user_json,
                MICRO_SYSTEM,
                model,
                out_est,
            )
        )
    return out


async def micro_summarize_items(items: List[NewsItem], config: dict) -> Tuple[List[NewsItem], List[Dict[str, Any]], int]:
    """
    Set item.condensed for items that need micro. Returns (items, batch_cost_infos, total_output_chars).
    """
    if not _micro_enabled(config):
        return items, [], 0

    batch_size = int(config.get("micro_batch_size", 12))
    max_micro = int(config.get("max_micro_items_per_run", 80))
    to_process: List[NewsItem] = [it for it in items if needs_micro(it, config)][:max_micro]

    if not to_process:
        for it in items:
            if it.condensed is None:
                it.condensed = None
        return items, [], 0

    provider = _micro_provider(config)
    if not provider:
        logger.warning("MICRO_SUMMARIZE enabled but no GEMINI_API_KEY or OPENROUTER_API_KEY; skipping micro.")
        return items, [], 0

    model = _micro_model(config, provider)
    id_to_summary: Dict[str, str] = {}
    batch_cost_infos: List[Dict[str, Any]] = []
    total_out_chars = 0

    from .cost_estimate import estimate_micro_batch_cost, count_tokens

    for i in range(0, len(to_process), batch_size):
        batch = to_process[i : i + batch_size]
        payload = build_micro_payload(batch, config)
        user_json = json.dumps(payload, ensure_ascii=False)
        out_est = len(batch) * ESTIMATED_OUTPUT_TOKENS_PER_ITEM

        try:
            if provider == "gemini":
                raw = await _gemini_micro_batch(payload, model, config)
            else:
                raw = await _openrouter_micro_batch(payload, model, config)
        except Exception as e:
            logger.error("Micro batch failed: %s", e)
            continue

        total_out_chars += len(raw)
        bi = estimate_micro_batch_cost(
            user_json,
            MICRO_SYSTEM,
            model,
            out_est,
        )
        batch_cost_infos.append(bi)

        try:
            parsed = _parse_json_response(raw)
        except Exception as e:
            logger.error("Micro batch JSON parse failed: %s", e)
            continue

        for row in parsed:
            sid = row.get("id")
            summ = (row.get("summary") or "").strip()
            if sid and summ:
                id_to_summary[sid] = summ

    for it in items:
        if it.id in id_to_summary:
            it.condensed = id_to_summary[it.id]

    return items, batch_cost_infos, total_out_chars
