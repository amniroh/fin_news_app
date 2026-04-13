"""
P0 symbols with minimum bar counts in daily + hourly + minute tables (full coverage).

Used by ``competitive-bots --backtest`` when ``--backtest-symbols p0-full-coverage``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

from telegram_agent.agent_db import connect, count_prices_for_symbol_interval, init_db
from telegram_agent.symbol_universe import symbols_with_exact_priority

logger = logging.getLogger(__name__)


def _load_env_file(path: Path) -> None:
    """Minimal KEY=VALUE loader when python-dotenv is not installed."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def list_p0_full_coverage_symbols(cfg: dict, con) -> List[str]:
    """
    Priority-0 universe symbols that meet minimum bar counts for 1d, 1h, and 1m.
    Counts use ``prices`` (1d) and intraday tables ``prices_hourly`` / ``prices_minute``.
    """
    p0 = symbols_with_exact_priority(cfg, 0)
    if not p0:
        logger.warning("No priority-0 symbols (need JSON universe with priority: 0 entries).")
        return []

    min1d = max(1, int(cfg.get("full_coverage_min_bars_1d", 200)))
    min1h = max(1, int(cfg.get("full_coverage_min_bars_1h", 400)))
    min1m = max(1, int(cfg.get("full_coverage_min_bars_1m", 800)))

    out: List[str] = []
    for sym in p0:
        n1d = count_prices_for_symbol_interval(con, sym, "1d")
        n1h = count_prices_for_symbol_interval(con, sym, "1h")
        n1m = count_prices_for_symbol_interval(con, sym, "1m")
        if n1d >= min1d and n1h >= min1h and n1m >= min1m:
            out.append(sym)
    return sorted(out)


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from telegram_agent.config import load_config

    root = Path(__file__).resolve().parents[1]
    _load_env_file(root / ".env")
    _load_env_file(Path(__file__).resolve().parent / ".env")
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass
    cfg = load_config()
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    syms = list_p0_full_coverage_symbols(cfg, con)
    con.close()
    line = ",".join(syms)
    print("P0 full-coverage symbols (%s):" % len(syms))
    print(line)
    print()
    print("Add to .env:")
    print(f"COMPETITIVE_BACKTEST_SYMBOLS={line}")
    print()
    print("Then run: python -m telegram_agent.agent competitive-bots --backtest --backtest-symbols env")


if __name__ == "__main__":
    _main()
