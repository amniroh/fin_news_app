"""Roll macro/micro memory from recent news + prior memory."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram_agent.agent_db import connect, init_db, fetch_news_rows_between, latest_memory, upsert_memory
from telegram_agent.config import DEFAULT_LLM_MODEL
from telegram_agent.summarizer import _get_openrouter_client
from telegram_agent.symbol_universe import symbol_universe_set

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

    allowed_syms = symbol_universe_set(cfg)
    # Universe mode: limit memory inputs to news items that already have extracted
    # mentions in the fixed universe (news_mentions is universe-filtered by extract).
    if allowed_syms is not None and cfg.get("memory_use_universe_news_only", True):
        cur = con.execute(
            """
            SELECT DISTINCT
              n.id, n.source_type, n.source_name, n.title, n.content, n.ts_utc, n.condensed
            FROM news_items n
            JOIN news_mentions m ON m.news_id = n.id
            WHERE n.ts_utc >= ? AND n.ts_utc <= ?
            ORDER BY n.ts_utc DESC
            LIMIT ?
            """,
            (start.astimezone(timezone.utc).isoformat(), now.astimezone(timezone.utc).isoformat(), 800),
        )
        news = list(cur.fetchall())
        if not news:
            news = fetch_news_rows_between(con, start, now, limit=800)
    else:
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

    model = cfg.get("llm_model", DEFAULT_LLM_MODEL)
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
