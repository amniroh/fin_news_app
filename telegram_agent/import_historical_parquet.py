"""
Audit and optionally import Parquet price snapshots under ``telegram_agent/historical_data``.

Expected columns (case-insensitive)::

    timestamp (or time / ts), price (or close), symbol (or ticker) — symbol optional if
    inferable from filename (e.g. ``AAPL_5min.parquet``).

Rows are stored as flat OHLC (open=high=low=close=price), ``adj_close`` and ``volume``
NULL, ``source='historical_parquet'``. Bar interval is inferred from median intra-row
spacing (1m / 5m / 1h / 1d). **5m** bars go to ``prices`` with ``interval='5m'``; **1m**
to ``prices_minute``; **1h** to ``prices_hourly``; **1d** to ``prices`` with ``interval='1d'``.

Inserts use ``ON CONFLICT DO NOTHING`` so existing **yfinance** (or any) rows are never
overwritten.

Usage::

    python -m telegram_agent.import_historical_parquet --audit-only
    python -m telegram_agent.import_historical_parquet --ingest
    python -m telegram_agent.import_historical_parquet --ingest --limit-files 20
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import sqlite3

import pandas as pd

from telegram_agent.agent_db import (
    _utc_iso,
    connect,
    ensure_instruments,
    init_db,
    upsert_intraday_rows,
    upsert_price_rows,
)
from telegram_agent.config import DATA_DIR, load_config
from telegram_agent.symbol_universe import normalize_symbol

logger = logging.getLogger(__name__)

SOURCE_TAG = "historical_parquet"
DEFAULT_DATA_DIR_NAME = "historical_data"
PRICES_DAY_FILE = re.compile(r"^prices_(\d{8})\.parquet$", re.IGNORECASE)
SYMBOL_MIN_FILE = re.compile(r"^([A-Za-z0-9\.\-]+)_(\d+)min\.parquet$", re.IGNORECASE)

# Per-file quality gates (fail the file if exceeded).
MAX_BAD_PRICE_RATE = 0.01
MAX_DUP_KEY_RATE = 0.01

# Dataset-level gate: abort ingest if more than this fraction of files fail audit.
MAX_AUDIT_FILE_FAIL_RATE = 0.05


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


def _default_data_dir() -> Path:
    return Path(__file__).resolve().parent / DEFAULT_DATA_DIR_NAME


def discover_parquet_files(root: Path, *, subdir: str) -> List[Path]:
    base = root / subdir if subdir else root
    if not base.is_dir():
        return []
    out = sorted(base.rglob("*.parquet")) if subdir else sorted(root.rglob("*.parquet"))
    # Skip empty shards — they skew audit fail rates without carrying price information.
    return [p for p in out if p.is_file() and p.stat().st_size > 0]


def _infer_symbol_from_filename(path: Path) -> Optional[str]:
    m = SYMBOL_MIN_FILE.match(path.name)
    if not m:
        return None
    raw = m.group(1).strip()
    sym = normalize_symbol(raw)
    return sym or None


def _file_day_from_name(path: Path) -> Optional[date]:
    m = PRICES_DAY_FILE.match(path.name)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d").date()


def _read_parquet_price_columns(path: Path) -> Tuple[pd.DataFrame, Optional[str]]:
    """
    Load only timestamp / price / symbol columns (by Parquet schema names) for faster IO.
    Returns a frame with canonical columns ``timestamp``, ``price``, and optionally ``symbol``.
    """
    import pyarrow.parquet as pq

    try:
        pf = pq.ParquetFile(path)
    except Exception as e:
        return pd.DataFrame(), f"read_error:{e!r}"
    raw_names = pf.schema_arrow.names
    cmap = {n.lower().strip(): n for n in raw_names}
    ts_c = cmap.get("timestamp") or cmap.get("time") or cmap.get("ts") or cmap.get("datetime")
    px_c = cmap.get("price") or cmap.get("close") or cmap.get("adj_close")
    if not ts_c or not px_c:
        return pd.DataFrame(), "missing_timestamp_or_price_column"
    sym_c = cmap.get("symbol") or cmap.get("ticker")
    read_cols = [ts_c, px_c]
    if sym_c:
        read_cols.append(sym_c)
    try:
        df = pd.read_parquet(path, columns=read_cols, engine="pyarrow")
    except Exception as e:
        return pd.DataFrame(), f"read_error:{e!r}"
    rename = {ts_c: "timestamp", px_c: "price"}
    if sym_c:
        rename[sym_c] = "symbol"
    df = df.rename(columns=rename)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str).map(lambda x: normalize_symbol(x))
    return df, None


def _normalize_columns(df: pd.DataFrame) -> Tuple[pd.DataFrame, Optional[str]]:
    """Return dataframe with columns timestamp, price, symbol (symbol may be missing)."""
    cmap = {c.lower().strip(): c for c in df.columns}
    err: Optional[str] = None

    ts_key = cmap.get("timestamp") or cmap.get("time") or cmap.get("ts") or cmap.get("datetime")
    if not ts_key:
        return df, "missing_timestamp_column"
    px_key = cmap.get("price") or cmap.get("close") or cmap.get("adj_close")
    if not px_key:
        return df, "missing_price_column"

    sym_key = cmap.get("symbol") or cmap.get("ticker")

    out = pd.DataFrame(
        {
            "timestamp": df[ts_key],
            "price": pd.to_numeric(df[px_key], errors="coerce"),
        }
    )
    if sym_key:
        out["symbol"] = df[sym_key].astype(str).map(lambda x: normalize_symbol(x))
    return out, err


def _coerce_utc_timestamps(s: pd.Series) -> pd.Series:
    ts = pd.to_datetime(s, utc=True, errors="coerce")
    return ts


def classify_interval_from_median_seconds(median_sec: float) -> Optional[str]:
    if median_sec != median_sec or median_sec <= 0:  # NaN
        return None
    # Daily-ish (23–27h) — rare in this folder but supported.
    if 23 * 3600 <= median_sec <= 27 * 3600:
        return "1d"
    # Hourly-ish: allow sparse / irregular spacing (e.g. ~99m medians when sessions are thin).
    if 35 * 60 <= median_sec <= 150 * 60:
        return "1h"
    if 3.5 * 60 <= median_sec <= 7.5 * 60:
        return "5m"
    if 40 <= median_sec <= 85:
        return "1m"
    return None


def _is_good_price(x: object) -> bool:
    try:
        v = float(x)  # numpy scalars ok
        return v == v and v > 0
    except (TypeError, ValueError):
        return False


def _median_bar_seconds(df: pd.DataFrame) -> float:
    if df.empty or "symbol" not in df.columns:
        return float("nan")
    d = df.sort_values(["symbol", "timestamp"])
    medians: List[float] = []
    for _, g in d.groupby("symbol", sort=False):
        g2 = g["timestamp"].dropna()
        if len(g2) < 2:
            continue
        delta = g2.diff().dt.total_seconds().dropna()
        if delta.empty:
            continue
        medians.append(float(delta.median()))
    if not medians:
        return float("nan")
    return float(pd.Series(medians).median())


def _filename_day_consistent(path: Path, df: pd.DataFrame) -> Tuple[bool, str]:
    fd = _file_day_from_name(path)
    if fd is None:
        return True, "no_filename_day"
    ts = df["timestamp"].dropna()
    if ts.empty:
        return False, "empty_timestamps"
    dmin = ts.min().date()
    dmax = ts.max().date()
    pad = timedelta(days=1)
    lo = dmin - pad
    hi = dmax + pad
    if lo <= fd <= hi:
        return True, "ok"
    return False, f"filename_day_{fd}_outside_range_{dmin}_{dmax}"


@dataclass
class FileAudit:
    path: str
    ok: bool
    rows: int = 0
    interval: Optional[str] = None
    median_bar_seconds: Optional[float] = None
    bad_price_rate: float = 0.0
    dup_rate: float = 0.0
    symbols: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)


def audit_parquet_file(path: Path) -> FileAudit:
    reasons: List[str] = []
    df, schema_err = _read_parquet_price_columns(path)
    if schema_err:
        return FileAudit(path=str(path), ok=False, reasons=[schema_err])

    df["timestamp"] = _coerce_utc_timestamps(df["timestamp"])
    n = len(df)
    if n == 0:
        return FileAudit(path=str(path), ok=False, rows=0, reasons=["empty_file"])

    inferred = _infer_symbol_from_filename(path)
    if "symbol" not in df.columns:
        if not inferred:
            return FileAudit(path=str(path), ok=False, rows=n, reasons=["missing_symbol_column"])
        df = df.copy()
        df["symbol"] = inferred

    df = df.dropna(subset=["timestamp", "symbol"])
    df = df[df["symbol"].astype(str).str.len() > 0]
    n = len(df)
    if n == 0:
        return FileAudit(path=str(path), ok=False, reasons=["no_valid_rows"])

    sym_set = sorted({str(s).strip().upper() for s in df["symbol"].dropna() if str(s).strip()})

    bad_px = (~df["price"].map(_is_good_price)).sum()
    bad_rate = bad_px / max(n, 1)
    if bad_rate > MAX_BAD_PRICE_RATE:
        reasons.append(f"bad_price_rate>{MAX_BAD_PRICE_RATE}:{bad_rate:.6f}")

    dup = df.duplicated(subset=["symbol", "timestamp"]).sum()
    dup_rate = dup / max(n, 1)
    if dup_rate > MAX_DUP_KEY_RATE:
        reasons.append(f"dup_rate>{MAX_DUP_KEY_RATE}:{dup_rate:.6f}")

    ok_day, day_reason = _filename_day_consistent(path, df)
    if not ok_day:
        reasons.append(day_reason)

    med = _median_bar_seconds(df)
    interval = classify_interval_from_median_seconds(med)
    if interval is None:
        reasons.append(f"unclassified_bar_spacing_median_sec={med}")

    ok = len(reasons) == 0
    return FileAudit(
        path=str(path),
        ok=ok,
        rows=n,
        interval=interval,
        median_bar_seconds=med if med == med else None,
        bad_price_rate=float(bad_rate),
        dup_rate=float(dup_rate),
        symbols=sym_set,
        reasons=reasons,
    )


def _df_to_ohlc_rows(df: pd.DataFrame) -> Dict[str, List[Tuple[str, float, float, float, float, Optional[float], Optional[float]]]]:
    """Build upsert row tuples without ``iterrows`` (too slow on multi-million-row loads)."""
    df = df.sort_values(["symbol", "timestamp"])
    df = df[df["price"].map(_is_good_price)]
    if df.empty:
        return {}
    ts = pd.to_datetime(df["timestamp"], utc=True)
    ts_iso = ts.map(lambda t: _utc_iso(t.to_pydatetime()))
    px = df["price"].astype(float)
    sym = df["symbol"].astype(str).str.strip().str.upper()
    tmp = pd.DataFrame({"sym": sym, "ts_iso": ts_iso, "px": px})
    out: Dict[str, List[Tuple[str, float, float, float, float, Optional[float], Optional[float]]]] = {}
    for s, g in tmp.groupby("sym", sort=False):
        rows = [(ti, p, p, p, p, None, None) for ti, p in zip(g["ts_iso"], g["px"])]
        if rows:
            out[s] = rows
    return out


def ingest_parquet_file(
    con: sqlite3.Connection,
    path: Path,
    *,
    audits_by_path: Dict[str, FileAudit],
) -> Dict[str, int]:
    """Insert rows for one file; callers commit. Uses conflict ignore everywhere."""
    au = audits_by_path.get(str(path))
    if not au or not au.ok or not au.interval:
        return {"skipped": 1, "rows_attempted": 0}
    use_interval = au.interval
    if use_interval not in ("1h", "1m", "5m", "1d"):
        return {"skipped": 1, "rows_attempted": 0}
    df, schema_err = _read_parquet_price_columns(path)
    if schema_err:
        return {"skipped": 1, "rows_attempted": 0}
    df["timestamp"] = _coerce_utc_timestamps(df["timestamp"])
    inferred = _infer_symbol_from_filename(path)
    if "symbol" not in df.columns and inferred:
        df = df.copy()
        df["symbol"] = inferred
    df = df.dropna(subset=["timestamp", "symbol"])
    df = df[df["symbol"].astype(str).str.len() > 0]
    by_sym = _df_to_ohlc_rows(df)
    if not by_sym:
        return {"skipped": 1, "rows_attempted": 0}

    n_attempt = 0
    for sym, rows in by_sym.items():
        n_attempt += len(rows)
        if use_interval == "1h":
            upsert_intraday_rows(
                con,
                "prices_hourly",
                sym,
                rows,
                source=SOURCE_TAG,
                commit=False,
                on_conflict="ignore",
            )
        elif use_interval == "1m":
            upsert_intraday_rows(
                con,
                "prices_minute",
                sym,
                rows,
                source=SOURCE_TAG,
                commit=False,
                on_conflict="ignore",
            )
        else:
            upsert_price_rows(
                con,
                sym,
                rows,
                interval=use_interval,
                source=SOURCE_TAG,
                commit=False,
                on_conflict="ignore",
            )
    return {"skipped": 0, "rows_attempted": n_attempt}


def run_audit(paths: List[Path]) -> Tuple[List[FileAudit], Dict[str, object]]:
    audits = [audit_parquet_file(p) for p in paths]
    n = len(audits)
    n_ok = sum(1 for a in audits if a.ok)
    summary = {
        "files_total": n,
        "files_pass": n_ok,
        "files_fail": n - n_ok,
        "fail_rate": (1.0 - n_ok / n) if n else 0.0,
        "interval_counts": {},
    }
    for a in audits:
        if a.ok and a.interval:
            summary["interval_counts"][a.interval] = summary["interval_counts"].get(a.interval, 0) + 1
    return audits, summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Audit / import historical Parquet prices")
    parser.add_argument(
        "--dir",
        type=str,
        default=None,
        help=f"Root folder (default: telegram_agent/{DEFAULT_DATA_DIR_NAME})",
    )
    parser.add_argument(
        "--subdir",
        type=str,
        default="",
        help="Only scan dir/subdir (e.g. parquet_days). Empty = entire tree under --dir.",
    )
    parser.add_argument("--audit-only", action="store_true", help="Run audit and print JSON; no DB writes")
    parser.add_argument("--ingest", action="store_true", help="After audit, insert if fail_rate is acceptable")
    parser.add_argument("--limit-files", type=int, default=0, help="Process only first N files (sorted paths)")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    _load_env_file(root / ".env")
    _load_env_file(Path(__file__).resolve().parent / ".env")
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    data_root = Path(args.dir) if args.dir else _default_data_dir()
    paths = discover_parquet_files(data_root, subdir=args.subdir.strip())
    if args.limit_files and args.limit_files > 0:
        paths = paths[: args.limit_files]

    audits, summary = run_audit(paths)
    audit_payload = {
        "summary": summary,
        "failures_sample": [a.__dict__ for a in audits if not a.ok][:50],
    }
    print(json.dumps(audit_payload, indent=2, default=str))

    if not args.ingest:
        return

    if summary["files_total"] == 0:
        logger.info("No files to ingest.")
        return

    fail_rate = summary["fail_rate"]
    if fail_rate > MAX_AUDIT_FILE_FAIL_RATE:
        raise SystemExit(
            f"Abort ingest: audit fail_rate {fail_rate:.4f} exceeds max {MAX_AUDIT_FILE_FAIL_RATE}"
        )

    cfg = load_config()
    db_path = Path(cfg.get("agent_db_path") or (DATA_DIR / "agent.sqlite"))
    con = connect(db_path)
    init_db(con)
    by_path = {a.path: a for a in audits}
    all_syms: Set[str] = set()
    for a in audits:
        if a.ok and a.symbols:
            all_syms.update(a.symbols)
        elif a.ok:
            inf = _infer_symbol_from_filename(Path(a.path))
            if inf:
                all_syms.add(inf)
    if all_syms:
        ensure_instruments(con, sorted(all_syms))

    total_attempt = 0
    files_ingested = 0
    for path in paths:
        st = ingest_parquet_file(con, path, audits_by_path=by_path)
        if st.get("skipped"):
            continue
        total_attempt += int(st.get("rows_attempted", 0))
        files_ingested += 1
        con.commit()

    con.close()
    print(
        json.dumps(
            {
                "ingest": True,
                "files_ingested": files_ingested,
                "rows_insert_attempted": total_attempt,
                "note": "conflicts skipped; existing yfinance rows unchanged",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
