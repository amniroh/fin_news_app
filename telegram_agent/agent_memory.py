"""Roll macro/micro memory from recent news + prior memory."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram_agent.agent_db import connect, init_db, fetch_news_rows_between, latest_memory, upsert_memory
from telegram_agent.summarizer import _get_openrouter_client

logger = logging.getLogger(__name__)


def run_memory_update(cfg: dict) -> int:
    cfg = {**cfg}
    months = int(cfg.get("agent_memory_months", 6))
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=30 * months)

    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)

    prev = latest_memory(con)
    prev_txt = (prev["text"] if prev else "")[:6000]

    news = fetch_news_rows_between(con, start, now, limit=800)
    chunks: list[str] = []
    for r in news[:400]:
        chunks.append(f"[{r['ts_utc']}] {r['source_name']}: {r['title'][:160]}")

    system = """You maintain a compact running memory of macro and micro themes for an investing agent.
Output plain text (no JSON), bullet points with • , max ~1200 words.
Focus on durable trends, regime shifts, and recurring narratives — not one-off headlines.
Merge with prior memory without duplicating; update what changed."""

    user = f"""Prior memory (may be empty):
{prev_txt}

Headlines sample from last {months} months ({len(chunks)} lines):
{chr(10).join(chunks[:350])}

Write updated memory."""

    client = _get_openrouter_client()
    if not client:
        logger.error("OPENROUTER_API_KEY not set; memory update skipped.")
        con.close()
        return 0

    model = cfg.get("llm_model", "anthropic/claude-3.5-sonnet")
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=2500,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error("Memory LLM failed: %s", e)
        con.close()
        return 0

    mid = upsert_memory(con, horizon_months=months, text=text, meta={"source": "llm"})
    logger.info("Memory snapshot id=%s", mid)
    con.close()
    return mid
