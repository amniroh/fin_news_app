#!/usr/bin/env python3
"""
Summarize price rows loaded from historical Parquet into the agent DB.

Ingest uses ``source='historical_parquet'`` (see ``import_historical_parquet``).
This report aggregates counts, min/max timestamps, and intervals per symbol.
Rows from other sources (e.g. yfinance) are excluded.

Usage::

    python -m telegram_agent.report_parquet_import_stats
    python -m telegram_agent.report_parquet_import_stats --output-json path.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram_agent.agent_db import connect, init_db
from telegram_agent.config import DATA_DIR, load_config

SOURCE_TAG = "historical_parquet"


def _table_stats(con, sql: str, params: tuple, *, table: str) -> List[Dict[str, Any]]:
    cur = con.execute(sql, params)
    rows = []
    for r in cur.fetchall():
        d = {
            "storage_table": table,
            "symbol": r["symbol"],
            "row_count": int(r["n"]),
            "min_ts_utc": r["min_ts"],
            "max_ts_utc": r["max_ts"],
        }
        if "interval" in r.keys():
            d["interval"] = r["interval"]
        rows.append(d)
    return rows


def _scalar_int(con, sql: str, params: tuple) -> int:
    cur = con.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return 0
    v = row[0]
    return int(v) if v is not None else 0


def build_report(con) -> Dict[str, Any]:
    hourly = _table_stats(
        con,
        """
        SELECT symbol,
               COUNT(*) AS n,
               MIN(ts_utc) AS min_ts,
               MAX(ts_utc) AS max_ts
        FROM prices_hourly
        WHERE source = ?
        GROUP BY symbol
        ORDER BY symbol
        """,
        (SOURCE_TAG,),
        table="prices_hourly",
    )
    minute = _table_stats(
        con,
        """
        SELECT symbol,
               COUNT(*) AS n,
               MIN(ts_utc) AS min_ts,
               MAX(ts_utc) AS max_ts
        FROM prices_minute
        WHERE source = ?
        GROUP BY symbol
        ORDER BY symbol
        """,
        (SOURCE_TAG,),
        table="prices_minute",
    )
    prices_generic = _table_stats(
        con,
        """
        SELECT symbol,
               interval,
               COUNT(*) AS n,
               MIN(ts_utc) AS min_ts,
               MAX(ts_utc) AS max_ts
        FROM prices
        WHERE source = ?
        GROUP BY symbol, interval
        ORDER BY symbol, interval
        """,
        (SOURCE_TAG,),
        table="prices",
    )

    total_hourly = _scalar_int(
        con,
        "SELECT COUNT(*) FROM prices_hourly WHERE source = ?",
        (SOURCE_TAG,),
    )
    total_minute = _scalar_int(
        con,
        "SELECT COUNT(*) FROM prices_minute WHERE source = ?",
        (SOURCE_TAG,),
    )
    total_prices = _scalar_int(
        con,
        "SELECT COUNT(*) FROM prices WHERE source = ?",
        (SOURCE_TAG,),
    )

    # Symbols that appear in any parquet-backed table
    syms: set[str] = set()
    for block in (hourly, minute):
        for r in block:
            syms.add(r["symbol"])
    for r in prices_generic:
        syms.add(r["symbol"])

    by_symbol: Dict[str, Any] = {}
    for s in sorted(syms):
        by_symbol[s] = {
            "prices_hourly": [x for x in hourly if x["symbol"] == s],
            "prices_minute": [x for x in minute if x["symbol"] == s],
            "prices": [x for x in prices_generic if x["symbol"] == s],
        }
        rows_h = sum(x["row_count"] for x in by_symbol[s]["prices_hourly"])
        rows_m = sum(x["row_count"] for x in by_symbol[s]["prices_minute"])
        rows_p = sum(x["row_count"] for x in by_symbol[s]["prices"])
        by_symbol[s]["row_count_total_parquet_source"] = rows_h + rows_m + rows_p

    return {
        "source_filter": SOURCE_TAG,
        "note": (
            "Only rows stored with source='historical_parquet'. "
            "Parquet ingest uses ON CONFLICT DO NOTHING; yfinance rows already present "
            "for the same (symbol, ts) are not overwritten, so counts can be lower than "
            "row counts in Parquet files on disk. "
            "Interval routing from import_historical_parquet: 1h→prices_hourly, 1m→prices_minute, "
            "5m/1d→prices with that interval — long history often appears as 5m in `prices`, "
            "not as rows in prices_hourly."
        ),
        "totals": {
            "rows_prices_hourly": total_hourly,
            "rows_prices_minute": total_minute,
            "rows_prices": total_prices,
            "rows_all_tables": total_hourly + total_minute + total_prices,
            "distinct_symbols": len(syms),
        },
        "by_table": {
            "prices_hourly": hourly,
            "prices_minute": minute,
            "prices": prices_generic,
        },
        "by_symbol": by_symbol,
    }


def _write_flat_csv(path: Path, report: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "storage_table",
                "symbol",
                "interval",
                "row_count",
                "min_ts_utc",
                "max_ts_utc",
            ]
        )
        for r in report["by_table"]["prices_hourly"]:
            w.writerow(
                [
                    r["storage_table"],
                    r["symbol"],
                    "",
                    r["row_count"],
                    r["min_ts_utc"],
                    r["max_ts_utc"],
                ]
            )
        for r in report["by_table"]["prices_minute"]:
            w.writerow(
                [
                    r["storage_table"],
                    r["symbol"],
                    "",
                    r["row_count"],
                    r["min_ts_utc"],
                    r["max_ts_utc"],
                ]
            )
        for r in report["by_table"]["prices"]:
            w.writerow(
                [
                    r["storage_table"],
                    r["symbol"],
                    r.get("interval", ""),
                    r["row_count"],
                    r["min_ts_utc"],
                    r["max_ts_utc"],
                ]
            )


def _print_tables(report: Dict[str, Any]) -> None:
    t = report["totals"]
    print(f"Source: {report['source_filter']}")
    print(f"Note: {report['note']}")
    print()
    print(
        "Totals:",
        f"hourly={t['rows_prices_hourly']}",
        f"minute={t['rows_prices_minute']}",
        f"prices={t['rows_prices']}",
        f"all={t['rows_all_tables']}",
        f"symbols={t['distinct_symbols']}",
    )
    print()

    def block(title: str, rows: List[Dict[str, Any]], show_interval: bool) -> None:
        print(title)
        if not rows:
            print("  (none)")
            print()
            return
        for r in rows:
            if show_interval:
                print(
                    f"  {r['symbol']:10}  {str(r.get('interval', '')):6}  "
                    f"n={r['row_count']:10}  {r['min_ts_utc']}  →  {r['max_ts_utc']}"
                )
            else:
                print(
                    f"  {r['symbol']:10}  n={r['row_count']:10}  "
                    f"{r['min_ts_utc']}  →  {r['max_ts_utc']}"
                )
        print()

    block("prices_hourly (1h bars)", report["by_table"]["prices_hourly"], False)
    block("prices_minute (1m bars)", report["by_table"]["prices_minute"], False)
    block("prices (5m / 1d / other intervals)", report["by_table"]["prices"], True)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Report SQLite stats for prices imported from historical Parquet."
    )
    p.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help=f"Write full report JSON (default: {DATA_DIR}/reports/parquet_import_stats.json)",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Only write JSON, no table print",
    )
    p.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Also write a flat CSV of per-table symbol stats (default: next to JSON)",
    )
    args = p.parse_args(argv)

    cfg = load_config()
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    out = args.output_json
    if out is None:
        out = Path(DATA_DIR) / "reports" / "parquet_import_stats.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    con = connect(db)
    init_db(con)
    try:
        report = build_report(con)
        report["agent_db_path"] = str(db.resolve())
    finally:
        con.close()

    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    csv_path = args.csv
    if csv_path is None and not args.quiet:
        csv_path = out.with_suffix(".csv")
    if csv_path is not None:
        _write_flat_csv(csv_path, report)
    if not args.quiet:
        _print_tables(report)
        print(f"Wrote {out.resolve()}")
        if csv_path is not None:
            print(f"Wrote {csv_path.resolve()}")
    else:
        print(str(out.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
