"""
A/B research arms: run research/memory for an extra model in its own agent DB.

Each arm DB keeps its own memories, recommendations, tester state and research logs, while news,
universe linkage and prices are copied in from the main agent DB (filled by the daily orchestrator).

  python -m telegram_agent.research_variant seed --db telegram_agent/data/agent_b.sqlite
  python -m telegram_agent.research_variant run --db telegram_agent/data/agent_b.sqlite \
      --model google/gemini-3.8-flash --label B
  python -m telegram_agent.research_variant backfill --db /tmp/bf_b.sqlite --model google/gemini-3.8-flash \
      --label B --from 2026-10-27 --to 2026-10-30
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("telegram_agent.research_variant")

_OWN_TABLES = ("memories", "recommendations", "research_internal_logs")


def _parse_utc(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _common_columns(con, table: str) -> List[str]:
    dst = [r[1] for r in con.execute(f"PRAGMA main.table_info({table})")]
    src = {r[1] for r in con.execute(f"PRAGMA src.table_info({table})")}
    return [c for c in dst if c in src]


def _copy(con, table: str, where: str, params: tuple, *, upsert: bool = True) -> int:
    """Copy rows from src; upsert updates existing rows in place (REPLACE would cascade-delete children)."""
    cols = _common_columns(con, table)
    if not cols:
        return 0
    col_s = ", ".join(cols)
    sql = f"INSERT INTO main.{table} ({col_s}) SELECT {col_s} FROM src.{table} WHERE {where}"
    if upsert:
        info = list(con.execute(f"PRAGMA main.table_info({table})"))
        pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        rest = [c for c in cols if c not in pk]
        if pk and rest:
            sql += f" ON CONFLICT({', '.join(pk)}) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in rest)
        elif pk:
            sql += f" ON CONFLICT({', '.join(pk)}) DO NOTHING"
    cur = con.execute(sql, params)
    return cur.rowcount or 0


def _open_arm(db: Path, main_db: Path):
    from telegram_agent.agent_db import connect, init_db

    if db.resolve() == main_db.resolve():
        raise SystemExit("arm DB must differ from the main agent DB")
    db.parent.mkdir(parents=True, exist_ok=True)
    con = connect(db)
    init_db(con)
    con.execute("ATTACH DATABASE ? AS src", (str(main_db),))
    return con


def sync_shared(db: Path, main_db: Path, *, news_since: Optional[datetime] = None, prices_since: Optional[date] = None) -> dict:
    """Copy news/linkage/prices from the main DB (incremental unless explicit since values are given)."""
    con = _open_arm(db, main_db)
    try:
        if news_since is None:
            row = con.execute("SELECT MAX(ts_utc) FROM main.news_items").fetchone()
            news_since = (_parse_utc(row[0]) - timedelta(days=3)) if row and row[0] else datetime.now(timezone.utc) - timedelta(days=30)
        if prices_since is None:
            row = con.execute("SELECT MAX(ts_utc) FROM main.prices").fetchone()
            prices_since = (_parse_utc(row[0]).date() - timedelta(days=7)) if row and row[0] else date.today() - timedelta(days=200)
        ns = news_since.astimezone(timezone.utc).isoformat()
        out = {
            "instruments": _copy(con, "instruments", "1", ()),
            "news_items": _copy(con, "news_items", "ts_utc >= ?", (ns,)),
            "symbol_news_linkage": _copy(
                con,
                "symbol_news_linkage",
                "news_id IN (SELECT id FROM src.news_items WHERE ts_utc >= ?)",
                (ns,),
            ),
            "prices": _copy(con, "prices", "ts_utc >= ?", (prices_since.isoformat(),)),
        }
        con.commit()
        logger.info("Synced shared tables into %s since news=%s prices=%s: %s", db, ns, prices_since, out)
        return out
    finally:
        con.close()


def seed(
    db: Path,
    main_db: Path,
    *,
    own_before: datetime,
    news_since: datetime,
    prices_since: date,
    strip_tester: bool,
) -> None:
    """Fresh arm DB: shared data plus memories/recommendations/logs from the main DB before ``own_before``."""
    if db.exists():
        raise SystemExit(f"{db} already exists; delete it first to re-seed")
    sync_shared(db, main_db, news_since=news_since, prices_since=prices_since)
    con = _open_arm(db, main_db)
    try:
        cut = own_before.astimezone(timezone.utc).isoformat()
        for t in _OWN_TABLES:
            n = _copy(con, t, "ts_utc < ?", (cut,), upsert=False)
            logger.info("Seeded %s rows into %s", n, t)
        if strip_tester:
            # Backfill arms recompute tester outcomes per simulated day (no later exits in the prompt).
            for rid, meta_json in con.execute("SELECT id, meta_json FROM main.recommendations").fetchall():
                try:
                    meta = json.loads(meta_json or "{}")
                except json.JSONDecodeError:
                    continue
                if meta.pop("tester", None) is not None:
                    con.execute("UPDATE main.recommendations SET meta_json = ? WHERE id = ?", (json.dumps(meta), rid))
        else:
            _copy(con, "kv_state", "k LIKE 'strategy_test:%'", ())
        con.commit()
    finally:
        con.close()


def _arm_cfg(base: dict, db: Path, model: str, label: str, publish: bool) -> dict:
    cfg = dict(base)
    cfg["agent_db_path"] = str(db)
    cfg["agent_research_model"] = model
    cfg["agent_research_variant_label"] = label
    cfg["agent_research_publish"] = publish
    cfg["agent_research_backfill_publish"] = False
    # Interesting Stocks headlines read AGENT_DB_PATH directly.
    os.environ["AGENT_DB_PATH"] = str(db)
    return cfg


def run_live(base_cfg: dict, db: Path, main_db: Path, *, model: str, label: str, publish: bool) -> int:
    from telegram_agent.agent_research import run_research
    from telegram_agent.agent_tester import run_suggestion_tests

    sync_shared(db, main_db)
    cfg = _arm_cfg(base_cfg, db, model, label, publish)
    now = datetime.now(timezone.utc)
    run_suggestion_tests(cfg, asof_utc=now, concluded_only=True)
    n = run_research(cfg)
    logger.info("Arm %s (%s) live research done: %s new recommendation(s)", label, model, n)
    return n


def run_backfill(base_cfg: dict, db: Path, *, model: str, label: str, start: date, end: date) -> dict:
    from telegram_agent.agent_db import connect, init_db
    from telegram_agent.agent_research import (
        ResearchRunContext,
        _end_of_utc_day,
        _run_research_once,
        _utc_day_start,
    )
    from telegram_agent.agent_tester import run_suggestion_tests

    cfg = _arm_cfg(base_cfg, db, model, label, publish=False)
    out = {}
    d = start
    while d <= end:
        day_start = _utc_day_start(d)
        sim_now = _end_of_utc_day(day_start)
        run_suggestion_tests(cfg, asof_utc=sim_now, concluded_only=True)
        con = connect(db)
        init_db(con)
        try:
            ctx = ResearchRunContext(sim_now=sim_now, daily_mode=True, day_start_utc=day_start)
            out[d.isoformat()] = _run_research_once(cfg, con, ctx)
        finally:
            con.close()
        logger.info("Arm %s backfill %s: %s new recommendation(s)", label, d, out[d.isoformat()])
        d += timedelta(days=1)
    return out


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")

    from telegram_agent.log_redact import install_log_redaction

    install_log_redaction()

    from telegram_agent.config import load_config

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("seed", "run", "backfill"):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True, help="Arm agent DB path")
        if name == "seed":
            p.add_argument("--own-before", default=None, help="Copy memories/recommendations before this UTC ts (default: now)")
            p.add_argument("--news-since", default=None, help="UTC ts (default: 30 days ago)")
            p.add_argument("--prices-since", default=None, help="YYYY-MM-DD (default: 200 days ago)")
            p.add_argument("--strip-tester", action="store_true", help="Drop stored tester outcomes (for backfills)")
        else:
            p.add_argument("--model", required=True)
            p.add_argument("--label", required=True)
        if name == "run":
            p.add_argument("--no-publish", action="store_true")
        if name == "backfill":
            p.add_argument("--from", dest="start", required=True)
            p.add_argument("--to", dest="end", required=True)
    args = ap.parse_args()

    cfg = load_config()
    main_db = Path(cfg["agent_db_path"]).resolve()
    db = Path(args.db)
    if not db.is_absolute():
        db = (root / db).resolve()

    if args.cmd == "seed":
        now = datetime.now(timezone.utc)
        seed(
            db,
            main_db,
            own_before=_parse_utc(args.own_before) if args.own_before else now,
            news_since=_parse_utc(args.news_since) if args.news_since else now - timedelta(days=30),
            prices_since=date.fromisoformat(args.prices_since) if args.prices_since else now.date() - timedelta(days=200),
            strip_tester=args.strip_tester,
        )
    elif args.cmd == "run":
        run_live(cfg, db, main_db, model=args.model, label=args.label, publish=not args.no_publish)
    else:
        res = run_backfill(
            cfg, db, model=args.model, label=args.label,
            start=date.fromisoformat(args.start), end=date.fromisoformat(args.end),
        )
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
