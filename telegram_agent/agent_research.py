"""Deep research: LLM proposes opportunities from news + price context."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram_agent.agent_db import (
    connect,
    init_db,
    fetch_news_rows_between,
    top_mentioned_symbols,
    get_close_at_or_before,
    insert_recommendation,
    latest_memory,
)
from telegram_agent.summarizer import _get_openrouter_client

logger = logging.getLogger(__name__)


def _price_context(con, symbol: str, asof: datetime) -> Dict[str, Any]:
    out: Dict[str, Any] = {"symbol": symbol}
    for days, label in [(1, "ret_1d"), (5, "ret_5d"), (30, "ret_30d")]:
        t0 = asof - timedelta(days=days)
        c0 = get_close_at_or_before(con, symbol, t0)
        c1 = get_close_at_or_before(con, symbol, asof)
        if c0 and c1 and c0 > 0:
            out[label] = round((c1 - c0) / c0 * 100.0, 3)
        else:
            out[label] = None
    return out


def _build_prompt(cfg: dict, con) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=14)
    news = fetch_news_rows_between(con, start, now, limit=400)
    lines: List[str] = []
    for r in news[:200]:
        t = r["title"][:120]
        c = (r["content"] or "")[:400]
        lines.append(f"- [{r['ts_utc']}] {r['source_name']}: {t}\n  {c}")

    mem = latest_memory(con)
    mem_txt = (mem["text"] if mem else "")[:4000]

    syms = top_mentioned_symbols(con, limit=30)
    px_lines: List[str] = []
    for sym, cnt in syms:
        ctx = _price_context(con, sym, now)
        px_lines.append(f"{sym} (mentions={cnt}): {ctx}")

    system = """You are a disciplined market research agent.
You must output ONLY valid JSON (no markdown fences) with this shape:
{
  "opportunities": [
    {
      "symbol": "TICKER_OR_PAIR",
      "duration": "short" | "mid" | "long",
      "forecast_pct": <number or null>,
      "forecast_usd": <number or null>,
      "confidence": <0..1>,
      "rationale": "<short>",
      "priced_in": "<why news may already be reflected in price, or not>"
    }
  ]
}
Rules:
- short = horizon under 7 days, mid = under 1 year, long = multi-year (up to ~7y narrative).
- Do not invent precise prices; use qualitative reasoning plus provided % moves when available.
- Prefer symbols that appear in the mention list; you may include 1–2 additional liquid tickers if strongly justified.
- If data is insufficient, return fewer items or empty opportunities array."""

    user = f"""Macro/micro memory (may be empty):
{mem_txt}

Recent news (sample):
{chr(10).join(lines)}

Mentioned symbols + recent return context (% over ~1d/5d/30d where data exists):
{chr(10).join(px_lines)}

Now produce JSON only."""

    return system, user


def run_research(cfg: dict) -> int:
    cfg = {**cfg}
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)

    system, user = _build_prompt(cfg, con)
    model = cfg.get("agent_research_model") or cfg.get("llm_model", "anthropic/claude-3.5-sonnet")

    client = _get_openrouter_client()
    if not client:
        logger.error("OPENROUTER_API_KEY not set; cannot run research.")
        con.close()
        return 0

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=4000,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error("Research LLM failed: %s", e)
        con.close()
        return 0

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Research output not JSON; first 500 chars: %s", raw[:500])
        con.close()
        return 0

    opps = data.get("opportunities") or []
    n = 0
    for o in opps:
        if not isinstance(o, dict):
            continue
        sym = str(o.get("symbol") or "").strip().upper()
        dur = str(o.get("duration") or "").strip().lower()
        if dur not in ("short", "mid", "long"):
            continue
        fc_pct = o.get("forecast_pct")
        fc_usd = o.get("forecast_usd")
        conf = o.get("confidence")
        rationale = str(o.get("rationale") or "")
        priced = str(o.get("priced_in") or "")
        meta = {"priced_in": priced, "raw": o}
        try:
            insert_recommendation(
                con,
                symbol=sym,
                duration=dur,
                forecast_usd=float(fc_usd) if fc_usd is not None else None,
                forecast_pct=float(fc_pct) if fc_pct is not None else None,
                confidence=float(conf) if conf is not None else None,
                rationale=rationale,
                meta=meta,
            )
            n += 1
        except Exception as e:
            logger.warning("Skip bad opportunity row: %s", e)

    logger.info("Research stored %s recommendations", n)
    con.close()
    return n
