#!/usr/bin/env python3
"""
Summarize and plot statistics for daily OHLCV rows in agent.sqlite (`prices` table).

Usage (from repo root):
  python -m telegram_agent.visualize_prices
  python -m telegram_agent.visualize_prices --db telegram_agent/data/agent.sqlite --out ./price_plots
  python -m telegram_agent.visualize_prices --no-plots

Requires: pandas, matplotlib (telegram_agent/requirements.txt)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _parse_ts(series):
    import pandas as pd

    try:
        return pd.to_datetime(series, utc=True, format="ISO8601")
    except (ValueError, TypeError):
        pass
    try:
        return pd.to_datetime(series, utc=True, format="mixed")
    except (ValueError, TypeError):
        pass
    return pd.to_datetime(series, utc=True)


def _ensure_matplotlib():
    try:
        import matplotlib.pyplot as plt

        return plt
    except ImportError:
        print("matplotlib not installed. pip install matplotlib")
        sys.exit(1)


def main() -> None:
    from telegram_agent.config import DATA_DIR, load_config

    parser = argparse.ArgumentParser(description="Visualize price DB statistics")
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to agent.sqlite (default: from config / env AGENT_DB_PATH)",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Output directory for PNGs (default: telegram_agent/data/price_stats)",
    )
    parser.add_argument(
        "--interval",
        type=str,
        default="1d",
        help="Price interval to analyze (default: 1d)",
    )
    parser.add_argument(
        "--no-plots",
        action="store_true",
        help="Print summary only; do not write charts",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="Top N symbols by row count for bar chart (default: 25)",
    )
    args = parser.parse_args()

    cfg = load_config()
    db_path = Path(args.db or cfg.get("agent_db_path") or (DATA_DIR / "agent.sqlite")).expanduser()
    out_dir = Path(args.out or (DATA_DIR / "price_stats")).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    import pandas as pd

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    cur = con.execute(
        "SELECT COUNT(*) AS c FROM prices WHERE interval = ?", (args.interval,)
    )
    total_rows = int(cur.fetchone()["c"])
    cur = con.execute(
        "SELECT COUNT(DISTINCT symbol) AS c FROM prices WHERE interval = ?",
        (args.interval,),
    )
    n_symbols = int(cur.fetchone()["c"])

    df_sym = pd.read_sql_query(
        """
        SELECT symbol,
               COUNT(*) AS n_bars,
               MIN(ts_utc) AS min_ts,
               MAX(ts_utc) AS max_ts
        FROM prices
        WHERE interval = ?
        GROUP BY symbol
        """,
        con,
        params=(args.interval,),
    )
    con.close()

    print("=== Price database stats ===")
    print(f"Database: {db_path.resolve()}")
    print(f"Interval: {args.interval}")
    print(f"Distinct symbols: {n_symbols}")
    print(f"Total price rows: {total_rows}")
    print()

    if df_sym.empty:
        print("No rows in `prices` for this interval. Run: python -m telegram_agent.agent prices")
        return

    df_sym["min_ts"] = _parse_ts(df_sym["min_ts"])
    df_sym["max_ts"] = _parse_ts(df_sym["max_ts"])
    df_sym["span_days"] = (df_sym["max_ts"] - df_sym["min_ts"]).dt.total_seconds() / 86400.0

    print("--- Bars per symbol ---")
    print(df_sym["n_bars"].describe().to_string())
    print()
    print("--- History span (days) per symbol ---")
    print(df_sym["span_days"].describe().to_string())
    print()

    print(f"--- Top {args.top} symbols by bar count ---")
    top = df_sym.nlargest(args.top, "n_bars")[
        ["symbol", "n_bars", "min_ts", "max_ts", "span_days"]
    ]
    print(top.to_string(index=False))
    print()

    thin = df_sym[df_sym["n_bars"] < 5]
    if len(thin) > 0:
        print(f"--- Symbols with fewer than 5 bars ({len(thin)}) — may need backfill ---")
        print(thin.sort_values("n_bars")[["symbol", "n_bars"]].head(40).to_string(index=False))
        print()

    if args.no_plots:
        print(f"(Charts skipped; output dir would be {out_dir})")
        return

    plt = _ensure_matplotlib()

    # 1) Histogram: distribution of bar counts per symbol
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.hist(df_sym["n_bars"], bins=min(50, max(10, n_symbols // 5)), color="steelblue", edgecolor="white")
    ax.set_title(f"Distribution: daily bars per symbol (interval={args.interval})")
    ax.set_xlabel("Number of bars")
    ax.set_ylabel("Number of symbols")
    fig.tight_layout()
    p1 = out_dir / "price_bars_per_symbol_hist.png"
    fig.savefig(p1, dpi=120)
    plt.close(fig)
    print(f"Saved: {p1}")

    # 2) Horizontal bar: top symbols by row count
    fig, ax = plt.subplots(figsize=(10, max(5, args.top * 0.22)))
    sub = df_sym.nlargest(args.top, "n_bars").sort_values("n_bars")
    ax.barh(sub["symbol"], sub["n_bars"], color="darkseagreen")
    ax.set_xlabel("Row count")
    ax.set_title(f"Top {args.top} symbols by price row count")
    fig.tight_layout()
    p2 = out_dir / "price_top_symbols_by_rows.png"
    fig.savefig(p2, dpi=120)
    plt.close(fig)
    print(f"Saved: {p2}")

    # 3) Span days distribution
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.hist(df_sym["span_days"].clip(upper=df_sym["span_days"].quantile(0.99)), bins=40, color="coral", edgecolor="white")
    ax.set_title("Distribution: history span (days) per symbol (99th pct clipped for scale)")
    ax.set_xlabel("Span (days)")
    ax.set_ylabel("Symbols")
    fig.tight_layout()
    p3 = out_dir / "price_history_span_days_hist.png"
    fig.savefig(p3, dpi=120)
    plt.close(fig)
    print(f"Saved: {p3}")

    print()
    print(f"Done. Charts: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
