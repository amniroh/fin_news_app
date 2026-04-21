#!/usr/bin/env python3
"""
Plot hourly adjusted-close price history from the agent DB for the competitive
backtest symbol set.

Symbol resolution matches ``competitive_backtest.resolve_backtest_symbols`` when
``COMPETITIVE_BACKTEST_SYMBOLS`` is empty: default ``COMPETITIVE_BACKTEST_SYMBOL_MODE``
(usually the priority-filtered competition universe). Override with ``--symbols``.

Outputs one PNG per symbol (and optional PDF), plus summary.json and index.html
for quick visual QA against an external feed.

``--start`` / ``--end`` only **clip** hourly rows already stored in SQLite; they do not
backfill history. If you only see recent years, the database’s ``prices_hourly`` (or
fallback ``prices`` 1h rows) for that symbol does not contain older bars yet.

``--sources`` restricts rows to the given ``source`` column values (e.g. ``yfinance,alpaca``).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telegram_agent.agent_db import connect, get_full_adj_close_series_asc, init_db
from telegram_agent.competitive_backtest import resolve_backtest_symbols
from telegram_agent.config import DATA_DIR, load_config
from telegram_agent.rsi_portfolio_simulator import _parse_competitive_backtest_symbols
from telegram_agent.symbol_universe import normalize_symbol


def _parse_dt_bounds(
    start_s: Optional[str], end_s: Optional[str]
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """YYYY-MM-DD or ISO; UTC."""
    from telegram_agent.rsi_portfolio_simulator import _parse_start_end_arg

    start = _parse_start_end_arg(start_s, is_end=False) if start_s else None
    end = _parse_start_end_arg(end_s, is_end=True) if end_s else None
    return start, end


def _clip_series(
    series: List[Tuple[datetime, float]],
    start: Optional[datetime],
    end: Optional[datetime],
) -> List[Tuple[datetime, float]]:
    if not series:
        return []
    out = series
    if start is not None:
        out = [(t, p) for t, p in out if t >= start]
    if end is not None:
        out = [(t, p) for t, p in out if t <= end]
    return out


def _safe_filename(sym: str) -> str:
    s = "".join(c if (c.isalnum() or c in "-_") else "_" for c in sym)
    return s or "symbol"


def _parse_symbols_override(raw: Optional[str]) -> List[str]:
    if not raw or not str(raw).strip():
        return []
    return sorted(
        {normalize_symbol(x) for x in str(raw).split(",") if str(x).strip()}
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Plot DB hourly prices for competitive backtest symbols: "
            "--symbols, else COMPETITIVE_BACKTEST_SYMBOLS if set, "
            "else resolve_backtest_symbols (same as walk-forward backtest)."
        )
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for PNGs / index.html / summary.json (default: under DATA_DIR)",
    )
    p.add_argument(
        "--symbols",
        type=str,
        default="",
        help=(
            "Comma-separated tickers. If omitted, uses COMPETITIVE_BACKTEST_SYMBOLS "
            "when set; otherwise the same resolution as competitive backtest "
            "(COMPETITIVE_BACKTEST_SYMBOL_MODE, default universe)."
        ),
    )
    p.add_argument(
        "--start",
        type=str,
        default="",
        help=(
            "Clip plotted series from this instant (YYYY-MM-DD or ISO UTC). "
            "Only filters bars already in the DB — does not load earlier history."
        ),
    )
    p.add_argument(
        "--end",
        type=str,
        default="",
        help=(
            "Clip plotted series through this instant. "
            "Only filters bars already in the DB."
        ),
    )
    p.add_argument(
        "--sources",
        type=str,
        default="",
        help=(
            "Comma-separated ``source`` values to include (e.g. yfinance,alpaca). "
            "If omitted, all sources in ``prices_hourly`` / hourly ``prices`` are used."
        ),
    )
    p.add_argument("--dpi", type=int, default=120, help="PNG resolution")
    p.add_argument(
        "--fig-width",
        type=float,
        default=14.0,
        help="Figure width in inches",
    )
    p.add_argument(
        "--fig-height",
        type=float,
        default=5.0,
        help="Figure height in inches",
    )
    p.add_argument(
        "--pdf",
        action="store_true",
        help="Also write competitive_hourly_all.pdf (one page per symbol)",
    )
    args = p.parse_args(list(argv) if argv is not None else None)

    cfg = load_config()
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))

    syms = _parse_symbols_override(args.symbols.strip() or None)
    symbol_source = "cli" if syms else None
    if not syms and (cfg.get("competitive_backtest_symbols") or "").strip():
        syms = _parse_competitive_backtest_symbols(cfg)
        symbol_source = "COMPETITIVE_BACKTEST_SYMBOLS"

    con: Any = None
    sym_err: Optional[str] = None
    if not syms:
        con = connect(db)
        init_db(con)
        syms, sym_err = resolve_backtest_symbols(cfg, con)
        symbol_source = "resolve_backtest_symbols"

    if not syms:
        if con is not None:
            con.close()
        print(
            "No symbols to plot. "
            + (f"({sym_err}) " if sym_err else "")
            + "Set COMPETITIVE_BACKTEST_SYMBOLS, fix COMPETITIVE_BACKTEST_SYMBOL_MODE / universe, "
            "or pass e.g. --symbols AAPL,MSFT",
            file=sys.stderr,
        )
        return 2

    out_dir = args.output_dir
    if out_dir is None:
        out_dir = Path(DATA_DIR) / "plots" / "competitive_hourly_db"
    out_dir.mkdir(parents=True, exist_ok=True)

    start, end = _parse_dt_bounds(
        args.start.strip() or None, args.end.strip() or None
    )
    source_filter = [
        x.strip()
        for x in (args.sources or "").split(",")
        if x and str(x).strip()
    ]
    source_filter_opt: Optional[List[str]] = source_filter if source_filter else None

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    if con is None:
        con = connect(db)
        init_db(con)

    summary: Dict[str, Any] = {
        "db_path": str(db.resolve()),
        "interval": "1h",
        "source_note": "adj_close when present else close (agent_db.get_full_adj_close_series_asc)",
        "sources_filter": source_filter_opt,
        "symbol_source": symbol_source,
        "competitive_backtest_symbol_mode": cfg.get("competitive_backtest_symbol_mode"),
        "symbols_requested": list(syms),
        "clip_start": start.isoformat() if start else None,
        "clip_end": end.isoformat() if end else None,
        "per_symbol": {},
    }

    pdf_path = out_dir / "competitive_hourly_all.pdf"
    pdf: Any = None

    html_rows: List[str] = []

    try:
        for sym in syms:
            raw_series = get_full_adj_close_series_asc(
                con, sym, "1h", sources=source_filter_opt
            )
            series = _clip_series(raw_series, start, end)
            db_first = raw_series[0][0] if raw_series else None
            db_last = raw_series[-1][0] if raw_series else None
            meta: Dict[str, Any] = {
                "n_bars": len(series),
                "n_bars_unclipped": len(raw_series),
                "db_first_ts_utc": db_first.isoformat() if db_first else None,
                "db_last_ts_utc": db_last.isoformat() if db_last else None,
                "first_ts_utc": series[0][0].isoformat() if series else None,
                "last_ts_utc": series[-1][0].isoformat() if series else None,
                "png": None,
                "error": None,
            }
            if start is not None and db_first is not None and db_first > start:
                print(
                    f"{sym}: hourly data in DB starts at {db_first.date()} (UTC); "
                    f"nothing exists before --start={start.date()}. "
                    "Backfill hourly history (e.g. yfinance / import) to extend the series.",
                    file=sys.stderr,
                )
            if end is not None and db_last is not None and db_last < end:
                print(
                    f"{sym}: hourly data in DB ends at {db_last.date()} (UTC), "
                    f"before --end={end.date()}.",
                    file=sys.stderr,
                )
            fn_base = _safe_filename(sym)
            png_name = f"{fn_base}.png"
            png_path = out_dir / png_name

            if not series:
                meta["error"] = "no_hourly_data"
                summary["per_symbol"][sym] = meta
                html_rows.append(
                    f"<section><h2>{sym}</h2><p><em>No hourly rows in DB for this symbol.</em></p></section>"
                )
                continue

            times = [t for t, _ in series]
            prices = [px for _, px in series]

            fig, ax = plt.subplots(
                figsize=(args.fig_width, args.fig_height), layout="tight"
            )
            ax.plot(
                times,
                prices,
                color="#1f77b4",
                linewidth=0.8,
                label="adj_close (or close)",
            )
            src_note = ""
            if source_filter_opt:
                src_note = f" | sources ∈ {{{', '.join(source_filter_opt)}}}"
            db_range = (
                f"Stored hourly in DB: {raw_series[0][0].date()} → {raw_series[-1][0].date()} "
                f"({len(raw_series)} bars){src_note}"
            )
            clip_note = ""
            if start is not None or end is not None:
                clip_note = (
                    f"\nClip request: "
                    f"{start.date() if start else '…'} → {end.date() if end else '…'} "
                    f"→ {len(series)} bars plotted"
                )
            ax.set_title(
                f"{sym} — hourly (adj/close)\n{db_range}{clip_note}",
                fontsize=10,
            )
            ax.set_xlabel("Time (UTC)")
            ax.set_ylabel("Price (USD)")
            ax.grid(True, alpha=0.25)
            ax.xaxis.set_major_formatter(
                mdates.ConciseDateFormatter(ax.xaxis.get_major_locator())
            )
            ax.legend(loc="upper left", fontsize=8)
            fig.text(
                0.01,
                0.01,
                f"DB: {db}",
                fontsize=7,
                color="gray",
                ha="left",
                va="bottom",
            )
            fig.savefig(png_path, dpi=args.dpi)
            if args.pdf:
                if pdf is None:
                    pdf = PdfPages(pdf_path)
                pdf.savefig(fig)
            plt.close(fig)

            meta["png"] = png_name
            summary["per_symbol"][sym] = meta
            html_rows.append(
                f'<section><h2 id="{fn_base}">{sym}</h2>'
                f"<p>n={len(series)} &nbsp; {meta['first_ts_utc']} → {meta['last_ts_utc']}</p>"
                f'<img src="{png_name}" alt="{sym} hourly" style="max-width:100%;height:auto;border:1px solid #ccc"/>'
                f"</section>"
            )
    finally:
        if pdf is not None:
            pdf.close()

    con.close()

    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    index_html = out_dir / "index.html"
    index_html.write_text(
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"/>"
        "<title>Hourly prices (DB) — competitive symbols</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:1200px;margin:24px auto;padding:0 16px}"
        "section{margin-bottom:48px}h1{font-size:1.25rem}</style></head><body>\n"
        f"<h1>Hourly prices from DB</h1><p>{summary['source_note']}</p>"
        + (
            f"<p><strong>Sources filter:</strong> {', '.join(summary['sources_filter'])}</p>"
            if summary.get("sources_filter")
            else ""
        )
        + f"<p><code>{summary['db_path']}</code></p>"
        "<nav><strong>Jump:</strong> "
        + " · ".join(
            f'<a href="#{_safe_filename(s)}">{s}</a>'
            for s in syms
            if summary["per_symbol"].get(s, {}).get("n_bars", 0) > 0
        )
        + "</nav>\n"
        + "\n".join(html_rows)
        + "\n</body></html>",
        encoding="utf-8",
    )

    print(f"Wrote {len(syms)} symbol outputs under {out_dir.resolve()}")
    print(f"  summary: {summary_path}")
    print(f"  index:   {index_html}")
    if args.pdf and pdf_path.is_file():
        print(f"  pdf:     {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
