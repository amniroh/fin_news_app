"""Cheap LLM pass: tag each news row with tickers from the configured universe only."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from telegram_agent.agent_db import (
    count_news_pending_universe_preprocess,
    fetch_news_pending_universe_preprocess,
    replace_universe_preprocess_for_news,
)
from telegram_agent.cost_estimate import estimate_micro_batch_cost
from telegram_agent.symbol_universe import load_symbol_universe, symbol_universe_set

logger = logging.getLogger(__name__)

PREPROCESS_SYSTEM = """You map each news item to tickers from the ALLOWED_UNIVERSE list only.

The user JSON has an "items" array: position **0** is the first story, position **1** the second, and so on. **Items do not include database ids** — you must use **index position only**.
The user JSON also provides **n_items**. Your output outer array length MUST be exactly **n_items**.
If a provider already included a ticker in its internal id, the system may pre-link it without asking you.

Return ONLY valid JSON (no markdown fences): **one** outer JSON array.
- Its length must be **exactly** the same as the input "items" array (same number of elements).
- **items[i]** in the input corresponds **only** to **output[i]** in your array (same order; do not reorder, insert, or drop elements).
- Each element of your outer array is a JSON array of strings: tickers from ALLOWED_UNIVERSE for that same-index item (company, asset, or explicit ticker).

Rules:
- Use **exact** ticker strings as they appear in ALLOWED_UNIVERSE (same spelling/case as given).
- If nothing in that item refers to a universe ticker, use [] for that position.
- At most 12 tickers per item; prefer the most central to the story.
- Do not output symbols not in ALLOWED_UNIVERSE."""

PREPROCESS_TEMPERATURE = 0.1


def utc_range_for_backfill_days(from_d: date, to_d: date) -> Tuple[datetime, datetime]:
    """Inclusive UTC calendar range [from_d, to_d] → (start_utc, end_exclusive_utc)."""
    if to_d < from_d:
        raise ValueError("backfill-to must be >= backfill-from")
    start = datetime.combine(from_d, time.min, tzinfo=timezone.utc)
    end_excl = datetime.combine(to_d + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start, end_excl


_ID_TICKER_SOURCES = {"finnhub", "alphavantage", "stocknewsapi"}


def _ticker_from_news_id(nid: str) -> Optional[str]:
    """
    Some providers already embed the ticker in news_items.id, e.g. finnhub:CMG:118199294.
    Returns the embedded ticker if the id matches a known provider pattern; else None.
    """
    if not nid or ":" not in nid:
        return None
    parts = nid.split(":", 2)
    if len(parts) < 3:
        return None
    src = (parts[0] or "").strip().lower()
    if src not in _ID_TICKER_SOURCES:
        return None
    sym = (parts[1] or "").strip().upper().replace(" ", "")
    if not sym:
        return None
    if len(sym) > 20:
        return None
    return sym


def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    t = text.strip()
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1].rsplit(" ", 1)[0] + "…"


def _rows_to_preprocess_payload(
    rows: Sequence[Any], max_chars: int
) -> List[Dict[str, str]]:
    """
    LLM-facing items: title + text only (no news id). Caller maps output[i] → rows[i]['id'].
    """
    out: List[Dict[str, str]] = []
    for row in rows:
        title = row["title"] or ""
        body = row["content"] or ""
        text = _truncate(f"{title}\n{body}", max_chars)
        out.append({"title": str(title)[:300], "text": text})
    return out


def build_preprocess_user_content(cfg: dict, allowed: Set[str], rows: Sequence[Any]) -> str:
    max_chars = int(cfg.get("extract_max_chars_per_item", 2200))
    items = _rows_to_preprocess_payload(rows, max_chars)
    # n_items is provided to reduce model miscounts; output must match this length.
    user_obj: Dict[str, Any] = {
        "n_items": len(items),
        "allowed_universe": sorted(allowed),
        "items": items,
    }
    return json.dumps(user_obj, ensure_ascii=False)


def _parse_outer_array(raw: str) -> List[Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("expected JSON array")
    return data


def _call_openrouter(
    user_obj: Dict[str, Any], model: str, max_out: int
) -> str:
    from openai import OpenAI

    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    client = OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1")
    user = json.dumps(user_obj, ensure_ascii=False)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": PREPROCESS_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=PREPROCESS_TEMPERATURE,
        max_tokens=max_out,
    )
    return (resp.choices[0].message.content or "").strip()


def _call_gemini(user_obj: Dict[str, Any], model: str, max_out: int) -> str:
    import google.generativeai as genai

    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    genai.configure(api_key=key)
    gen = genai.GenerativeModel(model)
    user = json.dumps(user_obj, ensure_ascii=False)
    full = f"{PREPROCESS_SYSTEM}\n\n{user}"
    resp = gen.generate_content(
        full,
        generation_config={"max_output_tokens": max_out, "temperature": PREPROCESS_TEMPERATURE},
    )
    return (resp.text or "").strip()


def preprocess_llm_available() -> bool:
    if os.getenv("OPENROUTER_API_KEY", "").strip():
        return True
    if os.getenv("GEMINI_API_KEY", "").strip():
        return True
    return False


def _resolve_preprocess_model(cfg: dict) -> Tuple[str, str]:
    """Returns (model_id, provider_label openrouter|gemini)."""
    if os.getenv("OPENROUTER_API_KEY", "").strip():
        m = (cfg.get("news_universe_preprocess_model") or "").strip() or (
            cfg.get("micro_model_openrouter", "anthropic/claude-3-haiku")
        )
        return m, "openrouter"
    if os.getenv("GEMINI_API_KEY", "").strip():
        m = (cfg.get("news_universe_preprocess_model") or "").strip() or cfg.get(
            "micro_model_gemini", "gemini-2.0-flash"
        )
        return m, "gemini"
    return "", "none"


def _max_out_tokens_for_batch(n_items: int) -> int:
    return min(8192, 80 * n_items + 400)


def estimate_universe_preprocess_dry_run(
    cfg: dict,
    con,
    *,
    min_ts_utc_inclusive: Optional[datetime] = None,
    max_ts_utc_exclusive: Optional[datetime] = None,
    max_ts_utc_inclusive: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Cost/token estimate + sample prompt (first batch only). No API calls.
    """
    cfg = {**cfg}
    allowed_syms = symbol_universe_set(cfg)
    if allowed_syms is None:
        return {
            "skipped": True,
            "reason": "no_universe",
            "llm_calls_est": 0,
            "model": None,
            "provider": None,
            "pending_in_scope": 0,
            "note": "Symbol universe mode is not active.",
        }

    try:
        universe_list = load_symbol_universe(cfg) or sorted(allowed_syms)
    except Exception as e:
        return {
            "skipped": True,
            "reason": "universe_load_error",
            "error": str(e),
            "llm_calls_est": 0,
            "model": None,
            "provider": None,
            "pending_in_scope": 0,
        }

    allowed: Set[str] = set(universe_list) & allowed_syms
    if not allowed:
        return {
            "skipped": True,
            "reason": "empty_universe",
            "llm_calls_est": 0,
            "model": None,
            "provider": None,
            "pending_in_scope": 0,
        }

    if not preprocess_llm_available():
        return {
            "skipped": True,
            "reason": "no_llm",
            "llm_calls_est": 0,
            "model": None,
            "provider": None,
            "pending_in_scope": 0,
            "note": "Set OPENROUTER_API_KEY or GEMINI_API_KEY.",
        }

    pending = count_news_pending_universe_preprocess(
        con,
        min_ts_utc_inclusive=min_ts_utc_inclusive,
        max_ts_utc_exclusive=max_ts_utc_exclusive,
        max_ts_utc_inclusive=max_ts_utc_inclusive,
    )

    # Estimate how many pending rows could be satisfied without LLM by parsing provider ids.
    # Exact up to a cap (to keep dry-run fast on huge DBs).
    id_fastpath_cap = 200_000
    id_fastpath_checked = 0
    id_fastpath_hits = 0
    if pending:
        where_bits: List[str] = ["universe_preprocess_ts_utc IS NULL"]
        params: List[Any] = []
        if min_ts_utc_inclusive is not None:
            where_bits.append("ts_utc >= ?")
            params.append(min_ts_utc_inclusive.astimezone(timezone.utc).isoformat())
        if max_ts_utc_exclusive is not None:
            where_bits.append("ts_utc < ?")
            params.append(max_ts_utc_exclusive.astimezone(timezone.utc).isoformat())
        elif max_ts_utc_inclusive is not None:
            where_bits.append("ts_utc <= ?")
            params.append(max_ts_utc_inclusive.astimezone(timezone.utc).isoformat())
        where_bits.append(
            "("
            + " OR ".join([f"id LIKE '{s}:%:%'" for s in sorted(_ID_TICKER_SOURCES)])
            + ")"
        )
        where_sql = " AND ".join(where_bits)
        cur = con.execute(
            f"SELECT id FROM news_items WHERE {where_sql} ORDER BY ts_utc ASC LIMIT ?",
            (*params, int(id_fastpath_cap)),
        )
        rows = cur.fetchall()
        id_fastpath_checked = len(rows)
        for r in rows:
            sym = _ticker_from_news_id(str(r["id"] or ""))
            if sym and sym in allowed:
                id_fastpath_hits += 1

    pending_llm_est = max(0, pending - id_fastpath_hits)

    batch_size = max(1, int(cfg.get("news_universe_preprocess_batch_size", 16)))
    model, provider = _resolve_preprocess_model(cfg)
    if not model:
        return {
            "skipped": True,
            "reason": "no_llm",
            "pending_in_scope": pending,
            "llm_calls_est": 0,
            "model": None,
            "provider": None,
        }

    batches = (pending_llm_est + batch_size - 1) // batch_size if pending_llm_est else 0
    sample_rows = fetch_news_pending_universe_preprocess(
        con,
        limit=batch_size,
        min_ts_utc_inclusive=min_ts_utc_inclusive,
        max_ts_utc_exclusive=max_ts_utc_exclusive,
        max_ts_utc_inclusive=max_ts_utc_inclusive,
    )
    user_sample = build_preprocess_user_content(cfg, allowed, sample_rows) if sample_rows else ""
    max_out_sample = _max_out_tokens_for_batch(len(sample_rows) if sample_rows else batch_size)
    est_one = estimate_micro_batch_cost(
        user_sample if user_sample else "{}",
        PREPROCESS_SYSTEM,
        model,
        max_out_sample,
    )

    input_tokens_total_est = est_one["input_tokens"] * batches if batches else 0
    output_tokens_est_total = est_one["output_tokens_est"] * batches if batches else 0
    total_usd_typical = est_one["total_usd"] * batches if batches else 0.0

    scope_bits: List[str] = []
    if min_ts_utc_inclusive is not None:
        scope_bits.append(f"from_ts>={min_ts_utc_inclusive.isoformat()}")
    if max_ts_utc_exclusive is not None:
        scope_bits.append(f"to_ts<{max_ts_utc_exclusive.isoformat()}")
    if max_ts_utc_inclusive is not None and max_ts_utc_exclusive is None:
        scope_bits.append(f"ts<={max_ts_utc_inclusive.isoformat()}")
    scope_desc = "; ".join(scope_bits) if scope_bits else "all pending (no extra ts filter)"

    system_chars = len(PREPROCESS_SYSTEM)
    user_chars = len(user_sample)
    return {
        "skipped": pending == 0,
        "reason": "no_pending_news" if pending == 0 else None,
        "pending_in_scope": pending,
        "pending_fastpath_id_checked": id_fastpath_checked,
        "pending_fastpath_id_hits_in_universe": id_fastpath_hits,
        "pending_llm_est": pending_llm_est,
        "batch_size": batch_size,
        "batches_est": batches,
        "llm_calls_est": batches,
        "model": model,
        "provider": provider,
        "temperature": PREPROCESS_TEMPERATURE,
        "max_output_tokens_per_batch": max_out_sample,
        "scope_filter": scope_desc,
        "allowed_universe_size": len(allowed),
        "input_tokens_per_batch_est": est_one["input_tokens"],
        "input_tokens_total_est": input_tokens_total_est,
        "output_tokens_est_per_batch": est_one["output_tokens_est"],
        "output_tokens_est_total": output_tokens_est_total,
        "total_usd_typical": total_usd_typical,
        "sample_batch_items": len(sample_rows),
        "system_prompt": PREPROCESS_SYSTEM,
        "user_prompt_sample": user_sample,
        "system_chars": system_chars,
        "user_chars": user_chars,
        "total_chars": system_chars + user_chars,
        "note": "USD uses cost_estimate.py pricing; install tiktoken for better token counts. "
        "Total cost assumes uniform batch sizes (approximation).",
    }


def write_universe_preprocess_dry_run_file(rep: Dict[str, Any], path: Path) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    st = {k: rep.get(k) for k in ("pending_in_scope", "batches_est", "allowed_universe_size", "scope_filter")}
    header = (
        f"# Universe preprocess dry-run (no API calls)\n"
        f"# model={rep.get('model')} provider={rep.get('provider')} temperature={rep.get('temperature')}\n"
        f"# llm_calls_est={rep.get('llm_calls_est')} max_output_tokens_per_batch={rep.get('max_output_tokens_per_batch')}\n"
        f"# input_tokens_total_est={rep.get('input_tokens_total_est')} total_usd_typical={rep.get('total_usd_typical', 0):.6f}\n"
        f"# pending={st.get('pending_in_scope')} batches_est={st.get('batches_est')} universe_size={st.get('allowed_universe_size')}\n"
        f"# scope: {st.get('scope_filter')}\n"
        f"# prompt_chars_sample: system={rep.get('system_chars')} user={rep.get('user_chars')} total={rep.get('total_chars')}\n"
        f"\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("=== SYSTEM PROMPT ===\n\n")
        f.write(str(rep.get("system_prompt") or ""))
        f.write("\n\n=== USER PROMPT (first batch sample) ===\n\n")
        f.write(str(rep.get("user_prompt_sample") or "(no pending rows — empty sample)"))
        if not str(rep.get("user_prompt_sample") or "").endswith("\n"):
            f.write("\n")


def _run_preprocess_batch(
    cfg: dict,
    allowed: Set[str],
    rows: Sequence[Any],
) -> Dict[str, List[str]]:
    """Returns news_id -> list of symbols (filtered to allowed)."""
    if not rows:
        return {}
    max_chars = int(cfg.get("extract_max_chars_per_item", 2200))
    items = _rows_to_preprocess_payload(rows, max_chars)
    model, prov = _resolve_preprocess_model(cfg)
    user_obj: Dict[str, Any] = {
        "n_items": len(items),
        "allowed_universe": sorted(allowed),
        "items": items,
    }
    max_out = _max_out_tokens_for_batch(len(items))

    raw = ""
    try:
        if prov == "openrouter":
            raw = _call_openrouter(user_obj, model, max_out)
        elif prov == "gemini":
            raw = _call_gemini(user_obj, model, max_out)
        else:
            raise RuntimeError("No OPENROUTER_API_KEY or GEMINI_API_KEY for preprocess")
    except Exception as e:
        logger.error("Universe preprocess LLM batch failed: %s", e)
        return {}

    try:
        parsed = _parse_outer_array(raw)
    except Exception as e:
        logger.warning(
            "Universe preprocess JSON parse failed: %s | first 400 chars: %s",
            e,
            raw[:400],
        )
        return {}

    n = len(rows)
    if len(parsed) != n:
        # Models sometimes miscount and produce extra / fewer elements. We normalize to preserve
        # positional mapping (output[i] -> rows[i].id) rather than failing the whole batch.
        logger.warning(
            "Universe preprocess: output length mismatch (expected %s, got %s); normalizing (truncate/pad).",
            n,
            len(parsed),
        )
    if len(parsed) > n:
        parsed = parsed[:n]
    elif len(parsed) < n:
        parsed = list(parsed) + ([[]] * (n - len(parsed)))

    out: Dict[str, List[str]] = {}
    expected_ids = [str(r["id"]) for r in rows]
    for i, eid in enumerate(expected_ids):
        cell = parsed[i]
        if not isinstance(cell, list):
            logger.warning(
                "Universe preprocess: output[%s] is not a JSON array (got %s); treating as [].",
                i,
                type(cell).__name__,
            )
            cell = []
        chunk: List[str] = []
        for x in cell[:12]:
            if isinstance(x, str):
                s = x.strip().upper().replace(" ", "")
                if s in allowed:
                    chunk.append(s)
        out[eid] = sorted(set(chunk))
    return out


def run_news_universe_preprocess(
    cfg: dict,
    con,
    *,
    limit: Optional[int] = None,
    max_ts_utc_inclusive: Optional[datetime] = None,
    min_ts_utc_inclusive: Optional[datetime] = None,
    max_ts_utc_exclusive: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Process pending news rows (universe_preprocess_ts_utc IS NULL) using the cheap LLM.
    Skips entirely when there are no matching rows (no API calls).
    """
    allowed_syms = symbol_universe_set(cfg)
    if allowed_syms is None:
        logger.info("Universe preprocess skipped: symbol universe mode not active.")
        return {"skipped": True, "reason": "no_universe", "processed": 0, "batches": 0, "pending_in_scope": 0}

    try:
        universe_list = load_symbol_universe(cfg) or sorted(allowed_syms)
    except Exception as e:
        logger.warning("Universe preprocess: could not load universe list: %s", e)
        universe_list = sorted(allowed_syms)

    allowed: Set[str] = set(universe_list) & allowed_syms
    if not allowed:
        return {"skipped": True, "reason": "empty_universe", "processed": 0, "batches": 0, "pending_in_scope": 0}

    if not preprocess_llm_available():
        logger.warning("Universe preprocess skipped: no LLM API key.")
        return {"skipped": True, "reason": "no_llm", "processed": 0, "batches": 0, "pending_in_scope": 0}

    n_pending = count_news_pending_universe_preprocess(
        con,
        min_ts_utc_inclusive=min_ts_utc_inclusive,
        max_ts_utc_exclusive=max_ts_utc_exclusive,
        max_ts_utc_inclusive=max_ts_utc_inclusive,
    )
    if n_pending == 0:
        logger.info("Universe preprocess skipped: no pending news in scope.")
        return {
            "skipped": True,
            "reason": "no_pending_news",
            "processed": 0,
            "batches": 0,
            "pending_in_scope": 0,
        }

    max_rows = limit if limit is not None else int(cfg.get("news_universe_preprocess_max_rows_per_run", 100_000))
    max_rows = max(0, min(2_000_000, max_rows))
    batch_size = max(1, min(64, int(cfg.get("news_universe_preprocess_batch_size", 16))))

    total_processed = 0
    batches = 0
    now = datetime.now(timezone.utc)

    while total_processed < max_rows:
        pending = fetch_news_pending_universe_preprocess(
            con,
            limit=min(batch_size, max_rows - total_processed),
            min_ts_utc_inclusive=min_ts_utc_inclusive,
            max_ts_utc_exclusive=max_ts_utc_exclusive,
            max_ts_utc_inclusive=max_ts_utc_inclusive,
        )
        if not pending:
            break

        # Fast-path: if provider id already embeds a ticker, skip LLM entirely.
        llm_rows: List[Any] = []
        for row in pending:
            nid = str(row["id"])
            sym = _ticker_from_news_id(nid)
            if sym:
                if sym in allowed:
                    replace_universe_preprocess_for_news(con, nid, [sym], linked_ts_utc=now)
                else:
                    # Not in universe: mark as processed with empty link so we never spend LLM on it.
                    replace_universe_preprocess_for_news(con, nid, [], linked_ts_utc=now)
                total_processed += 1
            else:
                llm_rows.append(row)

        if not llm_rows:
            continue

        batches += 1
        mmap = _run_preprocess_batch(cfg, allowed, llm_rows)
        if not mmap or len(mmap) != len(llm_rows):
            logger.error(
                "Universe preprocess: batch mapping incomplete (%s vs %s rows); stopping without further updates.",
                len(mmap),
                len(llm_rows),
            )
            break
        for row in llm_rows:
            nid = str(row["id"])
            syms = mmap.get(nid, [])
            replace_universe_preprocess_for_news(con, nid, syms, linked_ts_utc=now)
            total_processed += 1

    return {
        "skipped": False,
        "processed": total_processed,
        "batches": batches,
        "allowed_universe_size": len(allowed),
        "pending_in_scope_before_run": n_pending,
    }
