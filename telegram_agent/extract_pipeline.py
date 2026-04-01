"""Scan news_items and populate news_mentions + instruments."""
from __future__ import annotations

import logging
from pathlib import Path

from telegram_agent.agent_db import connect, init_db, add_mentions
from telegram_agent.extract_tickers import extract_symbols_from_text

logger = logging.getLogger(__name__)


def run_extract(cfg: dict, *, limit: int = 2000) -> int:
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    cur = con.execute(
        """
        SELECT n.id, n.title, n.content
        FROM news_items n
        LEFT JOIN news_mentions m ON m.news_id = n.id
        WHERE m.news_id IS NULL
        ORDER BY n.ts_utc DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    total = 0
    for row in rows:
        nid = row["id"]
        text = f"{row['title']}\n{row['content']}"
        mentions = extract_symbols_from_text(text)
        if not mentions:
            continue
        add_mentions(con, nid, mentions)
        total += len(mentions)
    con.close()
    logger.info("Extracted mentions for %s news rows (%s mention rows)", len(rows), total)
    return total
