"""Keep API keys and bot tokens out of log output (httpx logs full request URLs at INFO)."""
from __future__ import annotations

import logging
import os
import re
from typing import Iterable, List

_PATTERNS = [
    # Telegram Bot API: https://api.telegram.org/bot<id>:<secret>/method
    (re.compile(r"(/bot)\d+:[A-Za-z0-9_-]{20,}"), r"\1***"),
    # Query-string credentials: ?token=..., &apikey=..., &api_key=..., &key=...
    (re.compile(r"([?&](?:token|apikey|api_key|apiKey|key|access_token)=)[^&\s\"']+", re.I), r"\1***"),
    # Authorization headers echoed into messages
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._-]{12,}", re.I), r"\1***"),
    (re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}"), "sk-or-***"),
]

_SECRET_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "FINNHUB_API_KEY",
    "ALPHAVANTAGE_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "TWITTER_BEARER_TOKEN",
    "TELEGRAM_API_HASH",
    "STOCKNEWSAPI_KEY",
    "NEWSAPI_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
)

_installed = False


def _env_secrets(names: Iterable[str]) -> List[str]:
    out = []
    for n in names:
        v = (os.getenv(n) or "").strip()
        if len(v) >= 8:
            out.append(v)
    return out


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for s in secrets:
        if s and s in text:
            text = text.replace(s, "***")
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def install_log_redaction() -> None:
    """
    Process-wide: quiet per-request httpx logs and redact secrets from formatted output
    (messages and tracebacks) of every handler that uses a logging.Formatter.
    """
    global _installed
    if _installed:
        return
    _installed = True

    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

    secrets = _env_secrets(_SECRET_ENV_VARS)
    base_format = logging.Formatter.format

    def format(self, record):  # noqa: A001 - mirrors logging.Formatter.format
        return redact(base_format(self, record), secrets)

    logging.Formatter.format = format
