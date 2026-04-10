"""
Collectors package.

Keep this module dependency-light: avoid importing optional heavy deps (e.g. telethon)
at import time so tooling can import non-Telegram collectors without needing telethon installed.
"""

__all__ = []
