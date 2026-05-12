#!/usr/bin/env python3
"""
Paper-trade the ML daily top-N basket (predicted-return weighted) toward target weights
via Interactive Brokers (TWS / IB Gateway, paper account).

Requires:
  - ML_PAPER_BACKEND_DIR  (directory containing sp500_return_model.py)
  - SP500_MODEL_DIR, SP500_PRICE_CACHE_DIR  (or default layout under repo backend/data/...)

IB (when --execute):
  - TWS or IB Gateway running, API enabled, paper login
  - IB_HOST, IB_PORT, IB_CLIENT_ID  (optional IB_ACCOUNT)

Default is --dry-run (no orders).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("paper_rebalance")


def _backend_dir() -> Path:
    raw = (os.environ.get("ML_PAPER_BACKEND_DIR") or "").strip()
    if raw:
        p = Path(raw).expanduser().resolve()
        if (p / "sp500_return_model.py").is_file():
            return p
        raise SystemExit(f"ML_PAPER_BACKEND_DIR={raw!r} must contain sp500_return_model.py")
    here = Path(__file__).resolve()
    for parent in [here.parents[i] for i in range(2, 7)]:
        cand = parent / "backend" / "sp500_return_model.py"
        if cand.is_file():
            return cand.parent.resolve()
    raise SystemExit(
        "Set ML_PAPER_BACKEND_DIR to the directory that contains sp500_return_model.py "
        "(typically .../market_analysis/backend)."
    )


def _ensure_predict_path() -> None:
    bd = _backend_dir()
    if str(bd) not in sys.path:
        sys.path.insert(0, str(bd))


def _load_symbol_map(path: Optional[Path]) -> Dict[str, str]:
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k).strip().upper(): str(v).strip() for k, v in data.items()}
    except Exception as e:
        raise SystemExit(f"invalid --symbol-map JSON: {e}") from e


def cache_stem_to_ib_ticker(stem: str, symbol_map: Dict[str, str]) -> str:
    s = (stem or "").strip().upper()
    if s in symbol_map:
        return symbol_map[s]
    if "-" in s:
        return s.replace("-", " ")
    return s


def _predict_with_prices_refreshed(cadence: str, top_n: int, weighting: str) -> List[Dict[str, Any]]:
    _ensure_predict_path()
    from sp500_return_model import load_prices, predict_top_n  # noqa: WPS433

    # Force refresh path inside predict by passing freshly loaded dict
    from sp500_return_model import PRICE_CACHE_DIR  # noqa: WPS433

    symbols = [p.stem for p in PRICE_CACHE_DIR.glob("*.parquet")]
    if not symbols:
        raise SystemExit("no symbols in SP500_PRICE_CACHE_DIR; populate parquet caches first")
    prices = load_prices(list(set(symbols + ["SPY"])), years=10.0, refresh=True)
    return predict_top_n(cadence, top_n, weighting=weighting, prices=prices)


def _predict_top_n_cached(cadence: str, top_n: int, weighting: str) -> List[Dict[str, Any]]:
    _ensure_predict_path()
    from sp500_return_model import predict_top_n  # noqa: WPS433

    return predict_top_n(cadence, top_n, weighting=weighting)


def _connect_ib(host: str, port: int, client_id: int, account: Optional[str]):
    try:
        from ib_insync import IB  # type: ignore
    except ImportError as e:
        raise SystemExit("install ib-insync: pip install ib-insync") from e

    ib = IB()
    kw: Dict[str, Any] = {"host": host, "port": port, "clientId": client_id, "readonly": False}
    if account:
        kw["account"] = account
    ib.connect(**kw)
    return ib


def _net_liquidation_usd(ib, account: Optional[str]) -> float:
    best: Optional[float] = None
    for row in ib.accountSummary():
        if row.tag != "NetLiquidation":
            continue
        if row.currency not in ("USD", "BASE"):
            continue
        if account and row.account != account:
            continue
        try:
            v = float(row.value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0:
            best = v if best is None else max(best, v)
    if best is None:
        raise RuntimeError("could not read NetLiquidation from IB accountSummary()")
    return best


def _current_stock_positions(ib, account: Optional[str]) -> Dict[str, float]:
    from ib_insync import Stock  # type: ignore

    out: Dict[str, float] = {}
    for p in ib.positions():
        if account and p.account != account:
            continue
        c = p.contract
        if not isinstance(c, Stock):
            continue
        if (c.currency or "").upper() != "USD":
            continue
        sym = (c.symbol or "").strip().upper()
        if not sym:
            continue
        out[sym] = out.get(sym, 0.0) + float(p.position)
    return out


def _ib_ticker_to_cache_stem(ib_sym: str) -> str:
    """Best-effort inverse for position keys (IB 'BRK B' -> cache 'BRK-B')."""
    s = (ib_sym or "").strip().upper()
    if " " in s:
        return s.replace(" ", "-")
    return s


def _last_close_usd(ib, contract) -> Optional[float]:
    from ib_insync import util  # type: ignore

    try:
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="10 D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        if bars:
            return float(bars[-1].close)
    except Exception as e:
        log.warning("historical price failed for %s: %s", contract.symbol, e)
    try:
        t = ib.reqMktData(contract, "", True, False)
        ib.sleep(2.0)
        px = t.last or t.close
        ib.cancelMktData(contract)
        if px and math.isfinite(float(px)) and float(px) > 0:
            return float(px)
    except Exception as e:
        log.warning("mkt data snapshot failed for %s: %s", getattr(contract, "symbol", "?"), e)
    return None


def _qualify_stock(ib, local_symbol: str):
    from ib_insync import Stock  # type: ignore

    c = Stock(local_symbol, "SMART", "USD")
    ib.qualifyContracts(c)
    return c


def run_rebalance(
    *,
    dry_run: bool,
    top_n: int,
    deploy_fraction: float,
    max_order_usd: Optional[float],
    refresh_prices: bool,
    symbol_map: Dict[str, str],
    ib_host: str,
    ib_port: int,
    ib_client_id: int,
    ib_account: Optional[str],
) -> None:
    if refresh_prices:
        targets = _predict_with_prices_refreshed("daily", top_n, "score_weighted")
    else:
        targets = _predict_top_n_cached("daily", top_n, "score_weighted")

    if not targets:
        raise SystemExit("predict_top_n returned no targets")

    log.info("targets (%d):", len(targets))
    for r in targets[:15]:
        log.info("  %s  pred=%.6f  w=%.4f", r.get("symbol"), float(r.get("predicted_return", 0)), float(r.get("weight", 0)))
    if len(targets) > 15:
        log.info("  ...")

    if dry_run:
        log.info("dry-run: not connecting to IB")
        return

    ib = _connect_ib(ib_host, ib_port, ib_client_id, ib_account)
    try:
        nav = _net_liquidation_usd(ib, ib_account)
        budget = nav * float(deploy_fraction)
        log.info("NetLiq≈%.2f USD deploy_fraction=%.3f budget≈%.2f", nav, deploy_fraction, budget)

        pos_by_ib_sym = _current_stock_positions(ib, ib_account)

        # Map cache stem -> IB local symbol and contract
        stem_to_ib: Dict[str, str] = {t["symbol"]: cache_stem_to_ib_ticker(str(t["symbol"]), symbol_map) for t in targets}
        contracts: Dict[str, Any] = {}
        prices: Dict[str, float] = {}
        for stem, ibsym in stem_to_ib.items():
            c = _qualify_stock(ib, ibsym)
            contracts[stem] = c
            px = _last_close_usd(ib, c)
            if px is None or px <= 0:
                raise RuntimeError(f"no price for {stem} (IB local {ibsym!r})")
            prices[stem] = px

        # Target shares (long-only): budget * weight / price
        target_shares: Dict[str, int] = {}
        for row in targets:
            stem = str(row["symbol"])
            w = float(row["weight"])
            px = prices[stem]
            usd = budget * w
            if max_order_usd is not None:
                usd = min(usd, float(max_order_usd))
            sh = int(math.floor(usd / px))
            if sh < 0:
                sh = 0
            target_shares[stem] = sh

        # Current shares mapped by cache stem (approximate inverse IB symbol)
        current_by_stem: Dict[str, int] = {}
        for ibsym, sh in pos_by_ib_sym.items():
            stem = _ib_ticker_to_cache_stem(ibsym)
            current_by_stem[stem] = current_by_stem.get(stem, 0) + int(sh)

        from ib_insync import MarketOrder  # type: ignore

        for row in targets:
            stem = str(row["symbol"])
            cur = int(current_by_stem.get(stem, 0))
            tgt = int(target_shares.get(stem, 0))
            delta = tgt - cur
            if delta == 0:
                log.info("%s: already at %d shares", stem, tgt)
                continue
            c = contracts[stem]
            action = "BUY" if delta > 0 else "SELL"
            qty = abs(delta)
            log.info("%s %s %d (cur=%d tgt=%d px=%.2f)", stem, action, qty, cur, tgt, prices[stem])
            ib.placeOrder(c, MarketOrder(action, qty))

        ib.sleep(1.0)
    finally:
        ib.disconnect()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        from dotenv import load_dotenv  # noqa: WPS433

        load_dotenv()
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="IB paper rebalance for ML daily score-weighted top-N")
    ap.add_argument("--dry-run", action="store_true", help="only print targets; do not connect to IB")
    ap.add_argument(
        "--execute",
        action="store_true",
        help="connect to IB and place orders (paper account). If omitted, behaves as dry-run.",
    )
    ap.add_argument("--top-n", type=int, default=50)
    ap.add_argument("--deploy-fraction", type=float, default=0.95, help="fraction of NetLiquidation to allocate to targets")
    ap.add_argument("--max-order-usd", type=float, default=None, help="cap dollar notional per name (optional)")
    ap.add_argument("--refresh-prices", action="store_true", help="force yfinance refresh before predict (slow)")
    ap.add_argument("--symbol-map", type=Path, default=None, help="JSON map cache-stem -> IB local symbol, e.g. {\"BRK-B\":\"BRK B\"}")
    ap.add_argument("--ib-host", default=os.environ.get("IB_HOST", "127.0.0.1"))
    ap.add_argument("--ib-port", type=int, default=int(os.environ.get("IB_PORT", "7497")))
    ap.add_argument("--ib-client-id", type=int, default=int(os.environ.get("IB_CLIENT_ID", "51")))
    ap.add_argument("--ib-account", default=os.environ.get("IB_ACCOUNT") or None)
    args = ap.parse_args()
    if args.execute and args.dry_run:
        ap.error("choose at most one of --execute and --dry-run")
    dry_run = not bool(args.execute)

    smap = _load_symbol_map(args.symbol_map)

    # If user passed --backend-root, expose as env for _backend_dir
    # (optional convenience)
    br = os.environ.get("ML_PAPER_BACKEND_ROOT", "").strip()
    if br and not os.environ.get("ML_PAPER_BACKEND_DIR"):
        p = Path(br).expanduser() / "backend"
        if (p / "sp500_return_model.py").is_file():
            os.environ["ML_PAPER_BACKEND_DIR"] = str(p)

    run_rebalance(
        dry_run=dry_run,
        top_n=int(args.top_n),
        deploy_fraction=float(args.deploy_fraction),
        max_order_usd=args.max_order_usd,
        refresh_prices=bool(args.refresh_prices),
        symbol_map=smap,
        ib_host=str(args.ib_host),
        ib_port=int(args.ib_port),
        ib_client_id=int(args.ib_client_id),
        ib_account=args.ib_account,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
