"""
Load hourly price snapshots from ``historical_prices/prices_YYYYMMDD.csv`` into ``prices_hourly``.

Dataset format (one file per calendar day of snapshots)::

    timestamp,symbol,price
    2025-12-01 09:00:00,AAPL,277.57

Only a single ``price`` column is present; it is stored as ``open`` = ``high`` = ``low`` =
``close`` = ``adj_close`` = ``price``, ``volume`` = NULL. Rows are tagged with
``source='historical_csv'`` (ON CONFLICT updates overlap with yfinance rows for the same key).

Naive ``timestamp`` values are interpreted in ``--tz`` (default ``America/New_York`` for
US equity session hours) and stored as UTC ISO strings in ``ts_utc``.

Usage::

    python -m telegram_agent.import_historical_prices_csv --dry-run
    python -m telegram_agent.import_historical_prices_csv
    python -m telegram_agent.import_historical_prices_csv --limit-files 5
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from telegram_agent.agent_db import connect, ensure_instruments, init_db, upsert_intraday_rows
from telegram_agent.config import DATA_DIR, load_config
from telegram_agent.symbol_universe import normalize_symbol

logger = logging.getLogger(__name__)

SOURCE_TAG = "historical_csv"
DEFAULT_GLOB = "prices_*.csv"


def _parse_naive_timestamp(raw: str) -> datetime:
    s = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized timestamp: {raw!r}")


def naive_local_to_utc_iso(naive: datetime, tz_name: str) -> str:
    z = ZoneInfo(tz_name)
    aware = naive.replace(tzinfo=z)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _load_env_file(path: Path) -> None:
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


def iter_csv_files(directory: Path, pattern: str) -> List[Path]:
    files = sorted(directory.glob(pattern))
    return [p for p in files if p.is_file()]


def parse_csv_file(
    path: Path,
    *,
    tz_name: str,
    symbol_allowlist: Optional[Set[str]] = None,
) -> Tuple[Dict[str, List[Tuple[str, float, float, float, float, Optional[float], Optional[float]]]], int, int]:
    """
    Returns (rows_by_symbol, row_count, skip_count).
    Each row tuple matches upsert_intraday_rows: (ts_utc, o, h, l, c, adj_close, volume).
    """
    rows_by_symbol: Dict[str, List[Tuple[str, float, float, float, float, Optional[float], Optional[float]]]] = (
        defaultdict(list)
    )
    n_ok = 0
    n_skip = 0
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return dict(rows_by_symbol), 0, 0
        fields = {x.strip().lower(): x for x in reader.fieldnames}
        ts_col = fields.get("timestamp") or fields.get("ts") or fields.get("time")
        sym_col = fields.get("symbol") or fields.get("ticker")
        px_col = fields.get("price") or fields.get("close")
        if not ts_col or not sym_col or not px_col:
            raise ValueError(
                f"{path}: need columns timestamp, symbol, price (got {reader.fieldnames})"
            )
        for row in reader:
            try:
                raw_ts = row.get(ts_col, "")
                sym = normalize_symbol(str(row.get(sym_col, "")))
                if not sym:
                    n_skip += 1
                    continue
                if symbol_allowlist is not None and sym not in symbol_allowlist:
                    continue
                px = float(row.get(px_col, "") or "")
                if not (px == px) or px <= 0:
                    n_skip += 1
                    continue
                naive = _parse_naive_timestamp(raw_ts)
                ts_iso = naive_local_to_utc_iso(naive, tz_name)
            except Exception as e:
                logger.debug("skip row in %s: %s", path.name, e)
                n_skip += 1
                continue
            tup = (ts_iso, px, px, px, px, px, None)
            rows_by_symbol[sym].append(tup)
            n_ok += 1
    return dict(rows_by_symbol), n_ok, n_skip


def import_files(
    paths: Sequence[Path],
    *,
    tz_name: str,
    dry_run: bool,
    symbol_allowlist: Optional[Set[str]] = None,
) -> Dict[str, int]:
    cfg = load_config()
    db_path = Path(cfg.get("agent_db_path") or (DATA_DIR / "agent.sqlite"))
    con = connect(db_path)
    init_db(con)

    total_rows = 0
    total_upsert = 0
    files_ok = 0
    for path in paths:
        try:
            by_sym, n_ok, n_skip = parse_csv_file(path, tz_name=tz_name, symbol_allowlist=symbol_allowlist)
        except Exception as e:
            logger.warning("Skip %s: %s", path, e)
            continue
        if not by_sym:
            logger.debug("Empty or filtered: %s", path)
            continue
        total_rows += n_ok
        files_ok += 1
        if dry_run:
            continue
        syms = list(by_sym.keys())
        ensure_instruments(con, syms)
        try:
            for sym, rows in by_sym.items():
                n = upsert_intraday_rows(
                    con,
                    "prices_hourly",
                    sym,
                    rows,
                    source=SOURCE_TAG,
                    commit=False,
                )
                total_upsert += n
            con.commit()
        except Exception:
            con.rollback()
            raise
    con.close()
    return {
        "files_considered": len(paths),
        "files_imported": files_ok,
        "rows_parsed": total_rows,
        "rows_written_to_db": total_upsert,
        "dry_run": bool(dry_run),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="Import historical_prices CSVs into prices_hourly")
    p.add_argument(
        "--dir",
        type=str,
        default=None,
        help=f"Folder with prices_*.csv (default: package historical_prices/)",
    )
    p.add_argument("--glob", type=str, default=DEFAULT_GLOB, help="File glob under --dir")
    p.add_argument(
        "--tz",
        type=str,
        default="America/New_York",
        help="Timezone for naive timestamps (use UTC if your files are already UTC)",
    )
    p.add_argument("--dry-run", action="store_true", help="Parse files and count rows only")
    p.add_argument("--limit-files", type=int, default=0, help="Process only first N files (sorted)")
    p.add_argument(
        "--universe-only",
        action="store_true",
        help="Only import symbols present in the configured symbol universe",
    )
    args = p.parse_args()

    root = Path(__file__).resolve().parents[1]
    _load_env_file(root / ".env")
    _load_env_file(Path(__file__).resolve().parent / ".env")
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    base = Path(args.dir) if args.dir else Path(__file__).resolve().parent / "historical_prices"
    if not base.is_dir():
        raise SystemExit(f"Not a directory: {base}")

    files = iter_csv_files(base, args.glob)
    if args.limit_files and args.limit_files > 0:
        files = files[: args.limit_files]

    allow: Optional[Set[str]] = None
    if args.universe_only:
        from telegram_agent.symbol_universe import load_symbol_universe

        cfg = load_config()
        uni = load_symbol_universe(cfg)
        if not uni:
            raise SystemExit("--universe-only requires a configured symbol universe")
        allow = {normalize_symbol(s) for s in uni}

    stats = import_files(files, tz_name=args.tz, dry_run=bool(args.dry_run), symbol_allowlist=allow)
    print(stats)


if __name__ == "__main__":
    main()
