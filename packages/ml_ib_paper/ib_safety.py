#!/usr/bin/env python3
"""Safety guards so paper-trading bots cannot accidentally hit a live IB account."""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Sequence

log = logging.getLogger("ib_safety")

# Interactive Brokers conventional API ports
LIVE_API_PORTS = frozenset({4001, 7496})  # Gateway live, TWS live
PAPER_API_PORTS = frozenset({4002, 7497})  # Gateway paper, TWS paper


def _env_truthy(name: str, default: str = "0") -> bool:
    return (os.environ.get(name) or default).strip().lower() in ("1", "true", "yes", "on")


def assert_paper_port(port: int) -> None:
    """Refuse known live API ports unless IB_ALLOW_LIVE is explicitly set."""
    p = int(port)
    if p in LIVE_API_PORTS and not _env_truthy("IB_ALLOW_LIVE"):
        raise SystemExit(
            f"Refusing IB API port {p} (live). Paper Gateway uses 4002; paper TWS uses 7497. "
            "Set IB_PORT=4002 and keep IB_ALLOW_LIVE unset."
        )
    if p not in PAPER_API_PORTS and not _env_truthy("IB_ALLOW_NONSTANDARD_PORT"):
        raise SystemExit(
            f"IB_PORT={p} is not a standard paper port (4002 Gateway / 7497 TWS). "
            "Fix IB_PORT, or set IB_ALLOW_NONSTANDARD_PORT=1 only if you know what you are doing."
        )


def assert_trading_mode_paper() -> None:
    mode = (os.environ.get("IB_TRADING_MODE") or os.environ.get("TRADING_MODE") or "paper").strip().lower()
    if mode != "paper" and not _env_truthy("IB_ALLOW_LIVE"):
        raise SystemExit(
            f"IB_TRADING_MODE/TRADING_MODE={mode!r} is not paper. "
            "Set IB_TRADING_MODE=paper (live trading is blocked)."
        )


def _looks_like_paper_account(account: str) -> bool:
    a = (account or "").strip().upper()
    # Paper accounts commonly start with DU (and sometimes DF for financial advisor paper).
    return a.startswith("DU") or a.startswith("DF")


def assert_paper_accounts(accounts: Sequence[str], *, preferred: Optional[str] = None) -> str:
    """Ensure we only trade a paper account id. Returns the account to use."""
    cleaned = [str(a).strip() for a in accounts if str(a).strip()]
    if not cleaned:
        raise SystemExit("IB returned no managed accounts — cannot verify paper vs live.")

    if preferred:
        pref = preferred.strip()
        if pref not in cleaned:
            raise SystemExit(f"IB_ACCOUNT={pref!r} not in managed accounts {cleaned}")
        if not _looks_like_paper_account(pref) and not _env_truthy("IB_ALLOW_LIVE"):
            raise SystemExit(
                f"IB_ACCOUNT={pref!r} does not look like a paper account (expected DU… / DF…). "
                "Live accounts usually start with U. Refusing to trade."
            )
        return pref

    paper = [a for a in cleaned if _looks_like_paper_account(a)]
    liveish = [a for a in cleaned if not _looks_like_paper_account(a)]
    if liveish and not _env_truthy("IB_ALLOW_LIVE"):
        log.warning("Live-looking account(s) visible on this session: %s", liveish)
    if not paper:
        raise SystemExit(
            f"No paper account (DU…/DF…) in managed accounts {cleaned}. "
            "Log Gateway into PAPER mode, or set IB_ACCOUNT to your DU… id."
        )
    if len(paper) > 1 and not preferred:
        raise SystemExit(
            f"Multiple paper accounts {paper}; set IB_ACCOUNT to the one you want."
        )
    return paper[0]


def verify_ib_session_is_paper(ib: Any, *, preferred_account: Optional[str] = None) -> str:
    """Connect-time checks: port/mode already asserted; validate managed accounts."""
    assert_trading_mode_paper()
    accounts = list(ib.managedAccounts() or [])
    # managedAccounts can be empty briefly; also try accountSummary tags
    if not accounts:
        seen = []
        for row in ib.accountSummary():
            if row.account and row.account not in seen:
                seen.append(row.account)
        accounts = seen
    chosen = assert_paper_accounts(accounts, preferred=preferred_account)
    log.info("IB paper safety OK — using account %s (managed=%s)", chosen, accounts)
    return chosen
