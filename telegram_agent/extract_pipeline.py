"""Scan news_items and populate news_mentions + instruments (LLM + optional regex fallback)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Sequence

from telegram_agent.agent_db import connect, init_db, add_mentions
from telegram_agent.cost_estimate import estimate_micro_batch_cost
from telegram_agent.extract_llm import (
    EXTRACT_SYSTEM,
    extract_symbols_llm_batch,
    llm_extract_available,
    openrouter_extract_user_content,
)
from telegram_agent.symbol_universe import symbol_universe_set
from telegram_agent.extract_tickers import extract_symbols_from_text

logger = logging.getLogger(__name__)


def _fetch_rows_pending_extract(con: Any, limit: int) -> List[Any]:
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
    return list(cur.fetchall())


def estimate_extract_llm_cost(cfg: dict, *, limit: int = 2000) -> Dict[str, Any]:
    """
    Token + USD estimate for OpenRouter-style extract calls (no API requests).
    Assumes chat layout: system = EXTRACT_SYSTEM, user = openrouter_extract_user_content(...).
    """
    cfg = {**cfg}
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    rows = _fetch_rows_pending_extract(con, limit)
    con.close()

    allowed_syms = symbol_universe_set(cfg)
    if allowed_syms is not None:
        use_llm = False
    else:
        use_llm = bool(cfg.get("extract_use_llm", True)) and llm_extract_available(cfg)
    if not use_llm:
        return {
            "use_llm": False,
            "pending_news_rows": len(rows),
            "batches": 0,
            "model": None,
            "input_tokens": 0,
            "output_tokens_est": 0,
            "total_usd": 0.0,
            "per_batch": [],
            "note": "LLM extract disabled or no API keys; extract would use regex only (no LLM cost).",
        }

    batch_size = max(1, int(cfg.get("extract_llm_batch_size", 12)))
    model = (cfg.get("extract_llm_model") or "anthropic/claude-3-haiku").strip()
    max_chars = int(cfg.get("extract_max_chars_per_item", 2200))

    per_batch: List[Dict[str, Any]] = []
    total_in = 0
    total_out_est = 0
    total_usd = 0.0

    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        user_content = openrouter_extract_user_content(batch, max_chars)
        max_out = min(8192, 120 * len(batch) + 400)
        est = estimate_micro_batch_cost(
            user_content,
            EXTRACT_SYSTEM,
            model,
            batch_output_tokens_est=max_out,
        )
        per_batch.append(
            {
                "batch_index": len(per_batch),
                "items": len(batch),
                "input_tokens": est["input_tokens"],
                "output_tokens_est": est["output_tokens_est"],
                "total_usd": est["total_usd"],
            }
        )
        total_in += est["input_tokens"]
        total_out_est += est["output_tokens_est"]
        total_usd += est["total_usd"]

    return {
        "use_llm": True,
        "pending_news_rows": len(rows),
        "batch_size": batch_size,
        "batches": len(per_batch),
        "model": model,
        "input_tokens": total_in,
        "output_tokens_est": total_out_est,
        "total_usd": total_usd,
        "per_batch": per_batch,
        "note": "USD uses cost_estimate.py pricing; set ESTIMATE_* env vars to match your provider.",
    }


def run_extract(cfg: dict, *, limit: int = 2000) -> int:
    cfg = {**cfg}
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    rows: List[Any] = _fetch_rows_pending_extract(con, limit)

    allowed_syms = symbol_universe_set(cfg)
    if allowed_syms is not None:
        # Fixed universe mode: regex-only extraction constrained to allowed symbols.
        use_llm = False
    else:
        use_llm = bool(cfg.get("extract_use_llm", True)) and llm_extract_available(cfg)
    if cfg.get("extract_use_llm", True) and not llm_extract_available(cfg):
        logger.warning(
            "EXTRACT_USE_LLM is true but no OPENROUTER_API_KEY / GEMINI_API_KEY; using regex only."
        )
        use_llm = False

    batch_size = max(1, int(cfg.get("extract_llm_batch_size", 12)))
    fallback = bool(cfg.get("extract_regex_fallback", True))

    total = 0
    for i in range(0, len(rows), batch_size):
        batch: Sequence[Any] = rows[i : i + batch_size]
        if use_llm:
            mmap = extract_symbols_llm_batch(cfg, batch)
            for row in batch:
                nid = row["id"]
                mentions = list(mmap.get(str(nid), []))
                if not mentions and fallback:
                    text = f"{row['title']}\n{row['content']}"
                    mentions = extract_symbols_from_text(text, allowed_symbols=allowed_syms)
                if mentions:
                    add_mentions(con, nid, mentions)
                    total += len(mentions)
        else:
            for row in batch:
                nid = row["id"]
                text = f"{row['title']}\n{row['content']}"
                mentions = extract_symbols_from_text(text, allowed_symbols=allowed_syms)
                if mentions:
                    add_mentions(con, nid, mentions)
                    total += len(mentions)

    con.close()
    mode = "llm" + ("+regex_fallback" if use_llm and fallback else "") if use_llm else "regex"
    logger.info(
        "Extracted mentions for %s news rows (%s mention rows, mode=%s)",
        len(rows),
        total,
        mode,
    )
    return total
