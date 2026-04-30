#!/usr/bin/env python3
"""
Agent CLI: ingest → extract → prices → memory → research → backtest.

Examples:
  python -m telegram_agent.agent ingest --mode incremental
  python -m telegram_agent.agent ingest --mode backfill --days 365
  python -m telegram_agent.agent extract
  python -m telegram_agent.agent extract --dry-run
  python -m telegram_agent.agent universe-preprocess
  python -m telegram_agent.agent universe-preprocess --dry-run
  python -m telegram_agent.agent universe-preprocess --backfill-from 2026-03-24 --backfill-to 2026-03-24 --dry-run
  python -m telegram_agent.agent clear-universe-preprocess
  python -m telegram_agent.agent clear-extract
  python -m telegram_agent.agent clear-ingest
  python -m telegram_agent.agent clear-research
  python -m telegram_agent.agent prices --mode backfill
  python -m telegram_agent.agent prices --mode backfill --intervals 1d
  python -m telegram_agent.agent memory
  python -m telegram_agent.agent research
  python -m telegram_agent.agent research --dry-run
  python -m telegram_agent.agent research --dry-run --dry-run-out /tmp/research_prompt.txt
  python -m telegram_agent.agent research --backfill-from 2024-01-01 --backfill-to 2024-01-31
  python -m telegram_agent.agent research --backfill-from 2024-01-01 --backfill-to 2024-01-02 --dry-run
  python -m telegram_agent.agent research --backfill-from 2024-06-01 --backfill-to 2024-06-01 --clear-memories
  python -m telegram_agent.agent backtest
  python -m telegram_agent.agent test-suggestions
  python -m telegram_agent.agent run-all --mode incremental
  python -m telegram_agent.agent narrative --horizon daily
  python -m telegram_agent.agent narrative --horizon all
  python -m telegram_agent.agent competitive-bots --cadence daily
  python -m telegram_agent.agent competitive-bots --backtest
  python -m telegram_agent.agent competitive-bots --backtest --backtest-symbols env
  python -m telegram_agent.agent competitive-bots --backtest --backtest-per-ticker
  python -m telegram_agent.competitive_coverage
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("telegram_agent.agent")


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")

    from telegram_agent.config import DATA_DIR, load_config
    from telegram_agent.ingest import run_ingest
    from telegram_agent.extract_pipeline import run_extract, estimate_extract_llm_cost
    from telegram_agent.agent_db import (
        connect,
        init_db,
        clear_news_mentions,
        clear_news_items,
        clear_ingest_kv_cursors,
        clear_orphan_instruments,
        clear_research_outputs,
        clear_universe_preprocess,
    )
    from telegram_agent.prices import run_prices
    from telegram_agent.agent_memory import run_memory_update
    from telegram_agent.agent_research import (
        run_research,
        run_research_backfill,
        estimate_research_dry_run,
        estimate_research_dry_run_for_calendar_day,
        estimate_research_backfill_dry_run,
        write_research_dry_run_prompt_file,
    )
    from telegram_agent.backtest import print_backtest_report
    from telegram_agent.agent_tester import (
        run_suggestion_tests,
        print_tester_summary,
        print_strategy_aggregate,
    )
    from telegram_agent.narrative_tracker import generate_horizon_report, generate_all_horizons

    p = argparse.ArgumentParser(description="Market analysis agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="Fetch news into agent DB")
    pi.add_argument("--mode", choices=["incremental", "backfill"], default="incremental")
    pi.add_argument("--days", type=int, default=None, help="Backfill window in days (default from config)")
    pi.add_argument("--sources", choices=["all", "rss", "telegram", "api"], default=None)
    pi.add_argument(
        "--dry-run",
        action="store_true",
        help="No network calls: print which sources/date-ranges/tickers would be fetched (based on DB coverage + config)",
    )
    pi.add_argument(
        "--max-priority",
        type=int,
        default=None,
        help="Only use universe investments with priority <= this value (0..3). Default from env MAX_PRIORITY (3 = no filtering).",
    )
    pi.add_argument(
        "--force",
        action="store_true",
        help="Override duplication check and fetch even if DB already has coverage for the window",
    )
    pi.add_argument(
        "--spy_symbols",
        action="store_true",
        help="Use SP500_SYMBOLS from repo .env as the symbol universe (overrides API tickers/universe for this run).",
    )

    pe = sub.add_parser("extract", help="Extract tickers from news into news_mentions")
    pe.add_argument(
        "--dry-run",
        action="store_true",
        help="Estimate LLM token usage and USD (no API calls; uses same batching as extract)",
    )
    pe.add_argument(
        "--limit",
        type=int,
        default=2000,
        help="Max news rows without mentions to process (default 2000)",
    )
    pe.add_argument(
        "--max-priority",
        type=int,
        default=None,
        help="Only allow extracted symbols that are in the universe with priority <= this value (0..3). Default from env MAX_PRIORITY.",
    )

    pu = sub.add_parser(
        "universe-preprocess",
        help="Cheap LLM: tag each news row with tickers from the symbol universe; fills symbol_news_linkage",
    )
    pu.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max news rows to process this run (default: config NEWS_UNIVERSE_PREPROCESS_MAX_ROWS)",
    )
    pu.add_argument(
        "--max-priority",
        type=int,
        default=None,
        help="Universe membership uses priority <= this (0..3). Default MAX_PRIORITY.",
    )
    pu.add_argument(
        "--backfill-from",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="UTC calendar day: only preprocess news with ts_utc on or after this day (inclusive)",
    )
    pu.add_argument(
        "--backfill-to",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="UTC calendar day: end of range inclusive (default: same as --backfill-from)",
    )
    pu.add_argument(
        "--dry-run",
        action="store_true",
        help="No API calls: estimate batches, tokens, USD, and write sample prompts to --dry-run-out",
    )
    pu.add_argument(
        "--dry-run-out",
        type=str,
        default=None,
        metavar="PATH",
        help="Path for preprocess dry-run export (default: telegram_agent/data/universe_preprocess_dry_run.txt)",
    )
    pu.add_argument(
        "--model",
        type=str,
        default=None,
        choices=[
            # Match research agent choices
            # Long-context / strongest reasoning
            "anthropic/claude-opus-4.6",
            "anthropic/claude-sonnet-4.5",
            # Popular / strong general reasoning
            "anthropic/claude-3.5-sonnet",
            # Fast/cheap baseline
            "anthropic/claude-3-haiku",
            # Legacy 200k context
            "anthropic/claude-2.1",
            # Additional cheap options
            "openai/gpt-4o-mini",
            # Gemini 2.5 (common IDs across providers)
            "google/gemini-2.5-flash",
            "google/gemini-2.5-pro",
        ],
        help="Override the universe-preprocessor LLM model for this run (provider-specific model id).",
    )

    sub.add_parser(
        "clear-universe-preprocess",
        help="Remove symbol_news_linkage + universe_preprocess mentions; reset preprocess timestamps on news",
    )

    pc = sub.add_parser(
        "clear-extract",
        help="Delete all extracted mentions (news_mentions); optionally prune orphan instruments",
    )
    pc.add_argument(
        "--keep-instruments",
        action="store_true",
        help="Only clear news_mentions; keep instruments and liquidity_cache rows",
    )

    ping = sub.add_parser(
        "clear-ingest",
        help="Delete all ingested news (news_items); cascades mentions; resets ingest cursors",
    )
    ping.add_argument(
        "--keep-instruments",
        action="store_true",
        help="Do not prune orphan instruments or liquidity_cache (same as clear-extract)",
    )
    ping.add_argument(
        "--keep-cursors",
        action="store_true",
        help="Keep ingest:last_run_ts / ingest:last_backfill_ts in kv_state",
    )

    pcr = sub.add_parser(
        "clear-research",
        help="Delete all recommendations and memory snapshots (reset research + agent memory DB state)",
    )
    pcr.add_argument(
        "--keep-instruments",
        action="store_true",
        help="Do not prune orphan instruments or liquidity_cache after clearing recommendations",
    )

    pp = sub.add_parser("prices", help="Fetch yfinance prices for mentioned symbols")
    pp.add_argument("--mode", choices=["incremental", "backfill"], default="incremental")
    pp.add_argument("--days", type=int, default=400, help="History days for backfill")
    pp.add_argument(
        "--force",
        action="store_true",
        help="Override duplication check and refetch even if price window looks populated",
    )
    pp.add_argument(
        "--reverse",
        action="store_true",
        help="Backfill in reverse chronological order (newest day to oldest day)",
    )
    pp.add_argument(
        "--max-priority",
        type=int,
        default=None,
        help="Only fetch prices for universe investments with priority <= this value (0..3). Default from env MAX_PRIORITY.",
    )
    pp.add_argument(
        "--priority",
        type=int,
        default=None,
        metavar="N",
        help="Only fetch prices for symbols with this exact universe priority (0..3). Requires JSON universe with "
        "per-symbol priority fields. Applied after --max-priority.",
    )
    pp.add_argument(
        "--intervals",
        type=str,
        default="1d,1h,1m",
        metavar="LIST",
        help="Comma-separated yfinance intervals: 1d (daily → prices), 1h (→ prices_hourly), 1m (→ prices_minute). "
        "Default: all three. Example: --intervals 1d for daily only.",
    )
    pp.add_argument(
        "--symbols",
        type=str,
        default=None,
        metavar="LIST",
        help=(
            "Optional comma-separated canonical symbols to fetch prices for (overrides universe/news_mentions for this run). "
            "Example: --symbols TSLA,AAPL,BTC"
        ),
    )
    pp.add_argument(
        "--spy_symbols",
        action="store_true",
        help="Use SP500_SYMBOLS from repo .env as the symbol universe for this run.",
    )

    sub.add_parser("memory", help="Update rolling macro/micro memory (LLM)")

    prs = sub.add_parser("research", help="Run opportunity research (LLM) and store recommendations")
    prs.add_argument(
        "--dry-run",
        action="store_true",
        help="No API calls: with backfill dates, cost estimate for the range + optional prompt export; "
        "without backfill, 1x cost/stats and full prompts to a file",
    )
    prs.add_argument(
        "--model",
        type=str,
        default=None,
        choices=[
            # Long-context / strongest reasoning
            "anthropic/claude-opus-4.6",
            "anthropic/claude-sonnet-4.5",
            # Popular / strong general reasoning
            "anthropic/claude-3.5-sonnet",
            # Fast/cheap baseline
            "anthropic/claude-3-haiku",
            # Legacy 200k context
            "anthropic/claude-2.1",
        ],
        help="Override the research LLM model for this run (OpenRouter model id).",
    )
    prs.add_argument(
        "--min-confidence",
        type=float,
        default=None,
        help="Minimum suggestion confidence (0..1) required to persist/return suggestions. Default from env AGENT_RESEARCH_MIN_CONFIDENCE (currently 0.75).",
    )
    prs.add_argument(
        "--dry-run-out",
        type=str,
        default=None,
        metavar="PATH",
        help="Path for full system+user prompts (default: telegram_agent/data/research_dry_run_prompt.txt)",
    )
    prs.add_argument(
        "--max-num-ofnews",
        type=int,
        default=None,
        help="Max number of news rows fetched and included in the research prompt (applies to live + backfill + dry-run). Overrides env MAX_NUM_OFNEWS.",
    )
    prs.add_argument(
        "--max-priority",
        type=int,
        default=None,
        help="Only use universe investments with priority <= this value (0..3) for research context/symbol lists. Default from env MAX_PRIORITY.",
    )
    prs.add_argument(
        "--research-max-priority",
        type=int,
        default=1,
        help="Research-only universe priority override (priority <= this). If set, research filters news/linkage/universe to this subset without changing other commands' MAX_PRIORITY.",
    )
    prs.add_argument(
        "--backfill-from",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="UTC calendar day: start daily research backfill (one LLM call per day)",
    )
    prs.add_argument(
        "--backfill-to",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="UTC calendar day: end backfill inclusive (default: same as --backfill-from)",
    )
    prs.add_argument(
        "--clear-memories",
        action="store_true",
        help="Before backfill: delete all memory rows so replay starts empty (destructive)",
    )
    prs.add_argument(
        "--backfill-dry-run",
        action="store_true",
        help="With --backfill-from/--backfill-to: estimate total LLM cost for the range (no API calls)",
    )

    sub.add_parser("backtest", help="Print backtest JSON for stored recommendations")

    pts = sub.add_parser(
        "test-suggestions",
        help="Strategy test agent: evaluate stored legs in recommendations vs prices; per-row tester + aggregate metrics",
    )
    pts.add_argument(
        "--asof",
        type=str,
        default=None,
        metavar="ISO8601",
        help="Evaluate as-of this UTC timestamp (use for backfill to avoid future leakage). Default: now.",
    )
    pts.add_argument(
        "--concluded-only",
        action="store_true",
        help="Only test legs whose execute_review_utc is earlier than the as-of time.",
    )
    pts.add_argument(
        "--summary",
        action="store_true",
        help="Print id/symbol/tester JSON only (no DB update)",
    )
    pts.add_argument(
        "--show-aggregate",
        action="store_true",
        help="After a normal run, print full aggregate metrics JSON from kv_state",
    )
    pts.add_argument(
        "--print-aggregate-only",
        action="store_true",
        help="Skip per-leg updates; only print aggregate metrics JSON from the last test run",
    )

    pn = sub.add_parser("narrative", help="Generate narrative tracker report(s)")
    pn.add_argument("--horizon", choices=["hourly", "daily", "weekly", "monthly", "annual", "all"], default="daily")

    pa = sub.add_parser("run-all", help="ingest → extract → prices → memory → research")
    pa.add_argument("--mode", choices=["incremental", "backfill"], default="incremental")
    pa.add_argument("--days", type=int, default=None)
    pa.add_argument("--sources", choices=["all", "rss", "telegram"], default=None)
    pa.add_argument(
        "--max-priority",
        type=int,
        default=None,
        help="Only use universe investments with priority <= this value (0..3) across ingest/extract/prices/memory/research. Default from env MAX_PRIORITY.",
    )
    pa.add_argument("--skip-memory", action="store_true")
    pa.add_argument("--skip-research", action="store_true")

    po = sub.add_parser(
        "orchestrate",
        help="Daily orchestrator: ingest → prices → preprocess → test concluded legs → research (supports backfill)",
    )
    po.add_argument(
        "--backfill-from",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="UTC calendar day: start backfill orchestration (inclusive)",
    )
    po.add_argument(
        "--backfill-to",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="UTC calendar day: end backfill orchestration (inclusive; default: same as --backfill-from)",
    )
    po.add_argument(
        "--cadence",
        type=int,
        default=1,
        metavar="N",
        help="Backfill only every Nth UTC calendar day from --backfill-from (1=every day). "
        "Example: --cadence 3 runs on start, start+3d, start+6d, ... while <= --backfill-to.",
    )
    po.add_argument(
        "--max-priority",
        type=int,
        default=None,
        help="Only use universe investments with priority <= this value (0..3). Default MAX_PRIORITY.",
    )

    pc = sub.add_parser(
        "competitive-bots",
        help="Three systematic strategies on P0/P1 universe; evaluate with test-suggestions; store + optional Telegram",
    )
    pc.add_argument(
        "--cadence",
        type=str,
        default="manual",
        metavar="LABEL",
        help="Run label stored in DB (e.g. hourly, daily). Use the same string in cron/systemd.",
    )
    pc.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip Telegram publish to TARGET_CHANNEL",
    )
    pc.add_argument(
        "--backtest",
        action="store_true",
        help="Walk-forward backtest all 3 bots on each price interval present in the DB (1d/1h/1m/…); publish summary",
    )
    pc.add_argument(
        "--backtest-symbols",
        type=str,
        choices=["universe", "env", "p0-full-coverage"],
        default="universe",
        help="With --backtest: universe=default priority-filtered list; env=COMPETITIVE_BACKTEST_SYMBOLS only; "
        "p0-full-coverage=priority-0 symbols meeting FULL_COVERAGE_MIN_BARS_* for 1d+1h+1m.",
    )
    pc.add_argument(
        "--backtest-per-ticker",
        action="store_true",
        help="With --backtest: add by_ticker stats (each of 3 bots) for every symbol in the interval's cross-section.",
    )

    args = p.parse_args()
    cfg = load_config()
    if getattr(args, "max_priority", None) is not None:
        cfg["max_priority"] = int(args.max_priority)

    def _apply_sp500(cfg_in: dict) -> dict:
        from telegram_agent.symbol_universe import sp500_symbols_from_env

        sp = sp500_symbols_from_env()
        out = dict(cfg_in)
        out["symbol_universe_enabled"] = True
        out["symbol_universe_env"] = ",".join(sp)
        out["symbol_universe_path"] = ""  # env list takes precedence; make intent explicit
        out["api_use_symbol_universe"] = True
        # For API news providers that require explicit symbols, set them directly too.
        out["finnhub_symbols"] = list(sp)
        out["alphavantage_tickers"] = list(sp)
        out["stocknewsapi_tickers"] = list(sp)
        return out

    if args.cmd == "ingest":
        if getattr(args, "spy_symbols", False):
            cfg = _apply_sp500(cfg)
        n = asyncio.run(
            run_ingest(
                cfg,
                mode=args.mode,
                source_mode=args.sources,
                backfill_days=args.days,
                force=args.force,
                dry_run=bool(args.dry_run),
            )
        )
        logger.info("Ingest done: %s rows", n)
        return

    if args.cmd == "extract":
        if args.dry_run:
            est = estimate_extract_llm_cost(cfg, limit=args.limit)
            print("Extract LLM cost estimate (dry-run, no API calls)")
            print(f"  Pending news rows (no mentions yet): {est['pending_news_rows']}")
            if not est.get("use_llm"):
                print(f"  {est.get('note', '')}")
                return
            print(f"  Model: {est['model']}")
            print(f"  Batch size: {est['batch_size']}  Batches: {est['batches']}")
            print(f"  Input tokens (est): {est['input_tokens']}")
            print(f"  Output tokens (est, max_tokens budget per batch): {est['output_tokens_est']}")
            print(f"  Total USD (est): ${est['total_usd']:.4f}")
            print(f"  {est.get('note', '')}")
            return
        n = run_extract(cfg, limit=args.limit)
        logger.info("Extract done: %s mention rows", n)
        return

    if args.cmd == "universe-preprocess":
        from telegram_agent.news_universe_preprocess import (
            estimate_universe_preprocess_dry_run,
            run_news_universe_preprocess,
            write_universe_preprocess_dry_run_file,
            utc_range_for_backfill_days,
        )

        cfg_u = dict(cfg)
        if getattr(args, "max_priority", None) is not None:
            cfg_u["max_priority"] = int(args.max_priority)
        if getattr(args, "model", None):
            cfg_u["news_universe_preprocess_model"] = str(args.model).strip()

        db = Path(cfg_u.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
        con = connect(db)
        init_db(con)

        min_ts = None
        max_ts_excl = None
        max_ts_inc = None
        if args.backfill_from:
            fd = date.fromisoformat(args.backfill_from)
            td = date.fromisoformat(args.backfill_to) if args.backfill_to else fd
            min_ts, max_ts_excl = utc_range_for_backfill_days(fd, td)
        else:
            if args.backfill_to:
                logger.error("--backfill-to requires --backfill-from")
                con.close()
                sys.exit(1)
            max_ts_inc = datetime.now(timezone.utc)

        common_kw = dict(
            min_ts_utc_inclusive=min_ts,
            max_ts_utc_exclusive=max_ts_excl,
            max_ts_utc_inclusive=max_ts_inc,
        )

        if args.dry_run:
            rep = estimate_universe_preprocess_dry_run(cfg_u, con, **common_kw)
            out_path = (
                Path(args.dry_run_out).expanduser()
                if args.dry_run_out
                else DATA_DIR / "universe_preprocess_dry_run.txt"
            )
            write_universe_preprocess_dry_run_file(rep, out_path)
            con.close()
            print("Universe preprocess LLM dry-run (no API calls)")
            if rep.get("skipped"):
                print(f"  Skipped: {rep.get('reason')}")
                if rep.get("note"):
                    print(f"  Note: {rep['note']}")
            print(f"  Pending in scope: {rep.get('pending_in_scope', 0)}")
            print(f"  Model: {rep.get('model')}  provider: {rep.get('provider')}")
            print(
                f"  Batch size: {rep.get('batch_size')}  "
                f"LLM calls (est): {rep.get('llm_calls_est')}"
            )
            print(
                f"  Input tokens (est): total~{rep.get('input_tokens_total_est', 0)}  "
                f"per batch~{rep.get('input_tokens_per_batch_est', 0)}"
            )
            print(
                f"  Output tokens (est): total~{rep.get('output_tokens_est_total', 0)}  "
                f"per batch~{rep.get('output_tokens_est_per_batch', 0)}"
            )
            print(f"  Total USD (est, typical): ${rep.get('total_usd_typical', 0):.4f}")
            print(f"  Scope: {rep.get('scope_filter')}")
            print(
                f"  Prompt size (sample batch): system {rep.get('system_chars', 0)} chars, "
                f"user {rep.get('user_chars', 0)} chars, total {rep.get('total_chars', 0)} chars"
            )
            print(f"  Full sample prompts written to: {out_path.resolve()}")
            if rep.get("note") and not rep.get("skipped"):
                print(f"  {rep['note']}")
            return

        out = run_news_universe_preprocess(cfg_u, con, limit=args.limit, **common_kw)
        con.close()
        print(json.dumps(out, indent=2))
        return

    if args.cmd == "clear-universe-preprocess":
        db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
        con = connect(db)
        init_db(con)
        n_link, n_m = clear_universe_preprocess(con)
        con.close()
        print(
            f"Cleared {n_link} symbol_news_linkage row(s), {n_m} universe_preprocess mention row(s); "
            "preprocess timestamps reset on news_items."
        )
        return

    if args.cmd == "clear-extract":
        db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
        con = connect(db)
        init_db(con)
        nm = clear_news_mentions(con)
        extra = ""
        if not args.keep_instruments:
            n_inst, n_liq = clear_orphan_instruments(con)
            extra = f"; removed {n_inst} orphan instrument(s), {n_liq} liquidity_cache row(s)"
        con.close()
        logger.info("Cleared %s news_mention row(s)%s", nm, extra)
        print(f"Cleared {nm} news_mention row(s){extra}")
        return

    if args.cmd == "clear-ingest":
        db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
        con = connect(db)
        init_db(con)
        n_news = clear_news_items(con)
        kv_n = 0
        if not args.keep_cursors:
            kv_n = clear_ingest_kv_cursors(con)
        extra = ""
        if not args.keep_instruments:
            n_inst, n_liq = clear_orphan_instruments(con)
            extra = f"; removed {n_inst} orphan instrument(s), {n_liq} liquidity_cache row(s)"
        con.close()
        if args.keep_cursors:
            cur_msg = "kept ingest cursors"
        else:
            cur_msg = f"reset {kv_n} ingest cursor key(s)"
        logger.info(
            "Cleared %s news_items row(s); %s%s",
            n_news,
            cur_msg,
            extra,
        )
        print(
            f"Cleared {n_news} news_items row(s) (news_mentions cascade); {cur_msg}{extra}"
        )
        return

    if args.cmd == "clear-research":
        db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
        con = connect(db)
        init_db(con)
        n_mem, n_rec = clear_research_outputs(con)
        extra = ""
        if not args.keep_instruments:
            n_inst, n_liq = clear_orphan_instruments(con)
            extra = f"; removed {n_inst} orphan instrument(s), {n_liq} liquidity_cache row(s)"
        con.close()
        logger.info(
            "Cleared %s memory snapshot(s) and %s recommendation(s)%s",
            n_mem,
            n_rec,
            extra,
        )
        print(
            f"Cleared {n_mem} memory row(s) and {n_rec} recommendation row(s){extra}. "
            "You can run `memory` and/or `research` again from a clean state."
        )
        return

    if args.cmd == "prices":
        cfg2 = dict(cfg)
        if getattr(args, "spy_symbols", False):
            cfg2 = _apply_sp500(cfg2)
        cfg2["prices_force"] = bool(args.force)
        cfg2["prices_backfill_reverse"] = bool(getattr(args, "reverse", False))
        if getattr(args, "symbols", None):
            syms = []
            for raw in str(args.symbols).split(","):
                s = str(raw or "").strip().upper()
                while s.startswith("$") or s.startswith("#"):
                    s = s[1:]
                if s:
                    syms.append(s)
            # De-dupe preserving order
            out = []
            seen = set()
            for s in syms:
                if s and s not in seen:
                    out.append(s)
                    seen.add(s)
            cfg2["prices_symbols"] = out
        if getattr(args, "priority", None) is not None:
            p = int(args.priority)
            if p < 0 or p > 3:
                logger.error("--priority must be between 0 and 3")
                sys.exit(1)
            cfg2["prices_priority"] = p
        try:
            run_prices(
                cfg2,
                mode=args.mode,
                days=int(args.days),
                intervals=str(args.intervals or "1d,1h,1m"),
            )
        except ValueError as e:
            logger.error("%s", e)
            sys.exit(1)
        return

    if args.cmd == "memory":
        run_memory_update(cfg)
        return

    if args.cmd == "research":
        if args.model:
            cfg = dict(cfg)
            cfg["agent_research_model"] = str(args.model).strip()
        if getattr(args, "research_max_priority", None) is not None:
            cfg = dict(cfg)
            cfg["agent_research_max_priority"] = int(args.research_max_priority)
        if getattr(args, "min_confidence", None) is not None:
            cfg = dict(cfg)
            cfg["agent_research_min_confidence"] = float(args.min_confidence)
        if args.backfill_from:
            start_d = date.fromisoformat(args.backfill_from)
            end_d = date.fromisoformat(args.backfill_to) if args.backfill_to else start_d
            if end_d < start_d:
                logger.error("--backfill-to must be >= --backfill-from")
                sys.exit(1)
            if args.backfill_dry_run or args.dry_run:
                cfg_eff = dict(cfg)
                if args.max_num_ofnews is not None:
                    cfg_eff["agent_research_max_num_ofnews"] = int(args.max_num_ofnews)
                est = estimate_research_backfill_dry_run(cfg_eff, start=start_d, end=end_d)
                print("Research backfill cost estimate (no API calls)")
                print(f"  Range: {est['sample_day']} sample day; {est['days_in_range']} day(s) [{start_d} .. {end_d}]")
                print(f"  LLM calls (est): {est['llm_calls_total_est']}  model={est['model']}")
                print(
                    f"  Per day: input_tokens~{est['per_day_input_tokens_est']}  "
                    f"USD typical~${est['per_day_total_usd_typical']:.4f}  worst~${est['per_day_total_usd_worst']:.4f}"
                )
                print(
                    f"  Range total USD (est): typical~${est['range_total_usd_typical']:.4f}  "
                    f"worst~${est['range_total_usd_worst']:.4f}"
                )
                print(f"  Sample day news rows: {est['news_rows_sample_day']}")
                if args.dry_run:
                    rep = estimate_research_dry_run_for_calendar_day(cfg_eff, start_d)
                    out_path = (
                        Path(args.dry_run_out).expanduser()
                        if args.dry_run_out
                        else DATA_DIR / "research_dry_run_prompt.txt"
                    )
                    write_research_dry_run_prompt_file(rep, out_path)
                    print("Research LLM dry-run (no API calls)")
                    print(f"  Full prompts (sample day = range start) written to: {out_path.resolve()}")
                return
            out = run_research_backfill(
                cfg,
                start=start_d,
                end=end_d,
                clear_memories=args.clear_memories,
            )
            print(
                f"Backfill done: {out['days']} day(s), {out['recommendations']} new recommendation(s), "
                f"{out['days_with_zero_new_recs']} day(s) with zero new recs"
            )
            return
        if args.dry_run:
            cfg_eff = dict(cfg)
            if args.max_num_ofnews is not None:
                cfg_eff["agent_research_max_num_ofnews"] = int(args.max_num_ofnews)
            rep = estimate_research_dry_run(cfg_eff)
            out_path = (
                Path(args.dry_run_out).expanduser()
                if args.dry_run_out
                else DATA_DIR / "research_dry_run_prompt.txt"
            )
            write_research_dry_run_prompt_file(rep, out_path)
            print("Research LLM dry-run (no API calls)")
            print(f"  Full prompts written to: {out_path.resolve()}")
            print(f"  LLM calls: {rep['llm_calls']}  ({rep['endpoint']})")
            print(f"  Model: {rep['model']}")
            print(
                f"  temperature={rep['temperature']}  max_output_tokens={rep['max_output_tokens']}"
            )
            print(f"  Input tokens (est): {rep['input_tokens']}  (~${rep['input_usd']:.4f})")
            out_worst_usd = rep["total_usd_worst"] - rep["input_usd"]
            print(
                f"  Output tokens (est): typical={rep['assumed_output_tokens']} "
                f"(~${rep['output_usd_typical']:.4f})  worst-case={rep['max_output_tokens']} "
                f"(~${out_worst_usd:.4f})"
            )
            print(
                f"  Total USD (est): typical ~${rep['total_usd_typical']:.4f}  "
                f"worst ~${rep['total_usd_worst']:.4f}"
            )
            st = rep["stats"]
            print("  Context stats:")
            print(f"    news rows (prompt window, fetched): {st['news_rows_in_prompt_window']}")
            print(f"    news lines in prompt (max 200): {st['news_lines_in_prompt']}")
            print(
                f"    memory present: {st['memory_snapshot_present']}  "
                f"structured memory chars in prompt: {st['structured_memory_chars_in_prompt']}"
            )
            print(
                f"    price-context symbols: {st['price_context_symbols']}  "
                f"symbol_universe configured: {st['symbol_universe_configured']}"
            )
            print(
                f"  Prompt size: system {rep['system_chars']} chars, user {rep['user_chars']} chars, "
                f"total {rep['total_chars']} chars"
            )
            print(f"  {rep['note']}")
            return
        run_research(cfg)
        return

    if args.cmd == "backtest":
        print_backtest_report(cfg)
        return

    if args.cmd == "test-suggestions":
        if args.print_aggregate_only:
            print_strategy_aggregate(cfg)
            return
        if args.summary:
            print_tester_summary(cfg)
        else:
            asof = None
            if getattr(args, "asof", None):
                try:
                    asof = datetime.fromisoformat(str(args.asof).replace("Z", "+00:00"))
                except Exception:
                    asof = None
            n = run_suggestion_tests(cfg, asof_utc=asof, concluded_only=bool(args.concluded_only))
            logger.info("test-suggestions done: %s row(s) updated", n)
            if args.show_aggregate:
                print_strategy_aggregate(cfg)
        return

    if args.cmd == "narrative":
        if args.horizon == "all":
            out = generate_all_horizons(cfg)
            for k in ("hourly", "daily", "weekly", "monthly", "annual"):
                print("\n" + "=" * 60)
                print(out.get(k, ""))
        else:
            print(generate_horizon_report(cfg, args.horizon))
        return

    if args.cmd == "run-all":
        asyncio.run(
            run_ingest(
                cfg,
                mode=args.mode,
                source_mode=args.sources,
                backfill_days=args.days,
            )
        )
        run_extract(cfg)
        from telegram_agent.news_universe_preprocess import run_news_universe_preprocess

        dbp = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
        conp = connect(dbp)
        init_db(conp)
        run_news_universe_preprocess(
            cfg, conp, max_ts_utc_inclusive=datetime.now(timezone.utc)
        )
        conp.close()
        bd = int(args.days or int(cfg.get("agent_backfill_days", 365)) + 30)
        run_prices(cfg, mode=args.mode, days=bd, intervals="1d,1h,1m")
        if not args.skip_memory:
            run_memory_update(cfg)
        if not args.skip_research:
            run_research(cfg)
        logger.info("run-all finished")
        return

    if args.cmd == "competitive-bots":
        from telegram_agent.research_publish import (
            format_competitive_backtest_telegram_message,
            format_competitive_telegram_message,
            publish_plain_to_target,
        )

        if getattr(args, "backtest", False):
            from telegram_agent.competitive_backtest import (
                run_competitive_backtest_all_intervals,
            )

            cfg_bt = dict(cfg)
            cfg_bt["competitive_backtest_symbol_mode"] = str(
                getattr(args, "backtest_symbols", "universe") or "universe"
            )
            if getattr(args, "backtest_per_ticker", False):
                cfg_bt["competitive_backtest_per_ticker"] = True
            out = run_competitive_backtest_all_intervals(cfg_bt)
            print(json.dumps(out, indent=2, default=str))
            if (
                out.get("ok")
                and not args.no_publish
                and cfg.get("competitive_bots_publish", True)
            ):
                msg = format_competitive_backtest_telegram_message(out)
                publish_plain_to_target(cfg, msg)
            return

        from telegram_agent.competitive_bots import run_competitive_cycle

        out = run_competitive_cycle(cfg, cadence_label=str(args.cadence or "manual"))
        print(json.dumps(out, indent=2, default=str))
        if (
            out.get("ok")
            and not args.no_publish
            and cfg.get("competitive_bots_publish", True)
        ):
            msg = format_competitive_telegram_message(out)
            publish_plain_to_target(cfg, msg)
        return

    if args.cmd == "orchestrate":
        from telegram_agent.orchestrator import run_orchestration_backfill, run_orchestration_live
        from telegram_agent.orchestrator_logging import (
            append_orchestrator_stdout_summary,
            attach_orchestrator_file_logging,
        )

        orch_log_path = attach_orchestrator_file_logging()

        if args.backfill_from:
            start_d = date.fromisoformat(args.backfill_from)
            end_d = date.fromisoformat(args.backfill_to) if args.backfill_to else start_d
            cadence = int(args.cadence)
            if cadence < 1:
                logger.error("--cadence must be >= 1")
                sys.exit(1)
            if end_d < start_d:
                logger.error("--backfill-to must be >= --backfill-from")
                sys.exit(1)
            out = run_orchestration_backfill(
                cfg, start=start_d, end=end_d, cadence=cadence
            )
            append_orchestrator_stdout_summary(orch_log_path, out)
            print(json.dumps(out, indent=2, default=str))
            return
        if args.backfill_to:
            logger.error("--backfill-to requires --backfill-from")
            sys.exit(1)
        out = asyncio.run(run_orchestration_live(cfg))
        append_orchestrator_stdout_summary(orch_log_path, out.__dict__)
        print(json.dumps(out.__dict__, indent=2, default=str))
        return


if __name__ == "__main__":
    main()
