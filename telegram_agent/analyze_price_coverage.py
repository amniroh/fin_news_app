#!/usr/bin/env python3
"""
Analyze price table coverage, gaps, and data-quality flags (esp. P0/P1 universe).

  python -m telegram_agent.analyze_price_coverage
  python -m telegram_agent.analyze_price_coverage --json
  python -m telegram_agent.analyze_price_coverage --max-priority 1 --interval 1d
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env")

from telegram_agent.agent_db import connect, init_db, list_distinct_price_intervals
from telegram_agent.config import load_config
from telegram_agent.symbol_universe import normalize_symbol


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    t = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_universe_by_priority(
    path: Path, *, max_priority: int
) -> Tuple[List[str], Dict[str, int]]:
    """Symbols with priority <= max_priority; returns ordered list + priority map."""
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    items: List[Tuple[str, int]] = []
    if not isinstance(data, list):
        return [], {}
    for x in data:
        if not isinstance(x, dict):
            continue
        t = x.get("ticker") or x.get("symbol") or x.get("Ticker")
        if t is None or not str(t).strip():
            continue
        pr = x.get("priority")
        try:
            pri = int(pr) if pr is not None else 999
        except Exception:
            pri = 999
        if pri <= max_priority:
            sym = normalize_symbol(str(t))
            items.append((sym, pri))
    items.sort(key=lambda z: (z[1], z[0]))
    syms = [s for s, _ in items]
    pmap = {s: p for s, p in items}
    return syms, pmap


def _interval_rows(
    con: Any,
    symbol: str,
    interval: str,
) -> List[Tuple[datetime, Optional[float], Optional[float]]]:
    cur = con.execute(
        """
        SELECT ts_utc, adj_close, close FROM prices
        WHERE symbol = ? AND interval = ?
        ORDER BY ts_utc ASC
        """,
        (symbol.upper(), interval),
    )
    out: List[Tuple[datetime, Optional[float], Optional[float]]] = []
    for row in cur.fetchall():
        ts = _parse_ts(str(row["ts_utc"]))
        if not ts:
            continue
        adj = row["adj_close"]
        cl = row["close"]
        a = float(adj) if adj is not None else None
        c = float(cl) if cl is not None else None
        out.append((ts, a, c))
    return out


def analyze_symbol_series(
    rows: Sequence[Tuple[datetime, Optional[float], Optional[float]]],
    *,
    jump_threshold: float = 0.35,
    gap_warn_days: int = 10,
) -> Dict[str, Any]:
    if len(rows) < 2:
        return {
            "n_bars": len(rows),
            "max_gap_days": None,
            "large_jumps": 0,
            "missing_adj": 0,
            "median_gap_days": None,
        }
    gaps: List[float] = []
    jumps = 0
    missing_adj = 0
    prev_px: Optional[float] = None
    prev_ts: Optional[datetime] = None
    for ts, adj, cl in rows:
        px = adj if adj is not None and adj > 0 else (cl if cl and cl > 0 else None)
        if adj is None and cl is None:
            missing_adj += 1
        if prev_ts is not None:
            delta = (ts - prev_ts).total_seconds() / 86400.0
            gaps.append(delta)
        if prev_px is not None and px is not None and prev_px > 0:
            r = abs(px / prev_px - 1.0)
            if r > jump_threshold:
                jumps += 1
        prev_ts = ts
        prev_px = px if px is not None else prev_px

    gaps_sorted = sorted(gaps) if gaps else []
    med = gaps_sorted[len(gaps_sorted) // 2] if gaps_sorted else None
    mx = max(gaps) if gaps else None
    return {
        "n_bars": len(rows),
        "max_gap_days": round(mx, 3) if mx is not None else None,
        "median_gap_days": round(med, 3) if med is not None else None,
        "gaps_over_warn": sum(1 for g in gaps if g > gap_warn_days),
        "large_jumps": jumps,
        "missing_adj_or_close": missing_adj,
    }


def run_analysis(
    cfg: dict,
    *,
    max_priority: int,
    interval: str,
    jump_threshold: float,
    gap_warn_days: int,
) -> Dict[str, Any]:
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    path_raw = cfg.get("symbol_universe_path") or ""
    if not path_raw:
        return {"ok": False, "error": "symbol_universe_path not set in config"}
    uni_path = Path(path_raw)
    if not uni_path.is_file():
        return {"ok": False, "error": f"universe file not found: {uni_path}"}

    p0p1, pmap = load_universe_by_priority(uni_path, max_priority=max_priority)
    con = connect(db)
    init_db(con)

    intervals = list_distinct_price_intervals(con)
    global_counts: Dict[str, int] = {}
    cur = con.execute(
        "SELECT interval, COUNT(*) AS n FROM prices GROUP BY interval ORDER BY n DESC"
    )
    for row in cur.fetchall():
        global_counts[str(row["interval"])] = int(row["n"])

    cur = con.execute(
        "SELECT COUNT(DISTINCT symbol) AS n FROM prices WHERE interval = ?",
        (interval,),
    )
    distinct_syms_with_prices = int(cur.fetchone()["n"] or 0)

    missing: List[str] = []
    thin: List[Dict[str, Any]] = []
    flagged: List[Dict[str, Any]] = []

    for sym in p0p1:
        rows = _interval_rows(con, sym, interval)
        if not rows:
            missing.append(sym)
            continue
        st = analyze_symbol_series(
            rows, jump_threshold=jump_threshold, gap_warn_days=gap_warn_days
        )
        st["symbol"] = sym
        st["priority"] = pmap.get(sym, max_priority)
        t0, t1 = rows[0][0], rows[-1][0]
        st["first_ts"] = t0.isoformat()
        st["last_ts"] = t1.isoformat()
        st["span_calendar_days"] = (t1 - t0).days
        if st["n_bars"] < 60:
            thin.append(st)
        if (
            (st.get("large_jumps") or 0) > 0
            or (st.get("gaps_over_warn") or 0) > 0
            or st["n_bars"] < 60
        ):
            flagged.append(st)

    con.close()

    n_uni = len(p0p1)
    n_ok = n_uni - len(missing)
    return {
        "ok": True,
        "db_path": str(db),
        "universe_path": str(uni_path),
        "interval": interval,
        "max_priority_included": max_priority,
        "universe_count_p0_to_pN": n_uni,
        "intervals_in_db": intervals,
        "global_row_counts_by_interval": global_counts,
        "distinct_symbols_with_interval": distinct_syms_with_prices,
        "symbols_missing_price_rows": missing,
        "symbols_with_price_rows": n_ok,
        "coverage_pct": round(100.0 * n_ok / n_uni, 2) if n_uni else 0.0,
        "thin_symbols_lt_60_bars": thin,
        "quality_flags_jump_gt": jump_threshold,
        "quality_flags_gap_gt_days": gap_warn_days,
        "flagged_symbols_detail": sorted(
            flagged, key=lambda x: (-(x.get("large_jumps") or 0), -x["n_bars"])
        ),
        "recommendations": _recommendations(
            missing=missing,
            thin=thin,
            global_counts=global_counts,
            interval=interval,
            n_uni=n_uni,
        ),
    }


def _recommendations(
    *,
    missing: Sequence[str],
    thin: Sequence[Dict[str, Any]],
    global_counts: Dict[str, int],
    interval: str,
    n_uni: int,
) -> List[str]:
    tips: List[str] = []
    if missing:
        tips.append(
            f"Run `python -m telegram_agent.agent prices --mode backfill` (or incremental) "
            f"so yfinance fills {len(missing)} missing P0/P1 symbols for `{interval}`."
        )
    if thin:
        tips.append(
            f"{len(thin)} symbols have fewer than 60 `{interval}` bars — widen backfill window "
            f"(AGENT_BACKFILL_DAYS / prices backfill) or check delisted/illiquid tickers."
        )
    if not global_counts.get(interval):
        tips.append(f"No rows in `prices` for interval `{interval}` — ingest daily prices first.")
    elif global_counts.get(interval, 0) < n_uni * 100:
        tips.append(
            "Total row count for this interval looks low vs universe size × history — "
            "consider a full universe price backfill on the server with stable network."
        )
    tips.append(
        "Large |day-over-day| jumps in `adj_close` often indicate splits/dividends or bad ticks; "
        "yfinance `adj_close` usually fixes splits — verify symbols with many `large_jumps` on Yahoo."
    )
    tips.append(
        "For intraday backtests, ingest `1h`/`1m` data separately; current pipeline is mostly `1d`."
    )
    return tips


def main() -> None:
    p = argparse.ArgumentParser(description="Price coverage & quality for universe priorities")
    p.add_argument(
        "--max-priority",
        type=int,
        default=1,
        help="Include universe priorities 0..N (default 1 = P0 and P1)",
    )
    p.add_argument("--interval", type=str, default="1d", help="Price interval (default 1d)")
    p.add_argument(
        "--jump-threshold",
        type=float,
        default=0.35,
        help="Flag consecutive bar abs return > this (default 0.35 = 35%%)",
    )
    p.add_argument(
        "--gap-warn-days",
        type=int,
        default=10,
        help="Count gaps longer than this many calendar days between consecutive bars",
    )
    p.add_argument("--json", action="store_true", help="Machine-readable JSON only")
    args = p.parse_args()
    cfg = load_config()
    out = run_analysis(
        cfg,
        max_priority=args.max_priority,
        interval=args.interval,
        jump_threshold=args.jump_threshold,
        gap_warn_days=args.gap_warn_days,
    )
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return
    if not out.get("ok"):
        print("Error:", out.get("error"))
        sys.exit(1)
    print("=== Price coverage & quality ===")
    print(f"DB: {out['db_path']}")
    print(f"Universe file: {out['universe_path']}")
    print(f"Interval: {out['interval']}  |  priorities ≤ {out['max_priority_included']}")
    print(f"Universe size (P0–P{out['max_priority_included']}): {out['universe_count_p0_to_pN']}")
    print(f"Symbols with ≥1 `{out['interval']}` row: {out['symbols_with_price_rows']} "
          f"({out['coverage_pct']}% coverage)")
    print(f"Symbols missing entirely: {len(out['symbols_missing_price_rows'])}")
    g = out["global_row_counts_by_interval"]
    print(f"Intervals present in DB: {', '.join(out['intervals_in_db']) or '(none)'}")
    print(f"Row counts by interval: {g}")
    print(f"Distinct symbols with `{out['interval']}`: {out['distinct_symbols_with_interval']}")
    print()
    if out["symbols_missing_price_rows"][:40]:
        print("Missing (first 40):", ", ".join(out["symbols_missing_price_rows"][:40]))
        if len(out["symbols_missing_price_rows"]) > 40:
            print(f"... and {len(out['symbols_missing_price_rows']) - 40} more")
        print()
    if out["thin_symbols_lt_60_bars"]:
        print(f"Thin history (<60 bars): {len(out['thin_symbols_lt_60_bars'])} symbols")
        for row in out["thin_symbols_lt_60_bars"][:15]:
            print(
                f"  {row['symbol']} (P{row['priority']}) bars={row['n_bars']} "
                f"{row['first_ts'][:10]} .. {row['last_ts'][:10]}"
            )
        if len(out["thin_symbols_lt_60_bars"]) > 15:
            print(f"  ... +{len(out['thin_symbols_lt_60_bars']) - 15} more")
        print()
    print("Top flagged (jumps / long gaps / thin):")
    for row in out["flagged_symbols_detail"][:20]:
        print(
            f"  {row['symbol']} P{row['priority']}  bars={row['n_bars']}  "
            f"jumps>{out['quality_flags_jump_gt']}: {row.get('large_jumps', 0)}  "
            f"gaps>{out['quality_flags_gap_gt_days']}d: {row.get('gaps_over_warn', 0)}  "
            f"max_gap={row.get('max_gap_days')}d"
        )
    print()
    print("Recommendations:")
    for t in out["recommendations"]:
        print(f"  • {t}")


if __name__ == "__main__":
    main()
