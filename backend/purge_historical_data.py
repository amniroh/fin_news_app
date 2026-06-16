#!/usr/bin/env python3
"""
Delete market time-series rows older than a retention window, then VACUUM.

Targets value_metrics.sqlite and agent.sqlite (prices, fundamentals, news, etc.).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "backend"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024) if path.exists() else 0.0


def _cutoffs(days: int) -> Tuple[str, str]:
    ref = _utcnow()
    return (ref - timedelta(days=days)).date().isoformat(), (ref - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _count(con: sqlite3.Connection, table: str, where: str = "", params: Tuple[Any, ...] = ()) -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(con.execute(sql, params).fetchone()[0])


def _delete_batched(
    con: sqlite3.Connection,
    table: str,
    where: str,
    params: Tuple[Any, ...],
    *,
    batch_size: int = 50_000,
) -> int:
    total = 0
    while True:
        cur = con.execute(
            f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT ?)",
            (*params, batch_size),
        )
        n = int(cur.rowcount)
        con.commit()
        total += n
        if n < batch_size:
            break
    return total


def _delete(con: sqlite3.Connection, table: str, where: str, params: Tuple[Any, ...]) -> int:
    return _delete_batched(con, table, where, params)


def purge_value_metrics(con: sqlite3.Connection, cutoff_date: str, *, dry_run: bool) -> Dict[str, int]:
    specs = [
        ("vm_metric_points", "asof_date < ?", (cutoff_date,)),
        ("vm_fundamental_points", "asof_date < ?", (cutoff_date,)),
        ("vm_analyst_ratings", "asof_date < ?", (cutoff_date,)),
        ("vm_stock_splits", "ex_date < ?", (cutoff_date,)),
    ]
    out: Dict[str, int] = {}
    for table, where, params in specs:
        out[table] = _count(con, table, where, params)
        if not dry_run and out[table]:
            _delete(con, table, where, params)
    return out


def purge_agent(con: sqlite3.Connection, cutoff_ts: str, *, dry_run: bool) -> Dict[str, int]:
    out: Dict[str, int] = {}
    old_news = _count(con, "news_items", "ts_utc < ?", (cutoff_ts,))
    out["news_items"] = old_news

    linkage_where = "linked_ts_utc < ? OR news_id IN (SELECT id FROM news_items WHERE ts_utc < ?)"
    out["symbol_news_linkage"] = _count(con, "symbol_news_linkage", linkage_where, (cutoff_ts, cutoff_ts))
    out["news_mentions"] = _count(
        con,
        "news_mentions",
        "news_id IN (SELECT id FROM news_items WHERE ts_utc < ?)",
        (cutoff_ts,),
    )

    for table in ("prices", "prices_hourly", "prices_minute"):
        out[table] = _count(con, table, "ts_utc < ?", (cutoff_ts,))

    out["stock_splits"] = _count(con, "stock_splits", "effective_ts_utc < ?", (cutoff_ts,))

    if dry_run:
        return out

    if out["news_mentions"]:
        _delete(con, "news_mentions", "news_id IN (SELECT id FROM news_items WHERE ts_utc < ?)", (cutoff_ts,))
    if out["symbol_news_linkage"]:
        _delete(con, "symbol_news_linkage", linkage_where, (cutoff_ts, cutoff_ts))
    if out["news_items"]:
        _delete(con, "news_items", "ts_utc < ?", (cutoff_ts,))
    for table in ("prices", "prices_hourly", "prices_minute"):
        if out[table]:
            _delete(con, table, "ts_utc < ?", (cutoff_ts,))
    if out["stock_splits"]:
        _delete(con, "stock_splits", "effective_ts_utc < ?", (cutoff_ts,))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge market data older than N days")
    ap.add_argument("--days", type=int, default=365, help="Retention window in days (default: 365)")
    ap.add_argument(
        "--vm-db",
        default=os.getenv("VALUE_METRICS_DB_PATH", str(_REPO_ROOT / "backend" / "data" / "value_metrics.sqlite")),
    )
    ap.add_argument(
        "--agent-db",
        default=os.getenv("AGENT_DB_PATH", str(_REPO_ROOT / "telegram_agent" / "data" / "agent.sqlite")),
    )
    ap.add_argument("--dry-run", action="store_true", help="Report only; do not delete or VACUUM")
    ap.add_argument("--skip-vacuum", action="store_true", help="Delete rows but skip VACUUM")
    args = ap.parse_args()

    vm_path = Path(args.vm_db).expanduser()
    agent_path = Path(args.agent_db).expanduser()
    cutoff_date, cutoff_ts = _cutoffs(int(args.days))

    before_vm = _size_mb(vm_path)
    before_agent = _size_mb(agent_path)
    print(f"Retention: last {args.days} days (cutoff date={cutoff_date})")
    print(f"BEFORE: value_metrics={before_vm:.1f} MB  agent={before_agent:.1f} MB  total={before_vm + before_agent:.1f} MB")

    vm_deleted: Dict[str, int] = {}
    agent_deleted: Dict[str, int] = {}

    if vm_path.is_file():
        con = sqlite3.connect(vm_path)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            vm_deleted = purge_value_metrics(con, cutoff_date, dry_run=bool(args.dry_run))
        finally:
            con.close()
        if not args.dry_run and not args.skip_vacuum:
            print("VACUUM value_metrics.sqlite …")
            con = sqlite3.connect(vm_path)
            try:
                con.execute("VACUUM")
            finally:
                con.close()

    if agent_path.is_file():
        con = sqlite3.connect(agent_path)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            agent_deleted = purge_agent(con, cutoff_ts, dry_run=bool(args.dry_run))
        finally:
            con.close()
        if not args.dry_run and not args.skip_vacuum:
            print("VACUUM agent.sqlite …")
            con = sqlite3.connect(agent_path)
            try:
                con.execute("VACUUM")
            finally:
                con.close()

    after_vm = _size_mb(vm_path)
    after_agent = _size_mb(agent_path)
    saved = (before_vm + before_agent) - (after_vm + after_agent)

    print("\nRows" + (" that would be deleted" if args.dry_run else " deleted") + ":")
    for label, data in (("value_metrics", vm_deleted), ("agent", agent_deleted)):
        if not data:
            continue
        print(f"  [{label}]")
        for table, n in data.items():
            if n:
                print(f"    {table}: {n:,}")

    print(f"\nAFTER:  value_metrics={after_vm:.1f} MB  agent={after_agent:.1f} MB  total={after_vm + after_agent:.1f} MB")
    print(f"SAVED:  {saved:.1f} MB ({saved / 1024:.2f} GB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
