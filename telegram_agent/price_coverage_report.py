"""
Price coverage + quality report for the agent SQLite database.

Produces per-symbol stats for:
- minute bars (prices_minute)
- hourly bars (prices_hourly)
- daily bars (prices interval='1d')

Outputs both JSON and CSV for easy inspection / diffing over time.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from telegram_agent.agent_db import connect, init_db


def _utcnow_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso_or_none(x: Optional[str]) -> Optional[str]:
    s = (x or "").strip()
    return s or None


@dataclass(frozen=True)
class SeriesStats:
    symbol: str
    timeframe: str  # 1m | 1h | 1d
    table: str
    n_rows: int
    min_ts_utc: Optional[str]
    max_ts_utc: Optional[str]
    n_distinct_days_utc: int
    expected_rows_between_min_max: Optional[int]
    coverage_between_min_max: Optional[float]
    null_close_n: int
    nonpositive_close_n: int
    null_volume_n: int
    max_gap_seconds: Optional[int]
    gaps_gt_2x_step_n: Optional[int]


def _iter_symbols(con) -> List[str]:
    cur = con.execute(
        """
        SELECT DISTINCT symbol FROM (
            SELECT symbol FROM prices
            UNION
            SELECT symbol FROM prices_hourly
            UNION
            SELECT symbol FROM prices_minute
        )
        ORDER BY symbol
        """
    )
    return [str(r[0]) for r in cur.fetchall() if r and r[0]]


def _grouped_base_stats(
    con,
    *,
    table: str,
    interval: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Return {SYMBOL: {n, mn, mx, ndays, null_close, nonpos_close, null_vol}} for a table.
    Uses one grouped SQL query (fast) instead of per-symbol scans (slow).
    """
    if table == "prices":
        if not interval:
            raise ValueError("interval required when table='prices'")
        cur = con.execute(
            """
            SELECT
              symbol AS symbol,
              COUNT(*) AS n,
              MIN(ts_utc) AS mn,
              MAX(ts_utc) AS mx,
              COUNT(DISTINCT SUBSTR(ts_utc, 1, 10)) AS ndays,
              SUM(CASE WHEN close IS NULL THEN 1 ELSE 0 END) AS null_close,
              SUM(CASE WHEN close IS NOT NULL AND close <= 0 THEN 1 ELSE 0 END) AS nonpos_close,
              SUM(CASE WHEN volume IS NULL THEN 1 ELSE 0 END) AS null_vol
            FROM prices
            WHERE interval = ?
            GROUP BY symbol
            """,
            (str(interval),),
        )
    else:
        if table not in ("prices_minute", "prices_hourly"):
            raise ValueError("table must be prices, prices_minute, or prices_hourly")
        cur = con.execute(
            f"""
            SELECT
              symbol AS symbol,
              COUNT(*) AS n,
              MIN(ts_utc) AS mn,
              MAX(ts_utc) AS mx,
              COUNT(DISTINCT SUBSTR(ts_utc, 1, 10)) AS ndays,
              SUM(CASE WHEN close IS NULL THEN 1 ELSE 0 END) AS null_close,
              SUM(CASE WHEN close IS NOT NULL AND close <= 0 THEN 1 ELSE 0 END) AS nonpos_close,
              SUM(CASE WHEN volume IS NULL THEN 1 ELSE 0 END) AS null_vol
            FROM {table}
            GROUP BY symbol
            """
        )
    out: Dict[str, Dict[str, Any]] = {}
    for r in cur.fetchall():
        sym = str(r["symbol"]).strip().upper()
        out[sym] = {
            "n": int(r["n"] or 0),
            "mn": _iso_or_none(r["mn"]),
            "mx": _iso_or_none(r["mx"]),
            "ndays": int(r["ndays"] or 0),
            "null_close": int(r["null_close"] or 0),
            "nonpos_close": int(r["nonpos_close"] or 0),
            "null_vol": int(r["null_vol"] or 0),
        }
    return out


def _global_range(con, *, table: str, interval: Optional[str] = None) -> Tuple[Optional[str], Optional[str], int]:
    if table == "prices" and interval:
        cur = con.execute(
            """
            SELECT MIN(ts_utc) AS mn, MAX(ts_utc) AS mx, COUNT(*) AS n
            FROM prices
            WHERE interval = ?
            """,
            (str(interval),),
        )
    else:
        cur = con.execute(f"SELECT MIN(ts_utc) AS mn, MAX(ts_utc) AS mx, COUNT(*) AS n FROM {table}")
    row = cur.fetchone()
    if not row:
        return None, None, 0
    return _iso_or_none(row["mn"]), _iso_or_none(row["mx"]), int(row["n"] or 0)


def _expected_rows(min_ts: Optional[str], max_ts: Optional[str], *, step_seconds: int) -> Optional[int]:
    if not min_ts or not max_ts:
        return None
    try:
        a = int(datetime.fromisoformat(min_ts).timestamp())
        b = int(datetime.fromisoformat(max_ts).timestamp())
    except Exception:
        return None
    if b < a:
        return None
    return int((b - a) // int(step_seconds) + 1)


def _series_stats(
    con,
    *,
    symbol: str,
    timeframe: str,
    table: str,
    interval: Optional[str],
    step_seconds: int,
    max_rows_for_gap_diagnostics: int,
    base: Optional[Dict[str, Any]] = None,
) -> SeriesStats:
    sym = str(symbol).strip().upper()
    b = base or {}
    n = int(b.get("n") or 0)
    mn = _iso_or_none(b.get("mn"))
    mx = _iso_or_none(b.get("mx"))
    ndays = int(b.get("ndays") or 0)
    null_close = int(b.get("null_close") or 0)
    nonpos_close = int(b.get("nonpos_close") or 0)
    null_vol = int(b.get("null_vol") or 0)

    exp = _expected_rows(mn, mx, step_seconds=step_seconds) if n > 0 else None
    cov = (float(n) / float(exp)) if (exp is not None and exp > 0) else None

    max_gap = None
    n_big = None
    if n >= 2 and n <= int(max_rows_for_gap_diagnostics):
        # Gap diagnostics with SQLite window functions; uses indexed scan by symbol.
        # We treat a "big gap" as > 2 * expected step.
        gap_thr = int(2 * int(step_seconds))
        if table == "prices":
            gap_sql = """
            WITH t AS (
              SELECT
                CAST(strftime('%s', ts_utc) AS INTEGER) AS s,
                LAG(CAST(strftime('%s', ts_utc) AS INTEGER)) OVER (ORDER BY ts_utc) AS p
              FROM prices
              WHERE symbol = ? AND interval = ?
              ORDER BY ts_utc
            )
            SELECT
              MAX(CASE WHEN p IS NULL THEN NULL ELSE (s - p) END) AS max_gap,
              SUM(CASE WHEN p IS NULL THEN 0 WHEN (s - p) > ? THEN 1 ELSE 0 END) AS n_big
            FROM t
            """
            cur2 = con.execute(gap_sql, (sym, str(interval), gap_thr))
        else:
            gap_sql = f"""
            WITH t AS (
              SELECT
                CAST(strftime('%s', ts_utc) AS INTEGER) AS s,
                LAG(CAST(strftime('%s', ts_utc) AS INTEGER)) OVER (ORDER BY ts_utc) AS p
              FROM {table}
              WHERE symbol = ?
              ORDER BY ts_utc
            )
            SELECT
              MAX(CASE WHEN p IS NULL THEN NULL ELSE (s - p) END) AS max_gap,
              SUM(CASE WHEN p IS NULL THEN 0 WHEN (s - p) > ? THEN 1 ELSE 0 END) AS n_big
            FROM t
            """
            cur2 = con.execute(gap_sql, (sym, gap_thr))
        r2 = cur2.fetchone()
        if r2 is not None:
            max_gap = int(r2["max_gap"]) if r2["max_gap"] is not None else None
            n_big = int(r2["n_big"]) if r2["n_big"] is not None else 0

    return SeriesStats(
        symbol=sym,
        timeframe=str(timeframe),
        table=str(table),
        n_rows=n,
        min_ts_utc=mn,
        max_ts_utc=mx,
        n_distinct_days_utc=ndays,
        expected_rows_between_min_max=exp,
        coverage_between_min_max=cov,
        null_close_n=null_close,
        nonpositive_close_n=nonpos_close,
        null_volume_n=null_vol,
        max_gap_seconds=max_gap,
        gaps_gt_2x_step_n=n_big,
    )


def build_report(db_path: Path, *, max_rows_for_gap_diagnostics: int) -> Dict[str, Any]:
    con = connect(db_path)
    try:
        init_db(con)
        symbols = _iter_symbols(con)

        # Global ranges help interpret per-symbol coverage.
        g_1m = _global_range(con, table="prices_minute")
        g_1h = _global_range(con, table="prices_hourly")
        g_1d = _global_range(con, table="prices", interval="1d")

        # One grouped scan per table.
        base_1m = _grouped_base_stats(con, table="prices_minute")
        base_1h = _grouped_base_stats(con, table="prices_hourly")
        base_1d = _grouped_base_stats(con, table="prices", interval="1d")

        out_rows: List[SeriesStats] = []
        for s in symbols:
            su = str(s).strip().upper()
            out_rows.append(
                _series_stats(
                    con,
                    symbol=su,
                    timeframe="1m",
                    table="prices_minute",
                    interval=None,
                    step_seconds=60,
                    max_rows_for_gap_diagnostics=max_rows_for_gap_diagnostics,
                    base=base_1m.get(su),
                )
            )
            out_rows.append(
                _series_stats(
                    con,
                    symbol=su,
                    timeframe="1h",
                    table="prices_hourly",
                    interval=None,
                    step_seconds=3600,
                    max_rows_for_gap_diagnostics=max_rows_for_gap_diagnostics,
                    base=base_1h.get(su),
                )
            )
            out_rows.append(
                _series_stats(
                    con,
                    symbol=su,
                    timeframe="1d",
                    table="prices",
                    interval="1d",
                    step_seconds=86400,
                    max_rows_for_gap_diagnostics=max_rows_for_gap_diagnostics,
                    base=base_1d.get(su),
                )
            )

        return {
            "generated_ts_utc": datetime.now(timezone.utc).isoformat(),
            "db_path": str(db_path),
            "symbols_n": len(symbols),
            "global_ranges": {
                "1m": {"table": "prices_minute", "min_ts_utc": g_1m[0], "max_ts_utc": g_1m[1], "n_rows": g_1m[2]},
                "1h": {"table": "prices_hourly", "min_ts_utc": g_1h[0], "max_ts_utc": g_1h[1], "n_rows": g_1h[2]},
                "1d": {"table": "prices(interval='1d')", "min_ts_utc": g_1d[0], "max_ts_utc": g_1d[1], "n_rows": g_1d[2]},
            },
            "rows": [asdict(r) for r in out_rows],
        }
    finally:
        try:
            con.close()
        except Exception:
            pass


def _write_csv(rows: Iterable[Dict[str, Any]], path: Path) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # Stable column order.
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Measure price coverage/quality per symbol (1m/1h/1d).")
    ap.add_argument(
        "--db",
        default="telegram_agent/data/agent.sqlite",
        help="Path to agent sqlite (default: telegram_agent/data/agent.sqlite)",
    )
    ap.add_argument(
        "--out-dir",
        default="telegram_agent/data",
        help="Output directory for report files (default: telegram_agent/data)",
    )
    ap.add_argument(
        "--max-rows-for-gap-diagnostics",
        default=200000,
        type=int,
        help="Skip expensive max-gap diagnostics when a series has more than this many rows (default: 200000).",
    )
    args = ap.parse_args(argv)

    db_path = Path(str(args.db)).expanduser()
    out_dir = Path(str(args.out_dir)).expanduser()
    stamp = _utcnow_stamp()

    report = build_report(db_path, max_rows_for_gap_diagnostics=int(args.max_rows_for_gap_diagnostics))
    json_p = out_dir / f"price_coverage_report_{stamp}.json"
    csv_p = out_dir / f"price_coverage_report_{stamp}.csv"

    json_p.write_text(json.dumps(report, indent=2, sort_keys=False, default=str), encoding="utf-8")
    _write_csv(report["rows"], csv_p)
    print(str(json_p))
    print(str(csv_p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

