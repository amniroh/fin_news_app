"""Batched LLM extraction of tradable tickers/symbols from news text."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

EXTRACT_SYSTEM = """You extract investment instruments mentioned or clearly implied in each news item.
Return ONLY valid JSON (no markdown fences): a JSON array with one object per input item, in the SAME ORDER as given.

Each object must have:
- "id": string (copy exactly from input)
- "symbols": array of strings, each a tradable symbol. Use:
  - US equities: ticker like AAPL, NVDA
  - Major crypto: BTC-USD, ETH-USD, SOL-USD (yfinance-style) when the asset is crypto
  - Major ETFs/indexes: SPY, QQQ, ^GSPC, ^VIX if clearly the subject
  - FX pairs as EURUSD=X or similar only if clearly tradable and central to the item
Rules:
- Include at most 8 symbols per item; prefer the most central to the story.
- Do NOT output common English words, country codes alone, or generic terms (THE, FOR, GDP, CEO).
- If nothing is clearly a tradable instrument, use "symbols": [].
- Do not invent symbols not supported by the text."""

MentionRow = Tuple[str, str, float]  # symbol, mention_type, confidence


def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    t = text.strip()
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1].rsplit(" ", 1)[0] + "…"


def _parse_llm_json(raw: str) -> List[Dict[str, Any]]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("expected JSON array")
    return data


def openrouter_extract_user_content(rows: Sequence[Any], max_chars: int) -> str:
    """User message body as sent to OpenRouter (matches _extract_openrouter)."""
    payload = _rows_to_payload(rows, max_chars)
    user = json.dumps(payload, ensure_ascii=False)
    return f"Input JSON array:\n{user}"


def _rows_to_payload(
    rows: Sequence[Any], max_chars: int
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for row in rows:
        nid = row["id"]
        title = row["title"] or ""
        body = row["content"] or ""
        text = _truncate(f"{title}\n{body}", max_chars)
        out.append({"id": nid, "title": title[:300], "text": text})
    return out


def _extract_openrouter(
    payload: List[Dict[str, str]], model: str, max_out: int
) -> str:
    from openai import OpenAI

    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    client = OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1")
    user = json.dumps(payload, ensure_ascii=False)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": f"Input JSON array:\n{user}"},
        ],
        temperature=0.1,
        max_tokens=max_out,
    )
    return (resp.choices[0].message.content or "").strip()


def _extract_gemini(payload: List[Dict[str, str]], model: str, max_out: int) -> str:
    import google.generativeai as genai

    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    genai.configure(api_key=key)
    gen = genai.GenerativeModel(model)
    user = json.dumps(payload, ensure_ascii=False)
    full = f"{EXTRACT_SYSTEM}\n\nInput JSON array:\n{user}"
    resp = gen.generate_content(
        full,
        generation_config={"max_output_tokens": max_out, "temperature": 0.1},
    )
    return (resp.text or "").strip()


def extract_symbols_llm_batch(
    cfg: dict,
    rows: Sequence[Any],
) -> Dict[str, List[MentionRow]]:
    """
    Returns mapping news_id -> list of (symbol, mention_type, confidence).
    """
    if not rows:
        return {}

    max_chars = int(cfg.get("extract_max_chars_per_item", 2200))
    payload = _rows_to_payload(rows, max_chars)
    model = (cfg.get("extract_llm_model") or "anthropic/claude-3-haiku").strip()
    # ~40 tokens per item + JSON overhead
    max_out = min(8192, 120 * len(payload) + 400)

    raw = ""
    try:
        # Prefer OpenRouter when configured (cheap haiku / mini); else Gemini.
        if os.getenv("OPENROUTER_API_KEY", "").strip():
            raw = _extract_openrouter(payload, model, max_out)
        elif os.getenv("GEMINI_API_KEY", "").strip():
            raw = _extract_gemini(
                payload,
                cfg.get("micro_model_gemini", "gemini-1.5-flash"),
                max_out,
            )
        else:
            raise RuntimeError("No OPENROUTER_API_KEY or GEMINI_API_KEY for extract LLM")
    except Exception as e:
        logger.warning("LLM extract batch failed: %s", e)
        return {}

    try:
        parsed = _parse_llm_json(raw)
    except Exception as e:
        logger.warning("LLM extract JSON parse failed: %s | first 400 chars: %s", e, raw[:400])
        return {}

    out: Dict[str, List[MentionRow]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("id") or "")
        syms = item.get("symbols") or []
        if not isinstance(syms, list):
            syms = []
        mentions: List[MentionRow] = []
        for s in syms[:12]:
            if not isinstance(s, str):
                continue
            sym = s.strip().upper().replace(" ", "")
            if not sym or len(sym) > 20:
                continue
            mentions.append((sym, "llm", 0.82))
        if nid:
            out[nid] = mentions

    # Ensure we have keys for payload ids when model returned fewer rows
    expected = {str(r["id"]) for r in rows}
    for eid in expected:
        out.setdefault(eid, [])

    return out


def llm_extract_available(cfg: dict) -> bool:
    if os.getenv("OPENROUTER_API_KEY", "").strip():
        return True
    if os.getenv("GEMINI_API_KEY", "").strip():
        return True
    return False
