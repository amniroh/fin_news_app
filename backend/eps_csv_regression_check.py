#!/usr/bin/env python3
"""
Compare downloaded quarterly EPS CSVs against SQLite vm_fundamental_points.

This is meant as a sanity check after running:
  backend/value_metrics_daily_backfill.py --provider sec ...

CSV format expected (as provided by user):
  date,eps
  2022-06-30,$1.13
  ...
or:
  quarter date,EPS
  2012-03-31,$0.44
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "backend"))

from value_metrics_store import connect, init_db, query_fundamental_points


def _parse_eps(s: str) -> Optional[float]:
    t = (s or "").strip()
    if not t:
        return None
    neg = False
    if t.startswith("-"):
        neg = True
        t = t[1:].strip()
    t = t.replace("$", "").replace(",", "").strip()
    try:
        v = float(t)
        return -v if neg else v
    except Exception:
        return None


def load_eps_csv(path: Path) -> Dict[str, float]:
    rows: Dict[str, float] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        # normalize headers
        fields = [str(x or "").strip().lower() for x in (r.fieldnames or [])]
        date_key = None
        eps_key = None
        for i, k in enumerate(fields):
            if k in ("date", "quarter date"):
                date_key = r.fieldnames[i]  # original name
            if k in ("eps",):
                eps_key = r.fieldnames[i]
        if date_key is None or eps_key is None:
            raise ValueError(f"CSV must have date + eps columns, got: {r.fieldnames}")
        for row in r:
            d = str(row.get(date_key) or "").strip()[:10]
            e = _parse_eps(str(row.get(eps_key) or ""))
            if d and e is not None:
                rows[d] = float(e)
    return rows


def load_db_eps(con, symbol: str, provider: str, start: Optional[str], end: Optional[str]) -> Dict[str, float]:
    pts = query_fundamental_points(
        con,
        symbols=[symbol],
        start_date=start,
        end_date=end,
        provider=provider,
        period="quarter",
    )
    out: Dict[str, float] = {}
    for p in pts:
        d = str(p.get("asof_date") or "").strip()[:10]
        eps = p.get("eps")
        if not d or eps is None:
            continue
        try:
            out[d] = float(eps)
        except Exception:
            continue
    return out


def compare_series(expected: Dict[str, float], actual: Dict[str, float]) -> Tuple[int, float, List[str]]:
    overlap = sorted(set(expected.keys()) & set(actual.keys()))
    if not overlap:
        return 0, 0.0, ["no_overlap_dates"]
    diffs = []
    worst = 0.0
    worst_items: List[str] = []
    for d in overlap:
        de = expected[d]
        da = actual[d]
        diff = abs(da - de)
        diffs.append(diff)
        if diff > worst:
            worst = diff
    # show up to 12 worst rows
    ranked = sorted(overlap, key=lambda d: abs(actual[d] - expected[d]), reverse=True)[:12]
    for d in ranked:
        worst_items.append(f"{d}: expected={expected[d]:.6g} actual={actual[d]:.6g} diff={abs(actual[d]-expected[d]):.6g}")
    return len(overlap), worst, worst_items


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Regression check: CSV EPS vs vm_fundamental_points")
    ap.add_argument("--db", type=str, default="", help="SQLite path (default: VALUE_METRICS_DB_PATH or backend/data/value_metrics.sqlite)")
    ap.add_argument("--provider", type=str, default="sec", help="Provider in DB (default: sec)")
    ap.add_argument("--start", type=str, default="", help="Optional start YYYY-MM-DD")
    ap.add_argument("--end", type=str, default="", help="Optional end YYYY-MM-DD")
    ap.add_argument("--tol", type=float, default=0.03, help="Max abs diff tolerated (default: 0.03)")
    ap.add_argument("pairs", nargs="+", help="Pairs like SYMBOL=/abs/path/to/file.csv")
    args = ap.parse_args(list(argv) if argv is not None else None)

    db_path = Path(args.db).expanduser() if args.db.strip() else Path(
        os.getenv("VALUE_METRICS_DB_PATH", str(_REPO_ROOT / "backend" / "data" / "value_metrics.sqlite"))
    ).expanduser()
    prov = str(args.provider).strip().lower()
    start = str(args.start).strip() or None
    end = str(args.end).strip() or None
    tol = float(args.tol)

    con = connect(db_path)
    init_db(con)
    try:
        failed = 0
        for pair in args.pairs:
            if "=" not in pair:
                raise ValueError(f"Bad pair '{pair}'. Use SYMBOL=/path/to.csv")
            sym, p = pair.split("=", 1)
            sym_u = sym.strip().upper()
            path = Path(p).expanduser()
            exp = load_eps_csv(path)
            act = load_db_eps(con, sym_u, prov, start, end)
            n, worst, worst_items = compare_series(exp, act)
            ok = (n > 0) and (worst <= tol)
            print(f"[{sym_u}] overlap_n={n} worst_abs_diff={worst:.6g} tol={tol} ok={ok}")
            for line in worst_items:
                print(f"  {line}")
            if not ok:
                failed += 1
        return 0 if failed == 0 else 2
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())

