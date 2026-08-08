#!/usr/bin/env python3
"""
Paper-trade Regression on technicals (walk-forward–tuned) via Interactive Brokers.

Loads the daily LightGBM bundle + latest fold ``chosen_params`` from
``backend/data/regression_technicals_models/``, scores the latest technicals panel,
and rebalances a dedicated paper account toward equal-weight top-N targets.

Default is dry-run. Use ``--execute`` (or ``IB_PAPER_EXECUTE=1``) to place orders.

Requires TWS / IB Gateway logged into a **paper** account with API enabled.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger("regression_paper_rebalance")

# Reuse IB helpers from the sibling ML paper bot.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from paper_rebalance import (  # noqa: E402
    _connect_ib,
    _current_stock_positions,
    _ib_ticker_to_cache_stem,
    _last_close_usd,
    _load_symbol_map,
    _net_liquidation_usd,
    _qualify_stock,
    cache_stem_to_ib_ticker,
)
from ib_safety import assert_paper_port, assert_trading_mode_paper, verify_ib_session_is_paper  # noqa: E402


def _backend_dir() -> Path:
    raw = (os.environ.get("ML_PAPER_BACKEND_DIR") or "").strip()
    if raw:
        p = Path(raw).expanduser().resolve()
        if (p / "regression_based_on_technicals.py").is_file():
            return p
        raise SystemExit(f"ML_PAPER_BACKEND_DIR={raw!r} must contain regression_based_on_technicals.py")
    here = Path(__file__).resolve()
    for parent in [here.parents[i] for i in range(2, 7)]:
        cand = parent / "backend" / "regression_based_on_technicals.py"
        if cand.is_file():
            return cand.parent.resolve()
    raise SystemExit(
        "Set ML_PAPER_BACKEND_DIR to the directory that contains regression_based_on_technicals.py"
    )


def _ensure_backend_path() -> Path:
    bd = _backend_dir()
    if str(bd) not in sys.path:
        sys.path.insert(0, str(bd))
    return bd


def _state_path() -> Path:
    raw = (os.environ.get("IB_PAPER_REGRESSION_STATE") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _backend_dir().parent / "backend" / "data" / "regression_technicals_models" / "ib_paper_state.json"


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {"symbols": [], "updated_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"symbols": [], "updated_at": None}


def _save_state(path: Path, symbols: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbols": sorted({s.upper() for s in symbols}),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": "regression_based_on_technicals",
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _predict_targets(top_n: Optional[int]) -> List[Dict[str, Any]]:
    _ensure_backend_path()
    from regression_based_on_technicals import predict_top_n  # noqa: WPS433

    return predict_top_n("daily", n=top_n)


def run_rebalance(
    *,
    dry_run: bool,
    check_connection: bool,
    top_n: Optional[int],
    deploy_fraction: float,
    max_order_usd: Optional[float],
    liquidate_non_targets: bool,
    symbol_map: Dict[str, str],
    ib_host: str,
    ib_port: int,
    ib_client_id: int,
    ib_account: Optional[str],
) -> None:
    assert_trading_mode_paper()
    assert_paper_port(ib_port)

    if check_connection:
        ib = _connect_ib(ib_host, ib_port, ib_client_id, ib_account)
        try:
            account = verify_ib_session_is_paper(ib, preferred_account=ib_account)
            nav = _net_liquidation_usd(ib, account)
            log.info("Connected PAPER account=%s NetLiq≈%.2f USD (port=%s)", account, nav, ib_port)
            log.info("check-connection: paper session verified; no orders placed")
        finally:
            ib.disconnect()
        return

    targets = _predict_targets(top_n)
    state_path = _state_path()
    prev = _load_state(state_path)
    prev_syms: Set[str] = {str(s).upper() for s in (prev.get("symbols") or [])}

    log.info("targets (%d) asof=%s params=%s", len(targets), (targets[0].get("asof_date") if targets else None), (targets[0].get("chosen_params") if targets else None))
    for r in targets[:20]:
        log.info(
            "  %s  pred=%.6f  w=%.4f",
            r.get("symbol"),
            float(r.get("predicted_return", 0)),
            float(r.get("weight", 0)),
        )
    if len(targets) > 20:
        log.info("  ...")

    if dry_run:
        log.info("dry-run: not connecting to IB (would manage %d targets; prev_state=%d)", len(targets), len(prev_syms))
        return

    ib = _connect_ib(ib_host, ib_port, ib_client_id, ib_account)
    try:
        account = verify_ib_session_is_paper(ib, preferred_account=ib_account)
        nav = _net_liquidation_usd(ib, account)
        log.info("Connected PAPER account=%s NetLiq≈%.2f USD (port=%s)", account, nav, ib_port)

        budget = nav * float(deploy_fraction)
        log.info("deploy_fraction=%.3f budget≈%.2f", deploy_fraction, budget)

        pos_by_ib_sym = _current_stock_positions(ib, account)
        current_by_stem: Dict[str, int] = {}
        for ibsym, sh in pos_by_ib_sym.items():
            stem = _ib_ticker_to_cache_stem(ibsym)
            current_by_stem[stem] = current_by_stem.get(stem, 0) + int(sh)

        stem_to_ib: Dict[str, str] = {
            str(t["symbol"]): cache_stem_to_ib_ticker(str(t["symbol"]), symbol_map) for t in targets
        }
        # Also qualify previous / non-target names we may need to sell.
        managed = set(stem_to_ib.keys())
        if liquidate_non_targets:
            managed |= prev_syms
            managed |= set(current_by_stem.keys())

        contracts: Dict[str, Any] = {}
        prices: Dict[str, float] = {}
        for stem in sorted(managed):
            ibsym = stem_to_ib.get(stem) or cache_stem_to_ib_ticker(stem, symbol_map)
            try:
                c = _qualify_stock(ib, ibsym)
            except Exception as e:
                log.warning("qualify failed for %s (%s): %s", stem, ibsym, e)
                continue
            contracts[stem] = c
            px = _last_close_usd(ib, c)
            if px is None or px <= 0:
                log.warning("no price for %s — skip", stem)
                continue
            prices[stem] = px

        target_shares: Dict[str, int] = {stem: 0 for stem in managed}
        for row in targets:
            stem = str(row["symbol"])
            if stem not in prices:
                continue
            w = float(row["weight"])
            usd = budget * w
            if max_order_usd is not None:
                usd = min(usd, float(max_order_usd))
            sh = int(math.floor(usd / prices[stem]))
            target_shares[stem] = max(0, sh)

        if not liquidate_non_targets:
            # Only touch current target names.
            target_shares = {s: target_shares.get(s, 0) for s in stem_to_ib}

        from ib_insync import MarketOrder  # type: ignore

        # Sells first, then buys.
        deltas = []
        for stem, tgt in target_shares.items():
            cur = int(current_by_stem.get(stem, 0))
            delta = int(tgt) - cur
            if delta != 0 and stem in contracts:
                deltas.append((stem, delta, cur, int(tgt)))
        deltas.sort(key=lambda x: x[1])  # sells (negative) first

        for stem, delta, cur, tgt in deltas:
            c = contracts[stem]
            action = "BUY" if delta > 0 else "SELL"
            qty = abs(delta)
            px = prices.get(stem, float("nan"))
            log.info("%s %s %d (cur=%d tgt=%d px=%.2f)", stem, action, qty, cur, tgt, px)
            ib.placeOrder(c, MarketOrder(action, qty))

        ib.sleep(1.0)
        _save_state(state_path, [str(t["symbol"]) for t in targets])
    finally:
        ib.disconnect()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="IB paper rebalance for regression_based_on_technicals")
    ap.add_argument("--dry-run", action="store_true", help="print targets only (no IB)")
    ap.add_argument("--execute", action="store_true", help="place paper orders via IB")
    ap.add_argument(
        "--check-connection",
        action="store_true",
        help="connect to IB, verify paper account (DU…), print NetLiq, place no orders",
    )
    ap.add_argument("--top-n", type=int, default=None, help="override bundle top_n (default: from walk-forward params)")
    ap.add_argument("--deploy-fraction", type=float, default=float(os.environ.get("IB_PAPER_DEPLOY_FRACTION", "0.95")))
    ap.add_argument("--max-order-usd", type=float, default=None)
    ap.add_argument(
        "--no-liquidate-non-targets",
        action="store_true",
        help="do not sell names that left the basket (default: liquidate for a dedicated paper account)",
    )
    ap.add_argument("--symbol-map", type=Path, default=None)
    ap.add_argument("--ib-host", default=os.environ.get("IB_HOST", "127.0.0.1"))
    # Gateway paper default (TWS paper is 7497)
    ap.add_argument("--ib-port", type=int, default=int(os.environ.get("IB_PORT", "4002")))
    ap.add_argument("--ib-client-id", type=int, default=int(os.environ.get("IB_CLIENT_ID", "61")))
    ap.add_argument("--ib-account", default=os.environ.get("IB_ACCOUNT") or None)
    args = ap.parse_args()

    env_execute = (os.environ.get("IB_PAPER_EXECUTE") or "").strip().lower() in ("1", "true", "yes")
    if args.execute and args.dry_run:
        ap.error("choose at most one of --execute and --dry-run")
    if args.check_connection and args.execute:
        ap.error("choose at most one of --check-connection and --execute")

    dry_run = not (bool(args.execute) or env_execute)
    if args.check_connection:
        dry_run = False  # need a connection; orders still skipped inside run_rebalance

    br = os.environ.get("ML_PAPER_BACKEND_ROOT", "").strip()
    if br and not os.environ.get("ML_PAPER_BACKEND_DIR"):
        p = Path(br).expanduser() / "backend"
        if (p / "regression_based_on_technicals.py").is_file():
            os.environ["ML_PAPER_BACKEND_DIR"] = str(p)

    # Ensure mode default is paper for safety module
    os.environ.setdefault("IB_TRADING_MODE", "paper")

    run_rebalance(
        dry_run=dry_run,
        check_connection=bool(args.check_connection),
        top_n=args.top_n,
        deploy_fraction=float(args.deploy_fraction),
        max_order_usd=args.max_order_usd,
        liquidate_non_targets=not bool(args.no_liquidate_non_targets),
        symbol_map=_load_symbol_map(args.symbol_map),
        ib_host=str(args.ib_host),
        ib_port=int(args.ib_port),
        ib_client_id=int(args.ib_client_id),
        ib_account=args.ib_account,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
