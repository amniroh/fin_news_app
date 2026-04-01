"""Single-call digest: Market + Trend sections via one LLM request, then split by headers."""
import logging
import os
import re
from typing import List, Optional, Tuple

from .models import NewsItem
from .prompt_compact import item_to_prompt_snippet

logger = logging.getLogger(__name__)

# Exact header lines the model must emit (used for parsing).
HEADER_MARKET = "📊 MARKET & ASSET INTELLIGENCE"
HEADER_TREND = "📈 TREND & SIGNAL DETECTION"

USER_PROMPT_ITEMS = """Items:
---
{items_text}
---"""

SYSTEM_PROMPT_CONSOLIDATED = """You are a financial intelligence analyst. Below are items from the last {hours_label} across these specialized sources:

- Bloomberg: broad markets & macro breaking news
- Whale Alert: large on-chain crypto transactions (size = signal strength)
- Lookonchain: smart money wallet moves & crypto accumulation/distribution
- Wu Blockchain: Asia-focused crypto & regulatory news (often early on China/HK signals)
- SEC EDGAR: official US equity filings & regulatory actions (insider trades, 8-Ks)
- Federal Reserve RSS: macro policy — rate decisions, Fed statements, economic data

Gluing logic:
- Treat SEC + Fed items as macro ground truth; weight them highest for equities/rates narratives.
- Treat Whale Alert + Lookonchain as on-chain confirmation; a narrative is stronger when both fire together.
- Treat Wu Blockchain as an early Asia signal; if it clusters with Whale Alert, elevate urgency.
- Treat Bloomberg as the cross-asset connector — when it echoes any of the above, a signal has gone mainstream.
- Ignore items with no market or geopolitical relevance (ceremonies, unrelated domestic politics, etc).

Produce two sections. Use these exact headers and plain text only — no markdown, no code fences.

📊 MARKET & ASSET INTELLIGENCE
Cross-reference items with tickers, crypto assets, commodities, rates, FX, or macro instruments. Flag clusters where 2+ sources touch the same asset or theme. Summarize price levels or moves only if explicitly stated in the source — never invent figures. 4–8 bullets. Max 2000 chars.

📈 TREND & SIGNAL DETECTION
Identify topics heating up across multiple sources simultaneously. Call out which sources cluster on which theme and why it may matter before it goes mainstream. 4–8 bullets. Max 2000 chars.

Rules:
- Each section starts with its exact header line.
- Bullets start with •
- Neutral tone. No speculation beyond what sources imply.
- If an item is not from the listed sources or has no market/geopolitical relevance, skip it."""

SYSTEM_PROMPT_CONSOLIDATED_MINIMAL = """Financial intelligence analyst. Sources: Bloomberg; Whale Alert; Lookonchain; Wu Blockchain; SEC EDGAR; Fed RSS. Weight SEC+Fed for macro; Whale+Lookonchain on-chain; Wu Asia early signal; Bloomberg mainstream echo. Skip non-market noise.

Output two sections with exact header lines (plain text, no markdown):
📊 MARKET & ASSET INTELLIGENCE
Tickers, crypto, commodities, rates, FX; clusters; no invented prices. • bullets, max ~1600 chars.

📈 TREND & SIGNAL DETECTION
Cross-source clusters; which sources; early signals. • bullets, max ~1600 chars.

Rules: exact headers; • bullets; neutral tone."""


def _hours_label(config: dict) -> str:
    h = config.get("hours_back", 6)
    try:
        hf = float(h)
        if hf == int(hf):
            return f"{int(hf)} hours"
        return f"{hf} hours"
    except (TypeError, ValueError):
        return "6 hours"


def _system_consolidated(config: dict) -> str:
    style = (config.get("prompt_style") or "balanced").strip().lower()
    if style == "minimal":
        return SYSTEM_PROMPT_CONSOLIDATED_MINIMAL
    return SYSTEM_PROMPT_CONSOLIDATED.format(hours_label=_hours_label(config))


def strip_code_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t).strip()
    return t


def parse_digest_sections(raw: str) -> Tuple[str, str]:
    """
    Split consolidated model output into (market_block, trend_block).
    Each block includes its header line when found.
    """
    raw = strip_code_fences(raw)
    if not raw:
        return "", ""

    m = HEADER_MARKET
    t = HEADER_TREND
    i_m = raw.find(m)
    i_t = raw.find(t)

    if i_m == -1 and i_t == -1:
        logger.warning(
            "Digest parse: missing both %r and %r; returning full text as trend section.",
            m,
            t,
        )
        return "", raw.strip()

    if i_m == -1:
        return "", raw[i_t:].strip()
    if i_t == -1:
        return raw[i_m:].strip(), ""

    if i_m < i_t:
        market = raw[i_m:i_t].strip()
        trend = raw[i_t:].strip()
    else:
        trend = raw[i_t:i_m].strip()
        market = raw[i_m:].strip()
    return market, trend


def _build_items_text(items: List[NewsItem], config: dict) -> str:
    total_cap = int(config.get("max_prompt_chars", 6500))
    parts: List[str] = []
    total = 0
    sep_len = 2
    for it in items:
        part = item_to_prompt_snippet(it, config)
        next_total = total + (sep_len if parts else 0) + len(part)
        if next_total > total_cap:
            break
        parts.append(part)
        total = next_total

    if len(parts) < len(items):
        logger.info(
            "Prompt budget: included %d/%d items (MAX_PROMPT_CHARS=%s)",
            len(parts),
            len(items),
            total_cap,
        )
    return "\n\n".join(parts)


def build_consolidated_digest_prompts(items: List[NewsItem], config: dict) -> Tuple[str, str]:
    """Single system + user prompt for both digest sections."""
    items_text = _build_items_text(items, config)
    system_prompt = _system_consolidated(config)
    user_prompt = USER_PROMPT_ITEMS.format(items_text=items_text)
    return system_prompt, user_prompt


def build_digest_prompts(
    items: List[NewsItem], config: dict, digest_section: str = "trend"
) -> Tuple[str, str]:
    """Backward-compatible name: returns consolidated digest prompts (digest_section ignored)."""
    return build_consolidated_digest_prompts(items, config)


def _get_openrouter_client():
    try:
        from openai import OpenAI
    except ImportError:
        return None
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        return None
    return OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1")


def _get_gemini_client():
    try:
        import google.generativeai as genai
    except ImportError:
        return None
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    genai.configure(api_key=key)
    return genai.GenerativeModel("gemini-1.5-flash")


def _max_output_tokens(config: dict) -> int:
    return int(config.get("digest_max_output_tokens", 3500))


async def summarize_digest(
    items: List[NewsItem],
    config: Optional[dict] = None,
    dry_run: bool = False,
) -> Tuple[str, str]:
    """
    One LLM call; returns (market_text, trend_text) with section headers when parse succeeds.
    """
    if not items:
        return "No new items in this window.", ""

    config = config or {}
    system_prompt, user_prompt = build_consolidated_digest_prompts(items, config)
    label = "Consolidated digest (Market + Trend)"
    max_out = _max_output_tokens(config)

    if dry_run:
        block = (
            f"=== [{label}] SYSTEM PROMPT ===\n\n"
            + system_prompt
            + f"\n\n=== [{label}] USER PROMPT ===\n\n"
            + user_prompt
            + f"\n\n---\n(approx chars: system={len(system_prompt)}, user={len(user_prompt)})"
        )
        return block, ""

    use_gemini = config.get("use_gemini", False) or bool(os.getenv("USE_GEMINI", "").lower() == "true")
    model = config.get("llm_model", "anthropic/claude-3.5-sonnet")
    style = (config.get("prompt_style") or "balanced").strip().lower()
    logger.info(
        "[%s] single call; prompt ~%d chars user + %d system (style=%s, max_out_tokens=%s)",
        label,
        len(user_prompt),
        len(system_prompt),
        style,
        max_out,
    )

    raw = ""

    if use_gemini:
        gemini = _get_gemini_client()
        if gemini:
            try:
                import asyncio

                full = f"{system_prompt}\n\n{user_prompt}"
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: gemini.generate_content(
                        full,
                        generation_config={"max_output_tokens": max_out},
                    ),
                )
                raw = (response.text or "").strip()
            except Exception as e:
                logger.warning("Gemini summarization failed: %s", e)

    if not raw:
        client = _get_openrouter_client()
        if client:
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.3,
                    max_tokens=max_out,
                )
                raw = (response.choices[0].message.content or "").strip()
            except Exception as e:
                logger.error("OpenRouter summarization failed: %s", e)
                err = f"[Summarization failed: {e}. Raw item count: {len(items)}]"
                return err, ""

    if not raw:
        return (
            f"[No LLM configured. Set GEMINI_API_KEY (with USE_GEMINI=true) or OPENROUTER_API_KEY. Raw item count: {len(items)}]",
            "",
        )

    market_text, trend_text = parse_digest_sections(raw)
    if not market_text and not trend_text:
        return raw, ""
    if not market_text or not trend_text:
        logger.warning(
            "Digest parse incomplete (market=%s chars, trend=%s chars); check model output headers.",
            len(market_text),
            len(trend_text),
        )
    return market_text, trend_text
