#!/usr/bin/env python3
"""
Download Macrotrends-style fundamentals for many symbols via RapidAPI (see ``value_metrics_provider_macrotrends``).

Macrotrends.net does not publish an official API; this uses the RapidAPI Macrotrends Finance product
(``macrotrends-finance1`` by default). Set::

  export RAPIDAPI_KEY="your-key"

Optional::

  export MACROTRENDS_RAPIDAPI_HOST="macrotrends-finance1.p.rapidapi.com"

Symbol list: ``SP500_SYMBOLS`` (comma-separated), or ``--sp500-json`` (default
``backend/data/sp500_symbols.json`` from ``backend/sp500_quality_evaluator.py``), or ``--symbols-file``.

Rate limits: RapidAPI free tiers are often ~100 requests/month; fetching statements + price + estimates
for 500 names is ~1500 calls. Use a paid plan or ``--financial-only`` and/or ``--max-symbols`` for tests.

Output: one JSON file per ticker under ``--out-dir`` (default ``backend/data/macrotrends_sp500``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "backend"))

from dotenv import load_dotenv

from value_metrics_provider_macrotrends import (
    fetch_all_fundamentals_bundle,
    fetch_earnings_estimates,
    fetch_financial_statements,
    fetch_price_history,
    normalize_ticker,
)


def _utc_meta() -> Dict[str, str]:
    from datetime import datetime, timezone

    return {"fetched_ts_utc": datetime.now(timezone.utc).isoformat()}


def _load_env() -> None:
    for p in (_REPO_ROOT / ".env", _REPO_ROOT / "backend" / ".env"):
        if p.is_file():
            load_dotenv(dotenv_path=p, override=False)


def _parse_csv_symbols(s: str) -> List[str]:
    return sorted({normalize_ticker(x) for x in (s or "").split(",") if x.strip()})


def _load_symbols_from_json(path: Path) -> List[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("symbols"), list):
        return sorted({normalize_ticker(str(x)) for x in raw["symbols"] if str(x).strip()})
    if isinstance(raw, list):
        return sorted({normalize_ticker(str(x)) for x in raw if str(x).strip()})
    raise ValueError(f"Unexpected JSON shape in {path}")


def _resolve_symbols(
    *,
    symbols_csv: str,
    symbols_file: str,
    sp500_json: Path,
) -> List[str]:
    if symbols_csv.strip():
        return _parse_csv_symbols(symbols_csv)
    if symbols_file.strip():
        return _load_symbols_from_json(Path(symbols_file).expanduser())
    env = (os.getenv("SP500_SYMBOLS") or "").strip()
    if env:
        return _parse_csv_symbols(env)
    if sp500_json.is_file():
        return _load_symbols_from_json(sp500_json)
    raise RuntimeError(
        "No symbol source: pass --symbols, --symbols-file, set SP500_SYMBOLS, "
        f"or create {sp500_json} (e.g. run backend/sp500_quality_evaluator.py once)."
    )


def _fetch_one(
    sym: str,
    *,
    financial_only: bool,
    include_price: bool,
    include_estimates: bool,
) -> Dict[str, Any]:
    sym = normalize_ticker(sym)
    if financial_only:
        return {"symbol": sym, "financial_statements": fetch_financial_statements(sym)}
    if not include_price and not include_estimates:
        return {"symbol": sym, "financial_statements": fetch_financial_statements(sym)}
    out: Dict[str, Any] = {"symbol": sym}
    out["financial_statements"] = fetch_financial_statements(sym)
    if include_price:
        try:
            out["price_history"] = fetch_price_history(sym)
        except Exception as e:
            out["price_history"] = {"error": str(e)}
    if include_estimates:
        try:
            out["earnings_estimates"] = fetch_earnings_estimates(sym)
        except Exception as e:
            out["earnings_estimates"] = {"error": str(e)}
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fetch Macrotrends (RapidAPI) fundamentals for S&P 500 or custom symbol lists."
    )
    ap.add_argument(
        "--out-dir",
        type=str,
        default="",
        help="Directory for per-ticker JSON (default: backend/data/macrotrends_sp500)",
    )
    ap.add_argument(
        "--sp500-json",
        type=str,
        default="",
        help="Path to sp500_symbols.json (default: backend/data/sp500_symbols.json)",
    )
    ap.add_argument("--symbols", type=str, default="", help="Comma-separated tickers (overrides other sources)")
    ap.add_argument("--symbols-file", type=str, default="", help="JSON file with symbol list")
    ap.add_argument("--sleep", type=float, default=0.35, help="Pause between tickers (seconds)")
    ap.add_argument("--max-symbols", type=int, default=0, help="If >0, only first N symbols (debug)")
    ap.add_argument(
        "--financial-only",
        action="store_true",
        help="Only /financial-statements/{ticker} (1 call per symbol instead of 3)",
    )
    ap.add_argument("--no-price-history", action="store_true", help="Skip price history (unless --financial-only)")
    ap.add_argument("--no-earnings-estimates", action="store_true", help="Skip earnings estimates (unless --financial-only)")
    ap.add_argument("--force", action="store_true", help="Overwrite existing JSON files")
    args = ap.parse_args(list(argv) if argv is not None else None)

    _load_env()

    sp500_path = (
        Path(args.sp500_json).expanduser()
        if str(args.sp500_json).strip()
        else _REPO_ROOT / "backend" / "data" / "sp500_symbols.json"
    )
    try:
        symbols = _resolve_symbols(
            symbols_csv=args.symbols,
            symbols_file=args.symbols_file,
            sp500_json=sp500_path,
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if int(args.max_symbols) > 0:
        symbols = symbols[: int(args.max_symbols)]

    out_dir = (
        Path(args.out_dir).expanduser()
        if str(args.out_dir).strip()
        else _REPO_ROOT / "backend" / "data" / "macrotrends_sp500"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    include_price = not bool(args.no_price_history) and not bool(args.financial_only)
    include_estimates = not bool(args.no_earnings_estimates) and not bool(args.financial_only)

    manifest: Dict[str, Any] = {"symbols_requested": len(symbols), "rows": []}
    ok = 0
    failed = 0

    print(f"Output: {out_dir}")
    print(f"Symbols: {len(symbols)} (financial_only={args.financial_only}, price={include_price}, estimates={include_estimates})")

    for i, sym in enumerate(symbols):
        path = out_dir / f"{sym}.json"
        if path.is_file() and not args.force:
            manifest["rows"].append({"symbol": sym, "status": "skipped_exists"})
            ok += 1
            continue
        try:
            payload = _fetch_one(
                sym,
                financial_only=bool(args.financial_only),
                include_price=include_price,
                include_estimates=include_estimates,
            )
            payload["_meta"] = _utc_meta()
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            manifest["rows"].append({"symbol": sym, "status": "ok"})
            ok += 1
        except Exception as e:
            manifest["rows"].append({"symbol": sym, "status": "error", "error": str(e)})
            failed += 1
            print(f"  [{sym}] {e}", file=sys.stderr)
        time.sleep(max(0.0, float(args.sleep)))
        if (i + 1) % 50 == 0:
            print(f"  progress {i + 1}/{len(symbols)} ok={ok} failed={failed}")

    manifest_path = out_dir / "manifest.json"
    manifest["summary"] = {"ok": ok, "failed": failed, "out_dir": str(out_dir)}
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    print(f"Done. ok={ok} failed={failed} manifest={manifest_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
