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
        # Bot mode (python -m telegram_agent.bot_app) — optional allowlist; empty = any chat
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "bot_schedule_file": str(DATA_DIR / "bot_schedules.json"),
        "bot_allowed_chat_ids": [
            x.strip()
            for x in os.getenv("BOT_ALLOWED_CHAT_IDS", "").split(",")
            if x.strip()
        ],
        "bot_min_schedule_hours": float(os.getenv("BOT_MIN_SCHEDULE_HOURS", "0.25")),
        # Agent (ingest / research / backtest)
        "agent_db_path": str(DATA_DIR / "agent.sqlite"),
        "agent_backfill_days": int(os.getenv("AGENT_BACKFILL_DAYS", "365")),
        "agent_memory_months": int(os.getenv("AGENT_MEMORY_MONTHS", "6")),
        "agent_memory_prompt_chars": int(os.getenv("AGENT_MEMORY_PROMPT_CHARS", "8000")),
        "agent_max_telegram_backfill": int(os.getenv("AGENT_MAX_TELEGRAM_BACKFILL", "3000")),
        "agent_research_model": os.getenv("AGENT_RESEARCH_MODEL", "").strip()
        or os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL),
        # Only persist opportunities with confidence >= this (0..1). Tightens "high confidence" bar.
        "agent_research_min_confidence": float(os.getenv("AGENT_RESEARCH_MIN_CONFIDENCE", "0.75")),
        "agent_research_max_output_tokens": int(os.getenv("AGENT_RESEARCH_MAX_OUTPUT_TOKENS", "12000")),
        "agent_research_publish": os.getenv("AGENT_RESEARCH_PUBLISH", "true").lower() == "true",
        "agent_research_backfill_publish": os.getenv("AGENT_RESEARCH_BACKFILL_PUBLISH", "true").lower()
        == "true",
        "agent_research_backfill_sleep_seconds": float(
            os.getenv("AGENT_RESEARCH_BACKFILL_SLEEP_SECONDS", "1")
        ),
        # Structured memory caps (merge in memory_structured.merge_memory_state)
        "agent_memory_cap_strongest": int(os.getenv("AGENT_MEMORY_CAP_STRONGEST", "20")),
        "agent_memory_cap_recent": int(os.getenv("AGENT_MEMORY_CAP_RECENT", "20")),
        "agent_memory_suggestion_days": int(os.getenv("AGENT_MEMORY_SUGGESTION_DAYS", "30")),
        # Dry-run cost estimate only (typical completion size; actual may be lower).
        "agent_research_assumed_output_tokens": int(
            os.getenv("AGENT_RESEARCH_ASSUMED_OUTPUT_TOKENS", "4000")
        ),
        "agent_research_tester_prompt_limit": int(os.getenv("AGENT_RESEARCH_TESTER_PROMPT_LIMIT", "50")),
        # Extract: LLM ticker/symbol extraction (cheap model; batched)
        "extract_use_llm": os.getenv("EXTRACT_USE_LLM", "true").lower() == "true",
        "extract_llm_model": os.getenv("EXTRACT_LLM_MODEL", "").strip()
        or os.getenv("MICRO_MODEL_OPENROUTER", "anthropic/claude-3-haiku"),
        "extract_llm_batch_size": int(os.getenv("EXTRACT_LLM_BATCH_SIZE", "12")),
        "extract_max_chars_per_item": int(os.getenv("EXTRACT_MAX_CHARS_PER_ITEM", "2200")),
        "extract_regex_fallback": os.getenv("EXTRACT_REGEX_FALLBACK", "true").lower() == "true",
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

    return config
