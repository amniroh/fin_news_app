#!/usr/bin/env python3
"""
Agent CLI: ingest → extract → prices → memory → research → backtest.

Examples:
  python -m telegram_agent.agent ingest --mode incremental
  python -m telegram_agent.agent ingest --mode backfill --days 365
  python -m telegram_agent.agent extract
  python -m telegram_agent.agent extract --dry-run
  python -m telegram_agent.agent clear-extract
  python -m telegram_agent.agent clear-ingest
  python -m telegram_agent.agent clear-research
  python -m telegram_agent.agent prices --mode backfill
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
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date
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
    )
    from telegram_agent.prices import backfill_all_mentioned, incremental_prices
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

    args = p.parse_args()
    cfg = load_config()
    if getattr(args, "max_priority", None) is not None:
        cfg["max_priority"] = int(args.max_priority)

    if args.cmd == "ingest":
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
        cfg2["prices_force"] = bool(args.force)
        cfg2["prices_backfill_reverse"] = bool(getattr(args, "reverse", False))
        if args.mode == "backfill":
            backfill_all_mentioned(cfg2, days=args.days)
        else:
            incremental_prices(cfg2)
        return

    if args.cmd == "memory":
        run_memory_update(cfg)
        return

    if args.cmd == "research":
        if args.model:
            cfg = dict(cfg)
            cfg["agent_research_model"] = str(args.model).strip()
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
            n = run_suggestion_tests(cfg)
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
        if args.mode == "backfill":
            backfill_all_mentioned(cfg, days=args.days or int(cfg.get("agent_backfill_days", 365)) + 30)
        else:
            incremental_prices(cfg)
        if not args.skip_memory:
            run_memory_update(cfg)
        if not args.skip_research:
            run_research(cfg)
        logger.info("run-all finished")


if __name__ == "__main__":
    main()
