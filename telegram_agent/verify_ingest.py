#!/usr/bin/env python3
"""
Visual sanity check for agent pipeline output in agent.sqlite:

  • Ingest: news_items (per-day counts for the last N UTC days, newest first; --last-days)
  • Extract: news_mentions + instruments (ticker/symbol extraction)

Usage (from repo root):
  python -m telegram_agent.verify_ingest              # ingest + extract reports & charts
  python -m telegram_agent.verify_ingest --ingest-only
  python -m telegram_agent.verify_ingest --extract-only
  python -m telegram_agent.verify_ingest --db /path/to/agent.sqlite --out ./verify_plots
  python -m telegram_agent.verify_ingest --last-days 14

Requires: matplotlib (see telegram_agent/requirements.txt)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _parse_ts_utc(pd, series):
    """Parse ISO-8601 timestamps including fractional seconds (e.g. from SQLite agent DB)."""
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
        import matplotlib.dates as mdates

        return plt, mdates
    except ImportError:
        print("matplotlib not installed. Install with: pip install matplotlib")
        print("Or: pip install -r telegram_agent/requirements.txt")
        sys.exit(1)


def _report_ingest(
    df,
    *,
    out_dir: Path,
    no_plots: bool,
    sample: int,
    db_path: Path,
    last_days: int,
) -> None:
    import pandas as pd

    if df.empty:
        print(f"No rows in news_items at {db_path}")
        return

    df = df.copy()
    df["ts"] = _parse_ts_utc(pd, df["ts_utc"])

    print("=== Ingest sanity report (news_items) ===")
    print(f"Database: {db_path.resolve()}")
    print(f"Total rows: {len(df)}")
    print(f"Time range (UTC): {df['ts'].min()} → {df['ts'].max()}")
    print(f"Unique source_name: {df['source_name'].nunique()}")
    print()

    print("--- By source_type ---")
    print(df["source_type"].value_counts().to_string())
    print()

    print("--- Top 20 source_name ---")
    print(df["source_name"].value_counts().head(20).to_string())
    print()

    df["day"] = df["ts"].dt.floor("D")
    per_day = df.groupby("day", sort=True).size()
    # Last N UTC calendar days ending at the newest day in the data (inclusive); days with no rows show 0
    max_day = pd.Timestamp(per_day.index.max())
    if max_day.tzinfo is None:
        max_day = max_day.tz_localize("UTC")
    else:
        max_day = max_day.tz_convert("UTC")
    max_day = max_day.normalize()
    start_day = max_day - pd.Timedelta(days=last_days - 1)
    day_range = pd.date_range(start=start_day, end=max_day, freq="D", tz="UTC")
    per_day_utc = per_day.copy()
    _idx = pd.DatetimeIndex(per_day_utc.index)
    if _idx.tz is None:
        _idx = _idx.tz_localize("UTC")
    else:
        _idx = _idx.tz_convert("UTC")
    per_day_utc.index = _idx.normalize()
    daily_window = per_day_utc.reindex(day_range, fill_value=0).astype(int)
    print(f"--- News count per UTC calendar day (last {last_days} days, newest first) ---")
    for day in daily_window.index[::-1]:
        print(f"  {day.strftime('%Y-%m-%d')}  {int(daily_window[day])}")
    nz = int((daily_window > 0).sum())
    print(
        f"  Summary: {nz}/{last_days} day(s) with ≥1 item in window; "
        f"items in window={int(daily_window.sum())}; "
        f"mean/day={daily_window.mean():.2f}"
    )
    print()

    print("--- Content length (chars) ---")
    print(df["content_len"].describe().to_string())
    print()

    for st in sorted(df["source_type"].unique()):
        sub = df[df["source_type"] == st].sort_values("ts", ascending=False)
        print(f"--- Sample {st} (newest first, n={min(sample, len(sub))}) ---")
        for _, r in sub.head(sample).iterrows():
            t = str(r["title"])[:100].replace("\n", " ")
            print(f"  [{r['ts']}] {r['source_name']}: {t}")
        print()

    if no_plots:
        return

    plt, mdates = _ensure_matplotlib()

    fig, ax = plt.subplots(figsize=(8, 4))
    df["source_type"].value_counts().plot(kind="bar", ax=ax, color="steelblue")
    ax.set_title("News items by source_type")
    ax.set_xlabel("source_type")
    ax.set_ylabel("count")
    fig.tight_layout()
    fig.savefig(out_dir / "ingest_by_source_type.png", dpi=120)
    plt.close(fig)
    print(f"Saved: {out_dir / 'ingest_by_source_type.png'}")

    daily = per_day_utc
    fig, ax = plt.subplots(figsize=(10, 4))
    daily.plot(ax=ax, color="darkgreen", linewidth=1.2)
    ax.set_title("Ingest: items per day (UTC)")
    ax.set_ylabel("count")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_dir / "ingest_items_per_day.png", dpi=120)
    plt.close(fig)
    print(f"Saved: {out_dir / 'ingest_items_per_day.png'}")

    dw = daily_window.sort_index()
    fig_w = min(22, max(10, 0.22 * len(dw)))
    fig, ax = plt.subplots(figsize=(fig_w, 4))
    ax.bar(range(len(dw)), dw.values, color="steelblue", edgecolor="white", linewidth=0.5)
    ax.set_xticks(range(len(dw)))
    ax.set_xticklabels([d.strftime("%Y-%m-%d") for d in dw.index], rotation=55, ha="right")
    ax.set_title(f"Ingest: news items per UTC day (last {last_days} days, chronological)")
    ax.set_ylabel("news count")
    fig.tight_layout()
    fig.savefig(out_dir / "ingest_last_days_per_day.png", dpi=120)
    plt.close(fig)
    print(f"Saved: {out_dir / 'ingest_last_days_per_day.png'}")

    top = df["source_name"].value_counts().head(15)
    fig, ax = plt.subplots(figsize=(9, 5))
    top.sort_values().plot(kind="barh", ax=ax, color="coral")
    ax.set_title("Ingest: top 15 source_name")
    fig.tight_layout()
    fig.savefig(out_dir / "ingest_top_sources.png", dpi=120)
    plt.close(fig)
    print(f"Saved: {out_dir / 'ingest_top_sources.png'}")

    df_sorted = df.sort_values("ts")
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(df_sorted["ts"], range(1, len(df_sorted) + 1), color="navy", linewidth=1)
    ax.set_title("Ingest: cumulative news items over time")
    ax.set_ylabel("cumulative count")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_dir / "ingest_cumulative.png", dpi=120)
    plt.close(fig)
    print(f"Saved: {out_dir / 'ingest_cumulative.png'}")


def _report_extract(
    con,
    *,
    news_total: int,
    out_dir: Path,
    no_plots: bool,
    sample: int,
    db_path: Path,
) -> None:
    import pandas as pd

    mentions = pd.read_sql_query(
        """
        SELECT m.news_id, m.symbol, m.mention_type, m.confidence,
               n.ts_utc, n.source_type, n.source_name, n.title
        FROM news_mentions m
        JOIN news_items n ON n.id = m.news_id
        ORDER BY n.ts_utc
        """,
        con,
    )

    instruments = pd.read_sql_query("SELECT symbol, kind, name FROM instruments ORDER BY symbol", con)

    print()
    print("=== Extract sanity report (news_mentions + instruments) ===")
    print(f"Database: {db_path.resolve()}")

    if instruments.empty:
        print("instruments: 0 rows")
    else:
        print(f"instruments: {len(instruments)} unique symbols")
        print("--- instruments by kind ---")
        print(instruments["kind"].value_counts().to_string())
        print()

    if mentions.empty:
        print("news_mentions: 0 rows (run: python -m telegram_agent.agent extract)")
        print()
        if not no_plots:
            print("Skipping extract charts (no mentions).")
        return

    mentions["ts"] = _parse_ts_utc(pd, mentions["ts_utc"])

    print(f"news_mentions: {len(mentions)} rows")
    print(f"Unique symbols mentioned: {mentions['symbol'].nunique()}")
    if news_total > 0:
        covered = mentions["news_id"].nunique()
        pct = 100.0 * covered / news_total
        print(f"News items with ≥1 mention: {covered} / {news_total} ({pct:.1f}%)")
    print()

    print("--- By mention_type ---")
    print(mentions["mention_type"].value_counts().to_string())
    print()

    print("--- Top 25 symbols by mention count ---")
    print(mentions["symbol"].value_counts().head(25).to_string())
    print()

    if mentions["confidence"].notna().any():
        print("--- confidence (where present) ---")
        print(mentions["confidence"].describe().to_string())
        print()

    print(f"--- Sample mentions (newest first, n={min(sample, len(mentions))}) ---")
    for _, r in mentions.sort_values("ts", ascending=False).head(sample).iterrows():
        t = str(r["title"])[:90].replace("\n", " ")
        print(
            f"  [{r['ts']}] {r['symbol']} ({r['mention_type']}) "
            f"[{r['source_name']}] {t}"
        )
    print()

    if no_plots:
        return

    plt, mdates = _ensure_matplotlib()

    fig, ax = plt.subplots(figsize=(8, 4))
    mentions["mention_type"].value_counts().plot(kind="bar", ax=ax, color="teal")
    ax.set_title("Extract: mentions by mention_type")
    ax.set_xlabel("mention_type")
    ax.set_ylabel("count")
    fig.tight_layout()
    fig.savefig(out_dir / "extract_by_mention_type.png", dpi=120)
    plt.close(fig)
    print(f"Saved: {out_dir / 'extract_by_mention_type.png'}")

    top_sym = mentions["symbol"].value_counts().head(20)
    fig, ax = plt.subplots(figsize=(9, 6))
    top_sym.sort_values().plot(kind="barh", ax=ax, color="mediumpurple")
    ax.set_title("Extract: top 20 symbols by mention count")
    fig.tight_layout()
    fig.savefig(out_dir / "extract_top_symbols.png", dpi=120)
    plt.close(fig)
    print(f"Saved: {out_dir / 'extract_top_symbols.png'}")

    mentions["day"] = mentions["ts"].dt.floor("D")
    daily_m = mentions.groupby("day").size()
    fig, ax = plt.subplots(figsize=(10, 4))
    daily_m.plot(ax=ax, color="darkmagenta", linewidth=1.2)
    ax.set_title("Extract: mentions per day (by news ts, UTC)")
    ax.set_ylabel("mention rows")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_dir / "extract_mentions_per_day.png", dpi=120)
    plt.close(fig)
    print(f"Saved: {out_dir / 'extract_mentions_per_day.png'}")

    m_sorted = mentions.sort_values("ts")
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(m_sorted["ts"], range(1, len(m_sorted) + 1), color="indigo", linewidth=1)
    ax.set_title("Extract: cumulative mention rows over time")
    ax.set_ylabel("cumulative")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_dir / "extract_cumulative_mentions.png", dpi=120)
    plt.close(fig)
    print(f"Saved: {out_dir / 'extract_cumulative_mentions.png'}")

    # Mentions by source_type (of the news item)
    fig, ax = plt.subplots(figsize=(8, 4))
    mentions["source_type"].value_counts().plot(kind="bar", ax=ax, color="goldenrod")
    ax.set_title("Extract: mentions by underlying news source_type")
    fig.tight_layout()
    fig.savefig(out_dir / "extract_by_news_source_type.png", dpi=120)
    plt.close(fig)
    print(f"Saved: {out_dir / 'extract_by_news_source_type.png'}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Visualize agent ingest + extract DB sanity"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to agent.sqlite (default: from config / AGENT_DB_PATH)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Directory for PNG charts (default: telegram_agent/data/ingest_verify/)",
    )
    parser.add_argument("--no-plots", action="store_true", help="Print stats only, no charts")
    parser.add_argument("--sample", type=int, default=5, help="Sample rows per section")
    parser.add_argument(
        "--ingest-only",
        action="store_true",
        help="Only report news_items (ingest)",
    )
    parser.add_argument(
        "--extract-only",
        action="store_true",
        help="Only report news_mentions + instruments (extract)",
    )
    parser.add_argument(
        "--last-days",
        type=int,
        default=30,
        metavar="N",
        help="Ingest report: show per-day news counts for the last N UTC days (default: 30)",
    )
    args = parser.parse_args()

    if args.ingest_only and args.extract_only:
        print("Use only one of --ingest-only or --extract-only, not both.")
        sys.exit(2)

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")

    from telegram_agent.config import load_config
    from telegram_agent.agent_db import connect, init_db

    cfg = load_config()
    db_path = args.db or Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    out_dir = args.out or (Path(__file__).resolve().parent / "data" / "ingest_verify")
    if not args.no_plots:
        out_dir.mkdir(parents=True, exist_ok=True)

    import pandas as pd

    con = connect(db_path)
    init_db(con)

    df_news = None
    news_total = 0

    if args.extract_only:
        news_total = int(
            pd.read_sql_query("SELECT COUNT(*) AS c FROM news_items", con)["c"].iloc[0]
        )
    else:
        df_news = pd.read_sql_query(
            """
            SELECT id, source_type, source_name, title,
                   length(content) AS content_len, ts_utc, url
            FROM news_items
            ORDER BY ts_utc
            """,
            con,
        )
        news_total = len(df_news)

    # Ingest section first (news_items), then extract (mentions)
    if not args.extract_only:
        _report_ingest(
            df_news,
            out_dir=out_dir,
            no_plots=args.no_plots,
            sample=args.sample,
            db_path=db_path,
            last_days=max(1, args.last_days),
        )

    if not args.ingest_only:
        _report_extract(
            con,
            news_total=news_total,
            out_dir=out_dir,
            no_plots=args.no_plots,
            sample=args.sample,
            db_path=db_path,
        )

    con.close()

    print()
    print(f"Done. Charts (if any): {out_dir.resolve()}")


if __name__ == "__main__":
    main()
