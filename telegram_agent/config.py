"""Load configuration from environment and optional config file."""
import os
import json
import logging
from pathlib import Path
from typing import List

from .default_sources import (
    DEFAULT_RSS_FEEDS,
    DEFAULT_TELEGRAM_CHANNELS,
    DEFAULT_TWITTER_USERNAMES,
)

logger = logging.getLogger(__name__)

# Paths
ROOT = Path(__file__).resolve().parent
# Git repo root (parent of `telegram_agent/`). Env paths like SYMBOL_UNIVERSE_PATH=telegram_agent/foo.json
# resolve against this when relative, so the same .env works on EC2 and laptops.
REPO_ROOT = ROOT.parent
DATA_DIR = ROOT / "data"
SESSION_DIR = ROOT / "sessions"
CONFIG_JSON = ROOT / "config.json"

DATA_DIR.mkdir(exist_ok=True)
SESSION_DIR.mkdir(exist_ok=True)

# Default OpenRouter model id when LLM_MODEL is unset. Use a broadly routed slug (some
# anthropic/* defaults fail on certain OpenRouter provider routes with "invalid model").
DEFAULT_LLM_MODEL = "openai/gpt-4o-mini"


def _load_list_from_env(key: str, default: List[str]) -> List[str]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    return [x.strip() for x in raw.split(",") if x.strip()]


def _symbol_universe_enabled_from_env() -> bool:
    """
    Use fixed symbol universe when explicitly true, or when PATH / ENV list is set (implicit).
    SYMBOL_UNIVERSE_ENABLED=false disables even if PATH is set.
    """
    flag = os.getenv("SYMBOL_UNIVERSE_ENABLED", "").strip().lower()
    if flag == "false":
        return False
    if flag == "true":
        return True
    return bool(os.getenv("SYMBOL_UNIVERSE_PATH", "").strip()) or bool(
        os.getenv("SYMBOL_UNIVERSE_ENV", "").strip()
    )


def _resolve_repo_relative(path_str: str) -> str:
    """If path is relative, treat it as relative to REPO_ROOT (market_analysis/)."""
    p = Path(path_str).expanduser()
    if p.is_absolute():
        return str(p.resolve())
    return str((REPO_ROOT / p).resolve())


def load_config() -> dict:
    """Load config: env vars override config.json."""
    _symbol_path_env = os.getenv("SYMBOL_UNIVERSE_PATH", "").strip()
    config = {
        "telegram_api_id": os.getenv("TELEGRAM_API_ID", "").strip(),
        "telegram_api_hash": os.getenv("TELEGRAM_API_HASH", "").strip(),
        "telegram_session_name": os.getenv("TELEGRAM_SESSION_NAME", "news_agent"),
        "telegram_channels": _load_list_from_env("TELEGRAM_CHANNELS", []),
        "rss_feeds": _load_list_from_env("RSS_FEEDS", []),
        "twitter_usernames": _load_list_from_env("TWITTER_USERNAMES", []),
        "twitter_list_ids": _load_list_from_env("TWITTER_LIST_IDS", []),
        "twitter_bearer_token": os.getenv("TWITTER_BEARER_TOKEN", "").strip(),
        "twitter_max_tweets_per_source": int(os.getenv("TWITTER_MAX_TWEETS_PER_SOURCE", "10")),
        "target_channel": os.getenv("TARGET_CHANNEL", "").strip(),
        "openrouter_api_key": os.getenv("OPENROUTER_API_KEY", "").strip(),
        "gemini_api_key": os.getenv("GEMINI_API_KEY", "").strip(),
        "use_gemini": os.getenv("USE_GEMINI", "false").lower() == "true",
        "llm_model": os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL),
        "hours_back": float(os.getenv("HOURS_BACK", "6")),
        "max_items_per_run": int(os.getenv("MAX_ITEMS_PER_RUN", "50")),
        "seen_ids_file": str(DATA_DIR / "seen_ids.json"),
        # Token-efficient prompts (see prompt_compact.py)
        "prompt_style": os.getenv("PROMPT_STYLE", "balanced").strip().lower(),
        "max_snippet_telegram": int(os.getenv("MAX_SNIPPET_TELEGRAM", "220")),
        "max_snippet_rss": int(os.getenv("MAX_SNIPPET_RSS", "380")),
        "max_prompt_chars": int(os.getenv("MAX_PROMPT_CHARS", "6500")),
        # Micro-summarizer (cheap pass before digest)
        "micro_summarize": os.getenv("MICRO_SUMMARIZE", "false").lower() == "true",
        "micro_provider": os.getenv("MICRO_PROVIDER", "auto").strip().lower(),
        "micro_batch_size": int(os.getenv("MICRO_BATCH_SIZE", "12")),
        "micro_min_chars_telegram": int(os.getenv("MICRO_MIN_CHARS_TELEGRAM", "400")),
        "micro_min_chars_rss": int(os.getenv("MICRO_MIN_CHARS_RSS", "280")),
        "micro_min_chars_twitter": int(os.getenv("MICRO_MIN_CHARS_TWITTER", "220")),
        "micro_max_input_chars": int(os.getenv("MICRO_MAX_INPUT_CHARS", "3500")),
        "max_micro_items_per_run": int(os.getenv("MAX_MICRO_ITEMS_PER_RUN", "80")),
        "micro_model_gemini": os.getenv("MICRO_MODEL_GEMINI", "gemini-1.5-flash"),
        "micro_model_openrouter": os.getenv("MICRO_MODEL_OPENROUTER", "anthropic/claude-3-haiku"),
        "digest_assumed_output_tokens": int(os.getenv("DIGEST_ASSUMED_OUTPUT_TOKENS", "1800")),
        "digest_max_output_tokens": int(os.getenv("DIGEST_MAX_OUTPUT_TOKENS", "3500")),
        # all | rss | telegram — which inbound sources to collect (X/Twitter only when mode=all)
        "source_mode": os.getenv("SOURCE_MODE", "all").strip().lower(),
        # Optional API news providers (ingest.py supports SOURCE_MODE=api or all)
        "api_use_symbol_universe": os.getenv("API_USE_SYMBOL_UNIVERSE", "true").strip().lower() == "true",
        "finnhub_api_key": os.getenv("FINNHUB_API_KEY", "").strip(),
        "finnhub_symbols": _load_list_from_env("FINNHUB_SYMBOLS", []),
        "finnhub_max_symbols_per_run": int(os.getenv("FINNHUB_MAX_SYMBOLS_PER_RUN", "0")),  # 0 = all
        "finnhub_sleep_seconds": float(os.getenv("FINNHUB_SLEEP_SECONDS", "0.2")),
        "finnhub_jitter_seconds": float(os.getenv("FINNHUB_JITTER_SECONDS", "0.1")),
        "finnhub_max_retries": int(os.getenv("FINNHUB_MAX_RETRIES", "4")),
        "finnhub_backoff_seconds": float(os.getenv("FINNHUB_BACKOFF_SECONDS", "1.2")),
        "alphavantage_api_key": os.getenv("ALPHAVANTAGE_API_KEY", "").strip(),
        "alphavantage_tickers": _load_list_from_env("ALPHAVANTAGE_TICKERS", []),
        "alphavantage_topics": _load_list_from_env("ALPHAVANTAGE_TOPICS", []),
        "alphavantage_max_tickers_per_request": int(os.getenv("ALPHAVANTAGE_MAX_TICKERS_PER_REQUEST", "50")),
        "alphavantage_max_requests_per_run": int(os.getenv("ALPHAVANTAGE_MAX_REQUESTS_PER_RUN", "5")),
        "alphavantage_sleep_seconds": float(os.getenv("ALPHAVANTAGE_SLEEP_SECONDS", "12.5")),
        "alphavantage_jitter_seconds": float(os.getenv("ALPHAVANTAGE_JITTER_SECONDS", "1.5")),
        "alphavantage_max_retries": int(os.getenv("ALPHAVANTAGE_MAX_RETRIES", "4")),
        "alphavantage_backoff_seconds": float(os.getenv("ALPHAVANTAGE_BACKOFF_SECONDS", "2.0")),
        "stocknewsapi_items": int(os.getenv("STOCKNEWSAPI_ITEMS", "50")),
        "stocknewsapi_pages": int(os.getenv("STOCKNEWSAPI_PAGES", "1")),
        "stocknewsapi_max_tickers_per_request": int(os.getenv("STOCKNEWSAPI_MAX_TICKERS_PER_REQUEST", "50")),
        "stocknewsapi_max_requests_per_run": int(os.getenv("STOCKNEWSAPI_MAX_REQUESTS_PER_RUN", "10")),
        "stocknewsapi_sleep_seconds": float(os.getenv("STOCKNEWSAPI_SLEEP_SECONDS", "0.8")),
        "stocknewsapi_jitter_seconds": float(os.getenv("STOCKNEWSAPI_JITTER_SECONDS", "0.25")),
        "stocknewsapi_max_retries": int(os.getenv("STOCKNEWSAPI_MAX_RETRIES", "4")),
        "stocknewsapi_backoff_seconds": float(os.getenv("STOCKNEWSAPI_BACKOFF_SECONDS", "1.0")),
        "stocknewsapi_token": os.getenv("STOCKNEWSAPI_TOKEN", "").strip()
        or os.getenv("STOCKNEWS_API_KEY", "").strip(),
        "stocknewsapi_tickers": _load_list_from_env("STOCKNEWSAPI_TICKERS", []),
        # Bot mode (python -m telegram_agent.bot_app) — optional allowlist; empty = any chat
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "bot_schedule_file": str(DATA_DIR / "bot_schedules.json"),
        "bot_allowed_chat_ids": [
            x.strip()
            for x in os.getenv("BOT_ALLOWED_CHAT_IDS", "").split(",")
            if x.strip()
        ],
        "bot_min_schedule_hours": float(os.getenv("BOT_MIN_SCHEDULE_HOURS", "0.25")),
        # Agent (ingest / research / backtest). Relative AGENT_DB_PATH is from repo root.
        "agent_db_path": os.getenv("AGENT_DB_PATH", "").strip()
        or str(DATA_DIR / "agent.sqlite"),
        "agent_backfill_days": int(os.getenv("AGENT_BACKFILL_DAYS", "365")),
        "agent_memory_months": int(os.getenv("AGENT_MEMORY_MONTHS", "6")),
        "agent_memory_prompt_chars": int(os.getenv("AGENT_MEMORY_PROMPT_CHARS", "8000")),
        "agent_max_telegram_backfill": int(os.getenv("AGENT_MAX_TELEGRAM_BACKFILL", "3000")),
        "agent_research_model": os.getenv("AGENT_RESEARCH_MODEL", "").strip()
        or os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL),
        # Only persist opportunities with confidence >= this (0..1). Tightens "high confidence" bar.
        "agent_research_min_confidence": float(os.getenv("AGENT_RESEARCH_MIN_CONFIDENCE", "0.75")),
        "agent_research_max_output_tokens": int(os.getenv("AGENT_RESEARCH_MAX_OUTPUT_TOKENS", "12000")),
        # Single knob: max number of news rows fetched AND included in the research prompt.
        # CLI flag `research --max-num-ofnews` overrides this value at runtime.
        "agent_research_max_num_ofnews": int(os.getenv("MAX_NUM_OFNEWS", "200")),
        # Universe priority filter: only use investments with priority <= this value (0..3).
        # CLI flags across commands may override this at runtime.
        "max_priority": int(os.getenv("MAX_PRIORITY", "3")),
        # Research-only override for universe priority (if set, research uses this instead of max_priority).
        "agent_research_max_priority": (
            int(os.getenv("AGENT_RESEARCH_MAX_PRIORITY", "").strip())
            if os.getenv("AGENT_RESEARCH_MAX_PRIORITY", "").strip()
            else None
        ),
        "agent_research_publish": os.getenv("AGENT_RESEARCH_PUBLISH", "true").lower() == "true",
        "agent_research_backfill_publish": os.getenv("AGENT_RESEARCH_BACKFILL_PUBLISH", "true").lower()
        == "true",
        "agent_research_backfill_sleep_seconds": float(
            os.getenv("AGENT_RESEARCH_BACKFILL_SLEEP_SECONDS", "1")
        ),
        # Prices (yfinance) batching knobs
        "prices_yf_batch_size": int(os.getenv("PRICES_YF_BATCH_SIZE", "80")),
        "prices_yf_sleep_seconds": float(os.getenv("PRICES_YF_SLEEP_SECONDS", "0.5")),
        # Structured memory caps (merge in memory_structured.merge_memory_state)
        "agent_memory_cap_strongest": int(os.getenv("AGENT_MEMORY_CAP_STRONGEST", "20")),
        "agent_memory_cap_recent": int(os.getenv("AGENT_MEMORY_CAP_RECENT", "20")),
        "agent_memory_suggestion_days": int(os.getenv("AGENT_MEMORY_SUGGESTION_DAYS", "30")),
        # Dry-run cost estimate only (typical completion size; actual may be lower).
        "agent_research_assumed_output_tokens": int(
            os.getenv("AGENT_RESEARCH_ASSUMED_OUTPUT_TOKENS", "4000")
        ),
        "agent_research_tester_prompt_limit": int(os.getenv("AGENT_RESEARCH_TESTER_PROMPT_LIMIT", "50")),
        # Competitive systematic bots (priority-filtered universe; same tester as research legs)
        "competitive_bots_max_priority": int(os.getenv("COMPETITIVE_BOTS_MAX_PRIORITY", "1")),
        "competitive_bots_max_picks": int(os.getenv("COMPETITIVE_BOTS_MAX_PICKS", "3")),
        "competitive_bots_min_bars": int(os.getenv("COMPETITIVE_BOTS_MIN_BARS", "25")),
        "competitive_bots_review_horizon_days": int(
            os.getenv("COMPETITIVE_BOTS_REVIEW_HORIZON_DAYS", "12")
        ),
        "competitive_bots_entry_span_days": int(os.getenv("COMPETITIVE_BOTS_ENTRY_SPAN_DAYS", "3")),
        "competitive_bots_publish": os.getenv("COMPETITIVE_BOTS_PUBLISH", "true").lower() == "true",
        # Walk-forward backtest (competitive-bots --backtest)
        "competitive_backtest_max_symbols": int(os.getenv("COMPETITIVE_BACKTEST_MAX_SYMBOLS", "50")),
        "competitive_backtest_max_eval_points": int(
            os.getenv("COMPETITIVE_BACKTEST_MAX_EVAL_POINTS", "2000")
        ),
        "competitive_backtest_max_total_rows": int(
            os.getenv("COMPETITIVE_BACKTEST_MAX_TOTAL_ROWS", "3000000")
        ),
        "competitive_backtest_horizon_overrides": os.getenv(
            "COMPETITIVE_BACKTEST_HORIZON_OVERRIDES", ""
        ).strip(),
        # Strategy test agent: per-leg backtests + aggregate metrics (see strategy_metrics.py, agent_tester.py)
        "test_metrics_enabled": [
            x.strip().lower()
            for x in os.getenv(
                "TEST_METRICS_ENABLED",
                "sharpe,alpha,max_drawdown,oos_sharpe,calmar,significance",
            ).split(",")
            if x.strip()
        ],
        "test_optimization_metric": os.getenv("TEST_OPTIMIZATION_METRIC", "sharpe").strip().lower(),
        "test_benchmark_symbol": os.getenv("TEST_BENCHMARK_SYMBOL", "SPY").strip().upper(),
        "test_risk_free_annual": float(os.getenv("TEST_RISK_FREE_ANNUAL", "0.04")),
        "test_oos_split": float(os.getenv("TEST_OOS_SPLIT", "0.5")),
        # Extract: LLM ticker/symbol extraction (cheap model; batched)
        "extract_use_llm": os.getenv("EXTRACT_USE_LLM", "true").lower() == "true",
        "extract_llm_model": os.getenv("EXTRACT_LLM_MODEL", "").strip()
        or os.getenv("MICRO_MODEL_OPENROUTER", "anthropic/claude-3-haiku"),
        "extract_llm_batch_size": int(os.getenv("EXTRACT_LLM_BATCH_SIZE", "12")),
        "extract_max_chars_per_item": int(os.getenv("EXTRACT_MAX_CHARS_PER_ITEM", "2200")),
        "extract_regex_fallback": os.getenv("EXTRACT_REGEX_FALLBACK", "true").lower() == "true",
        # Cheap LLM: tag each news row with allowlist tickers; fills symbol_news_linkage + news_mentions (universe_preprocess).
        "news_universe_preprocess_model": os.getenv("NEWS_UNIVERSE_PREPROCESS_MODEL", "").strip()
        or os.getenv("MICRO_MODEL_OPENROUTER", "anthropic/claude-3-haiku"),
        "news_universe_preprocess_batch_size": int(os.getenv("NEWS_UNIVERSE_PREPROCESS_BATCH_SIZE", "16")),
        "news_universe_preprocess_max_rows_per_run": int(
            os.getenv("NEWS_UNIVERSE_PREPROCESS_MAX_ROWS", "100000")
        ),
        # Research: filter prompt news to items linked via preprocessor (symbol universe mode).
        "agent_research_universe_news_linkage": os.getenv("AGENT_RESEARCH_UNIVERSE_NEWS_LINKAGE", "true").lower()
        == "true",
        "agent_research_run_universe_preprocess_before_research": os.getenv(
            "AGENT_RESEARCH_RUN_UNIVERSE_PREPROCESS", "true"
        ).lower()
        == "true",
        # Fixed symbol universe mode (avoid broad extraction; constrain pipeline)
        # Provide either SYMBOL_UNIVERSE_ENV (comma-separated) or SYMBOL_UNIVERSE_PATH (JSON file).
        # If PATH or ENV list is set, universe is on unless SYMBOL_UNIVERSE_ENABLED=false.
        "symbol_universe_enabled": _symbol_universe_enabled_from_env(),
        "symbol_universe_env": os.getenv("SYMBOL_UNIVERSE_ENV", "").strip(),
        "symbol_universe_path": _symbol_path_env or str(DATA_DIR / "symbol_universe_top1000.json"),
        "memory_use_universe_news_only": os.getenv("MEMORY_USE_UNIVERSE_NEWS_ONLY", "true").lower()
        == "true",
    }

    if CONFIG_JSON.exists():
        try:
            with open(CONFIG_JSON, "r") as f:
                file_cfg = json.load(f)
            list_keys = (
                "telegram_channels",
                "rss_feeds",
                "twitter_usernames",
                "twitter_list_ids",
            )
            for k, v in file_cfg.items():
                if k in config and v is not None:
                    if k in list_keys and isinstance(v, list):
                        config[k] = v
                    elif k not in list_keys:
                        config[k] = v
        except Exception as e:
            logger.warning("Could not load config.json: %s", e)

    # Env overrides for lists if set
    if os.getenv("TELEGRAM_CHANNELS"):
        config["telegram_channels"] = _load_list_from_env("TELEGRAM_CHANNELS", config["telegram_channels"])
    if os.getenv("RSS_FEEDS"):
        config["rss_feeds"] = _load_list_from_env("RSS_FEEDS", config["rss_feeds"])
    if os.getenv("TWITTER_USERNAMES"):
        config["twitter_usernames"] = _load_list_from_env("TWITTER_USERNAMES", config["twitter_usernames"])
    if os.getenv("TWITTER_LIST_IDS"):
        config["twitter_list_ids"] = _load_list_from_env("TWITTER_LIST_IDS", config["twitter_list_ids"])

    # Default source packs when lists still empty (see default_sources.py)
    if not config["telegram_channels"]:
        config["telegram_channels"] = list(DEFAULT_TELEGRAM_CHANNELS)
    if not config["rss_feeds"]:
        config["rss_feeds"] = list(DEFAULT_RSS_FEEDS)
    if not config["twitter_usernames"]:
        config["twitter_usernames"] = list(DEFAULT_TWITTER_USERNAMES)

    for _path_key in ("symbol_universe_path", "agent_db_path"):
        raw = config.get(_path_key)
        if raw:
            try:
                config[_path_key] = _resolve_repo_relative(str(raw))
            except OSError as e:
                logger.warning("Could not resolve %s=%r: %s", _path_key, raw, e)

    # If API_USE_SYMBOL_UNIVERSE=true and provider-specific tickers are unset, default them to the universe.
    # This allows running API-only ingest/backfills over the full universe without duplicating config.
    if config.get("api_use_symbol_universe", True):
        try:
            from telegram_agent.symbol_universe import load_symbol_universe

            uni = load_symbol_universe(config) or []
        except Exception:
            uni = []
        if uni:
            if not config.get("finnhub_symbols"):
                config["finnhub_symbols"] = list(uni)
            if not config.get("alphavantage_tickers"):
                config["alphavantage_tickers"] = list(uni)
            if not config.get("stocknewsapi_tickers"):
                config["stocknewsapi_tickers"] = list(uni)

    return config
