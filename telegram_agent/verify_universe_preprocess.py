"""Verify universe preprocessor output in the SQLite DB.

Checks:
- Coverage: how many news rows in scope are processed vs pending.
- Integrity: symbol_news_linkage rows point to existing news_items.
- Universe membership: linkage symbols are within the configured symbol universe (priority-filtered).
- Consistency: each linkage (symbol, news_id) has a corresponding news_mentions row with mention_type='universe_preprocess'.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

from telegram_agent.agent_db import (
    connect,
    init_db,
    count_news_pending_universe_preprocess,
)
from telegram_agent.config import load_config
from telegram_agent.symbol_universe import load_symbol_universe, symbol_universe_set


def _utc_range_for_days(from_d: date, to_d: date) -> Tuple[datetime, datetime]:
    if to_d < from_d:
        raise ValueError("--to must be >= --from")
    start = datetime.combine(from_d, time.min, tzinfo=timezone.utc)
    end_excl = datetime.combine(to_d + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start, end_excl


def _count_news_in_scope(con, *, start: datetime, end_excl: datetime) -> int:
    cur = con.execute(
        "SELECT COUNT(*) AS c FROM news_items WHERE ts_utc >= ? AND ts_utc < ?",
        (start.isoformat(), end_excl.isoformat()),
    )
    r = cur.fetchone()
    return int(r["c"]) if r else 0


def _count_processed_in_scope(con, *, start: datetime, end_excl: datetime) -> int:
    cur = con.execute(
        """
        SELECT COUNT(*) AS c
        FROM news_items
        WHERE ts_utc >= ? AND ts_utc < ?
          AND universe_preprocess_ts_utc IS NOT NULL
        """,
        (start.isoformat(), end_excl.isoformat()),
    )
    r = cur.fetchone()
    return int(r["c"]) if r else 0


def _fetch_linkage_samples_by_source(
    con,
    *,
    start: datetime,
    end_excl: datetime,
    allowed: Set[str],
    per_source: int,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Return sample linked news rows grouped by source_type.
    Each sample includes id, ts_utc, source_name, title, and linked symbols (filtered to allowed).
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    if per_source <= 0:
        return out
    cur = con.execute(
        """
        SELECT DISTINCT n.source_type AS source_type
        FROM news_items n
        WHERE n.ts_utc >= ? AND n.ts_utc < ?
        ORDER BY n.source_type
        """,
        (start.isoformat(), end_excl.isoformat()),
    )
    source_types = [str(r["source_type"] or "") for r in cur.fetchall() if str(r["source_type"] or "").strip()]
    for st in source_types:
        cur2 = con.execute(
            """
            SELECT n.id, n.ts_utc, n.source_name, n.title
            FROM news_items n
            WHERE n.ts_utc >= ? AND n.ts_utc < ?
              AND n.source_type = ?
              AND EXISTS (SELECT 1 FROM symbol_news_linkage l WHERE l.news_id = n.id)
            ORDER BY n.ts_utc DESC
            LIMIT ?
            """,
            (start.isoformat(), end_excl.isoformat(), st, int(per_source)),
        )
        rows = cur2.fetchall()
        if not rows:
            continue
        samples: List[Dict[str, Any]] = []
        for r in rows:
            nid = str(r["id"])
            cur3 = con.execute(
                "SELECT symbol FROM symbol_news_linkage WHERE news_id = ? ORDER BY symbol",
                (nid,),
            )
            syms = [str(x["symbol"] or "").strip().upper() for x in cur3.fetchall()]
            syms = [s for s in syms if s and (s in allowed)]
            samples.append(
                {
                    "id": nid,
                    "ts_utc": str(r["ts_utc"]),
                    "source_name": str(r["source_name"] or ""),
                    "title": str(r["title"] or "")[:160],
                    "symbols": syms,
                }
            )
        out[st] = samples
    return out


def _top_linked_symbols(
    con,
    *,
    start: datetime,
    end_excl: datetime,
    allowed: Set[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """Counts of linked news per symbol (restricted to allowed universe), highest first."""
    if limit <= 0 or not allowed:
        return []
    # Count all linkage rows for news in scope. Restrict to allowed in Python to avoid temp tables here.
    cur = con.execute(
        """
        SELECT l.symbol AS symbol, COUNT(*) AS c
        FROM symbol_news_linkage l
        JOIN news_items n ON n.id = l.news_id
        WHERE n.ts_utc >= ? AND n.ts_utc < ?
        GROUP BY l.symbol
        ORDER BY c DESC
        LIMIT ?
        """,
        (start.isoformat(), end_excl.isoformat(), int(max(10, limit * 2))),
    )
    out: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        sym = str(r["symbol"] or "").strip().upper()
        if sym and sym in allowed:
            out.append({"symbol": sym, "news_count": int(r["c"] or 0)})
            if len(out) >= limit:
                break
    return out


def _unlinked_news_counts_by_day(
    con, *, start: datetime, end_excl: datetime, limit_days: int = 4000
) -> List[Dict[str, Any]]:
    """
    For each UTC day in [start, end_excl), count news_items that have zero symbol_news_linkage rows.
    This includes both:
    - processed items linked to [] (explicitly marked processed but no tickers), and
    - items not yet processed (universe_preprocess_ts_utc IS NULL).
    """
    cur = con.execute(
        """
        SELECT
          substr(n.ts_utc, 1, 10) AS day_utc,
          COUNT(*) AS c
        FROM news_items n
        WHERE n.ts_utc >= ? AND n.ts_utc < ?
          AND NOT EXISTS (SELECT 1 FROM symbol_news_linkage l WHERE l.news_id = n.id)
        GROUP BY substr(n.ts_utc, 1, 10)
        ORDER BY day_utc ASC
        LIMIT ?
        """,
        (start.isoformat(), end_excl.isoformat(), int(limit_days)),
    )
    return [{"day_utc": str(r["day_utc"]), "unlinked_news_count": int(r["c"] or 0)} for r in cur.fetchall()]


def verify_universe_preprocess(cfg: dict, *, day_from: date, day_to: date, max_errors: int = 50) -> Dict[str, Any]:
    cfg = dict(cfg)
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)

    allowed_syms = symbol_universe_set(cfg)
    if allowed_syms is None:
        con.close()
        return {"ok": False, "error": "symbol universe mode is not active (no allowlist configured)"}

    universe_list = load_symbol_universe(cfg) or sorted(allowed_syms)
    allowed: Set[str] = set(universe_list) & set(allowed_syms)

    start, end_excl = _utc_range_for_days(day_from, day_to)

    news_total = _count_news_in_scope(con, start=start, end_excl=end_excl)
    processed = _count_processed_in_scope(con, start=start, end_excl=end_excl)
    pending = count_news_pending_universe_preprocess(
        con,
        min_ts_utc_inclusive=start,
        max_ts_utc_exclusive=end_excl,
    )

    # Linkage integrity checks in the same scope (by news ts_utc)
    errs: List[str] = []

    # 1) Orphan linkage (should be prevented by FK, but verify)
    cur = con.execute(
        """
        SELECT l.symbol, l.news_id
        FROM symbol_news_linkage l
        LEFT JOIN news_items n ON n.id = l.news_id
        WHERE n.id IS NULL
        LIMIT ?
        """,
        (int(max_errors),),
    )
    for r in cur.fetchall():
        errs.append(f"orphan_linkage: symbol={r['symbol']} news_id={r['news_id']}")

    # 2) Linkage symbols outside allowed universe
    if len(errs) < max_errors:
        cur = con.execute(
            """
            SELECT DISTINCT l.symbol AS symbol
            FROM symbol_news_linkage l
            JOIN news_items n ON n.id = l.news_id
            WHERE n.ts_utc >= ? AND n.ts_utc < ?
            """,
            (start.isoformat(), end_excl.isoformat()),
        )
        for r in cur.fetchall():
            sym = str(r["symbol"] or "").strip().upper()
            if sym and sym not in allowed:
                errs.append(f"symbol_not_in_universe: {sym}")
                if len(errs) >= max_errors:
                    break

    # 3) Consistency: each linkage has a universe_preprocess mention row
    if len(errs) < max_errors:
        cur = con.execute(
            """
            SELECT l.symbol, l.news_id
            FROM symbol_news_linkage l
            JOIN news_items n ON n.id = l.news_id
            LEFT JOIN news_mentions m
              ON m.news_id = l.news_id AND m.symbol = l.symbol AND m.mention_type = 'universe_preprocess'
            WHERE n.ts_utc >= ? AND n.ts_utc < ?
              AND m.news_id IS NULL
            LIMIT ?
            """,
            (start.isoformat(), end_excl.isoformat(), int(max_errors - len(errs))),
        )
        for r in cur.fetchall():
            errs.append(f"missing_news_mentions_row: symbol={r['symbol']} news_id={r['news_id']}")

    # Counts for context
    cur = con.execute(
        """
        SELECT COUNT(*) AS c
        FROM symbol_news_linkage l
        JOIN news_items n ON n.id = l.news_id
        WHERE n.ts_utc >= ? AND n.ts_utc < ?
        """,
        (start.isoformat(), end_excl.isoformat()),
    )
    linkage_rows_in_scope = int(cur.fetchone()["c"])

    linkage_samples = _fetch_linkage_samples_by_source(
        con,
        start=start,
        end_excl=end_excl,
        allowed=allowed,
        per_source=int(cfg.get("verify_linkage_samples_per_source", 3)),
    )
    top_symbols = _top_linked_symbols(
        con,
        start=start,
        end_excl=end_excl,
        allowed=allowed,
        limit=int(cfg.get("verify_top_symbols_limit", 50)),
    )
    unlinked_by_day = _unlinked_news_counts_by_day(con, start=start, end_excl=end_excl)

    con.close()

    ok = (pending == 0) and (len(errs) == 0)
    return {
        "ok": ok,
        "scope": {"from_day_utc": day_from.isoformat(), "to_day_utc": day_to.isoformat()},
        "universe_size": len(allowed),
        "news_rows_in_scope": news_total,
        "processed_news_rows_in_scope": processed,
        "pending_news_rows_in_scope": pending,
        "linkage_rows_in_scope": linkage_rows_in_scope,
        "linkage_samples_by_source_type": linkage_samples,
        "top_universe_symbols_by_linked_news": top_symbols,
        "unlinked_news_counts_by_day_utc": unlinked_by_day,
        "errors": errs,
        "notes": [
            "pending_news_rows_in_scope counts news_items where universe_preprocess_ts_utc IS NULL in the scope.",
            "ok requires zero pending rows and no linkage integrity errors in scope.",
        ],
    }


def main() -> None:
    # Mirror agent.py: load repo-root .env then telegram_agent/.env (latter overrides).
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")

    p = argparse.ArgumentParser(description="Verify universe preprocessor output in agent DB")
    p.add_argument("--from", dest="day_from", required=True, help="UTC day start YYYY-MM-DD (inclusive)")
    p.add_argument("--to", dest="day_to", required=False, help="UTC day end YYYY-MM-DD (inclusive); default=--from")
    p.add_argument("--max-priority", type=int, default=None, help="Universe membership priority <= this (0..3)")
    p.add_argument("--max-errors", type=int, default=50, help="Max errors to print/return")
    p.add_argument(
        "--samples-per-source",
        type=int,
        default=3,
        help="How many linked news samples to print per source_type (api/rss/telegram).",
    )
    p.add_argument(
        "--top-symbols",
        type=int,
        default=50,
        help="Show top universe tickers by number of linked news items in scope (0 to disable).",
    )
    p.add_argument(
        "--show-unlinked-by-day",
        action="store_true",
        help="Print counts of news items per UTC day that are not linked to any ticker.",
    )

    args = p.parse_args()
    cfg = load_config()
    if args.max_priority is not None:
        cfg["max_priority"] = int(args.max_priority)

    d0 = date.fromisoformat(args.day_from)
    d1 = date.fromisoformat(args.day_to) if args.day_to else d0

    rep = verify_universe_preprocess(cfg, day_from=d0, day_to=d1, max_errors=int(args.max_errors))
    # Print human-friendly summary
    print(f"ok={rep.get('ok')}")
    if rep.get("error"):
        print(f"error={rep['error']}")
        sys.exit(2)
    print(
        "scope=%s..%s universe=%s news=%s processed=%s pending=%s linkage_rows=%s"
        % (
            rep["scope"]["from_day_utc"],
            rep["scope"]["to_day_utc"],
            rep["universe_size"],
            rep["news_rows_in_scope"],
            rep["processed_news_rows_in_scope"],
            rep["pending_news_rows_in_scope"],
            rep["linkage_rows_in_scope"],
        )
    )
    errs = rep.get("errors") or []
    if errs:
        print("errors:")
        for e in errs:
            print(f"- {e}")
        sys.exit(1)

    # Samples (printed after passing integrity checks)
    samples = rep.get("linkage_samples_by_source_type") or {}
    if args.samples_per_source and samples:
        print("\nlinkage_samples_by_source_type:")
        for st, items in samples.items():
            print(f"- source_type={st} samples={len(items)}")
            for it in items[: int(args.samples_per_source)]:
                sy = ", ".join(it.get("symbols") or [])
                print(f"  - [{it.get('ts_utc')}] {it.get('source_name')}: {it.get('title')}  |  symbols=[{sy}]  |  id={it.get('id')}")

    top_syms = rep.get("top_universe_symbols_by_linked_news") or []
    if args.top_symbols and top_syms:
        print("\ntop_universe_symbols_by_linked_news:")
        for row in top_syms[: int(args.top_symbols)]:
            print(f"- {row.get('symbol')}: {row.get('news_count')}")

    if args.show_unlinked_by_day:
        rows = rep.get("unlinked_news_counts_by_day_utc") or []
        print("\nunlinked_news_counts_by_day_utc:")
        for r in rows:
            print(f"- {r.get('day_utc')}: {r.get('unlinked_news_count')}")


if __name__ == "__main__":
    main()

