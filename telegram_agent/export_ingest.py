#!/usr/bin/env python3
"""
Export all ingest rows (news_items) from agent.sqlite to CSV.

Usage:
  python -m telegram_agent.export_ingest
  python -m telegram_agent.export_ingest --out ~/Desktop/news_items.csv
  python -m telegram_agent.export_ingest --db /path/to/agent.sqlite
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(description="Export news_items to CSV")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to agent.sqlite (default: AGENT_DB_PATH / config)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output CSV path (default: telegram_agent/data/news_items_export.csv)",
    )
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")

    from telegram_agent.config import load_config
    from telegram_agent.agent_db import connect, init_db

    cfg = load_config()
    db_path = args.db or Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    out_path = args.out or (
        Path(__file__).resolve().parent / "data" / "news_items_export.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    import pandas as pd

    con = connect(db_path)
    init_db(con)
    df = pd.read_sql_query(
        """
        SELECT id, source_type, source_name, title, content, url, ts_utc, condensed
        FROM news_items
        ORDER BY ts_utc
        """,
        con,
    )
    con.close()

    df.to_csv(out_path, index=False, encoding="utf-8")
    print(f"Exported {len(df)} rows to {out_path.resolve()}")


if __name__ == "__main__":
    main()
