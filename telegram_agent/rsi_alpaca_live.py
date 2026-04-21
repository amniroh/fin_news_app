"""
Run the same RSI mean-reversion *rules* as ``rsi_portfolio_simulator`` against Alpaca
(paper or live): priority-0 universe, top-K hourly signals, fixed notional per leg,
time-based exit after ``horizon_bars`` hours.

Uses Alpaca's REST API via ``httpx`` (no extra SDK). Tracks **lots** in a local JSON file
because Alpaca consolidates positions per symbol, so time-staggered exits must be managed
client-side.

Requires: APCA_API_KEY_ID, APCA_API_SECRET_KEY (or ALPACA_* aliases), universe with P0
symbols, and SYMBOL_UNIVERSE_ENABLED + SYMBOL_UNIVERSE_PATH as for the offline simulator.

Example::

    python -m telegram_agent.rsi_alpaca_live --once --dry-run
    python -m telegram_agent.rsi_alpaca_live --once
    python -m telegram_agent.rsi_alpaca_live --loop
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from telegram_agent.competitive_bots import _mean_reversion_score
from telegram_agent.config import DATA_DIR, load_config
from telegram_agent.symbol_universe import symbols_with_exact_priority

logger = logging.getLogger(__name__)

TRADING_PAPER = "https://paper-api.alpaca.markets"
TRADING_LIVE = "https://api.alpaca.markets"
DATA_V2 = "https://data.alpaca.markets"


def _retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


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


class AlpacaRest:
    """Minimal Alpaca Trading API v2 + Data API v2 client."""

    def __init__(self, key_id: str, secret: str, *, paper: bool = True, timeout: float = 60.0):
        self._trading = TRADING_PAPER if paper else TRADING_LIVE
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret,
        }
        self._timeout = timeout

    def get_account(self) -> Dict[str, Any]:
        url = f"{self._trading}/v2/account"
        with httpx.Client(timeout=self._timeout) as c:
            r = c.get(url, headers=self._headers)
            r.raise_for_status()
            return r.json()

    def submit_market_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: Optional[float] = None,
        notional_usd: Optional[float] = None,
    ) -> Dict[str, Any]:
        url = f"{self._trading}/v2/orders"
        body: Dict[str, Any] = {
            "symbol": symbol,
            "side": side.lower(),
            "type": "market",
            "time_in_force": "day",
        }
        if notional_usd is not None and qty is not None:
            raise ValueError("Provide either qty or notional_usd, not both")
        if notional_usd is not None:
            body["notional"] = f"{float(notional_usd):.2f}"
        elif qty is not None:
            body["qty"] = f"{float(qty):.9f}".rstrip("0").rstrip(".")
        else:
            raise ValueError("Need qty or notional_usd")
        with httpx.Client(timeout=self._timeout) as c:
            r = c.post(url, headers=self._headers, json=body)
            if r.status_code >= 400:
                logger.warning("Order rejected %s: %s", r.status_code, r.text)
            r.raise_for_status()
            return r.json()

    def get_stock_bars(
        self,
        symbols: Sequence[str],
        *,
        start: datetime,
        end: datetime,
        timeframe: str = "1Hour",
        feed: str = "iex",
        adjustment: str = "split",
        page_delay_sec: float = 0.0,
        max_retries: int = 12,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Return ``symbol -> list of bar dicts`` (each bar has t, o, h, l, c, v).

        Retries 429/502/503 with exponential backoff (honors ``Retry-After`` when present).
        Optional ``page_delay_sec`` sleep between successful paginated page fetches (default 0).
        """
        if not symbols:
            return {}
        out: Dict[str, List[Dict[str, Any]]] = {s: [] for s in symbols}
        # Alpaca returns up to 10k points per request; paginate with next_page_token.
        params_base: Dict[str, Any] = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": _iso_z(start),
            "end": _iso_z(end),
            "limit": 10000,
            "adjustment": adjustment,
            "feed": feed,
        }
        url = f"{DATA_V2}/v2/stocks/bars"
        next_token: Optional[str] = None
        with httpx.Client(timeout=self._timeout) as c:
            while True:
                params = dict(params_base)
                if next_token:
                    params["page_token"] = next_token
                attempt = 0
                while True:
                    r = c.get(url, headers=self._headers, params=params)
                    if r.status_code in (429, 502, 503):
                        attempt += 1
                        if attempt > max_retries:
                            logger.warning(
                                "Bars request failed %s after %s retries: %s",
                                r.status_code,
                                max_retries,
                                r.text[:500],
                            )
                            r.raise_for_status()
                        ra = _retry_after_seconds(r)
                        if ra is not None and ra > 0:
                            sleep_s = min(120.0, ra + random.uniform(0, 0.35))
                        else:
                            sleep_s = min(
                                120.0,
                                0.6 * (2 ** (attempt - 1)) + random.uniform(0, 0.5),
                            )
                        logger.warning(
                            "Bars request %s; backing off %.1fs (attempt %s/%s)",
                            r.status_code,
                            sleep_s,
                            attempt,
                            max_retries,
                        )
                        time.sleep(sleep_s)
                        continue
                    if r.status_code >= 400:
                        logger.warning("Bars request failed %s: %s", r.status_code, r.text[:500])
                    r.raise_for_status()
                    break
                data = r.json()
                bars_map = data.get("bars") or {}
                for sym, bars in bars_map.items():
                    if sym in out:
                        out[sym].extend(bars)
                next_token = data.get("next_page_token")
                if not next_token:
                    break
                if page_delay_sec > 0:
                    time.sleep(float(page_delay_sec))
        for sym in list(out.keys()):
            out[sym].sort(key=lambda b: b.get("t") or "")
        return out


def _iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    s = dt.isoformat().replace("+00:00", "Z")
    return s


def _chunks(xs: Sequence[str], n: int) -> List[List[str]]:
    return [list(xs[i : i + n]) for i in range(0, len(xs), n)]


def _rank_rsi_top_k(
    symbols: Sequence[str],
    closes_by_symbol: Dict[str, Sequence[float]],
    min_bars: int,
    k: int,
) -> List[Tuple[str, float]]:
    scored: List[Tuple[str, float]] = []
    for sym in symbols:
        closes = list(closes_by_symbol.get(sym) or [])
        if len(closes) < min_bars:
            continue
        raw = _mean_reversion_score(closes)
        if raw is None or not math.isfinite(raw):
            continue
        scored.append((sym, float(raw)))
    scored.sort(key=lambda x: -x[1])
    return scored[: max(1, k)]


def _float_bp(acct: Dict[str, Any]) -> float:
    raw = acct.get("buying_power") or acct.get("cash") or "0"
    try:
        return float(raw)
    except Exception:
        return 0.0


def _fetch_hourly_closes(
    client: AlpacaRest,
    symbols: Sequence[str],
    *,
    lookback_days: int,
    feed: str,
    chunk: int = 40,
) -> Dict[str, List[float]]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    closes_out: Dict[str, List[float]] = {}
    for group in _chunks(list(symbols), chunk):
        bars_map = client.get_stock_bars(group, start=start, end=end, feed=feed)
        for sym in group:
            bars = bars_map.get(sym) or []
            closes = [float(b["c"]) for b in bars if b.get("c") is not None]
            closes_out[sym] = closes
    return closes_out


@dataclass
class Lot:
    id: str
    symbol: str
    qty: float
    entry_utc: str
    exit_after_utc: str
    cost_basis_usd: float
    buy_order_id: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "qty": self.qty,
            "entry_utc": self.entry_utc,
            "exit_after_utc": self.exit_after_utc,
            "cost_basis_usd": round(self.cost_basis_usd, 2),
            "buy_order_id": self.buy_order_id,
        }

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "Lot":
        return Lot(
            id=str(d["id"]),
            symbol=str(d["symbol"]).upper(),
            qty=float(d["qty"]),
            entry_utc=str(d["entry_utc"]),
            exit_after_utc=str(d["exit_after_utc"]),
            cost_basis_usd=float(d["cost_basis_usd"]),
            buy_order_id=(str(d["buy_order_id"]) if d.get("buy_order_id") else None),
        )


def _parse_iso(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def load_state(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "last_entry_hour_utc": None, "lots": []}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {"version": 1, "last_entry_hour_utc": None, "lots": []}
    raw.setdefault("version", 1)
    raw.setdefault("last_entry_hour_utc", None)
    raw.setdefault("lots", [])
    return raw


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_once(
    cfg: dict,
    *,
    client: AlpacaRest,
    dry_run: bool,
    per_leg_usd: float,
    purchases_per_hour: int,
    horizon_bars: int,
    min_bars: int,
    force_entries: bool,
    lookback_days: int,
    feed: str,
) -> Dict[str, Any]:
    p0 = symbols_with_exact_priority(cfg, 0)
    if not p0:
        raise RuntimeError(
            "No priority-0 symbols. Enable SYMBOL_UNIVERSE_PATH with priorities or set priorities in JSON."
        )

    state_path = Path(cfg.get("rsi_alpaca_state_path") or (DATA_DIR / "rsi_alpaca_state.json"))
    state = load_state(state_path)
    lots: List[Lot] = [Lot.from_json(x) for x in (state.get("lots") or []) if isinstance(x, dict)]

    now = datetime.now(timezone.utc)
    decision_hour = now.replace(minute=0, second=0, microsecond=0)
    decision_hour_iso = decision_hour.isoformat()

    acct = client.get_account() if not dry_run else {}
    buying_power = _float_bp(acct) if acct else 0.0

    closes_by = _fetch_hourly_closes(client, p0, lookback_days=lookback_days, feed=feed)
    ranked = _rank_rsi_top_k(p0, closes_by, min_bars, purchases_per_hour)

    last_h = state.get("last_entry_hour_utc")
    entry_skipped_duplicate = bool(not force_entries and last_h == decision_hour_iso)

    log: Dict[str, Any] = {
        "decision_hour_utc": decision_hour_iso,
        "ranked_top": [{"symbol": s, "score": round(sc, 6)} for s, sc in ranked],
        "exits": [],
        "buys": [],
        "skipped_entries_reason": None,
        "entry_skipped_duplicate": entry_skipped_duplicate,
    }

    # 1) Exits: time-based (persist removals whenever not dry_run)
    still: List[Lot] = []
    for lot in lots:
        if _parse_iso(lot.exit_after_utc) <= now:
            if dry_run:
                log["exits"].append(
                    {"symbol": lot.symbol, "qty": lot.qty, "dry_run": True, "lot_id": lot.id}
                )
                continue
            try:
                od = client.submit_market_order(symbol=lot.symbol, side="sell", qty=lot.qty)
                log["exits"].append(
                    {
                        "symbol": lot.symbol,
                        "qty": lot.qty,
                        "order_id": od.get("id"),
                        "status": od.get("status"),
                        "lot_id": lot.id,
                    }
                )
            except Exception as e:
                logger.exception("Sell failed for %s: %s", lot.symbol, e)
                still.append(lot)
        else:
            still.append(lot)
    lots = still

    # 2) Entries: at most once per UTC clock hour unless --force-entries
    if entry_skipped_duplicate:
        log["skipped_entries_reason"] = "already_ran_entries_this_hour"
    else:
        k = max(1, int(purchases_per_hour))
        n_bought = 0
        cash_budget = buying_power if acct else float("inf")
        for sym, _sc in ranked:
            if n_bought >= k:
                break
            if cash_budget < per_leg_usd:
                log["skipped_entries_reason"] = log.get("skipped_entries_reason") or "insufficient_buying_power"
                break
            if dry_run:
                log["buys"].append({"symbol": sym, "notional_usd": per_leg_usd, "dry_run": True})
                n_bought += 1
                cash_budget -= per_leg_usd
                continue
            try:
                od = client.submit_market_order(
                    symbol=sym, side="buy", notional_usd=per_leg_usd
                )
                filled_qty_raw = od.get("filled_qty") or od.get("qty")
                try:
                    filled_qty = float(filled_qty_raw) if filled_qty_raw is not None else 0.0
                except Exception:
                    filled_qty = 0.0
                if filled_qty <= 0:
                    cl = closes_by.get(sym) or []
                    if cl:
                        lp = float(cl[-1])
                        if lp > 0:
                            filled_qty = per_leg_usd / lp
                exit_after = (decision_hour + timedelta(hours=horizon_bars)).isoformat()
                lid = str(uuid.uuid4())
                lots.append(
                    Lot(
                        id=lid,
                        symbol=sym,
                        qty=filled_qty,
                        entry_utc=decision_hour_iso,
                        exit_after_utc=exit_after,
                        cost_basis_usd=per_leg_usd,
                        buy_order_id=str(od.get("id") or ""),
                    )
                )
                log["buys"].append(
                    {
                        "symbol": sym,
                        "notional_usd": per_leg_usd,
                        "order_id": od.get("id"),
                        "status": od.get("status"),
                        "filled_qty": filled_qty,
                        "lot_id": lid,
                    }
                )
                n_bought += 1
                cash_budget -= per_leg_usd
            except Exception as e:
                logger.warning("Buy failed for %s: %s", sym, e)

        if log.get("skipped_entries_reason") is None and n_bought == 0 and ranked and not dry_run:
            log["skipped_entries_reason"] = "no_fills_or_all_failed"

    state["lots"] = [x.to_json() for x in lots]
    if not dry_run and not entry_skipped_duplicate:
        state["last_entry_hour_utc"] = decision_hour_iso

    if not dry_run:
        save_state(state_path, state)

    log["state_path"] = str(state_path)
    log["buying_power_usd"] = round(buying_power, 2) if acct else None
    log["open_lots"] = len(lots)
    log["dry_run"] = dry_run
    return log


def _sleep_until_next_hour_utc() -> None:
    now = datetime.now(timezone.utc)
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    secs = max(1.0, (next_hour - now).total_seconds())
    logger.info("Sleeping %.0f s until %s UTC", secs, next_hour.isoformat())
    time.sleep(secs)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="RSI P0 hourly strategy on Alpaca (paper/live)")
    p.add_argument("--once", action="store_true", help="Run a single decision cycle and exit")
    p.add_argument(
        "--loop",
        action="store_true",
        help="Sleep until next UTC hour boundary, run, repeat (use with systemd/cron instead for production)",
    )
    p.add_argument("--dry-run", action="store_true", help="Rank and log actions without orders")
    p.add_argument("--per-leg", type=float, default=1_000.0, help="USD notional per new lot")
    p.add_argument("--purchases-per-hour", type=int, default=3, help="Max new buys per hour")
    p.add_argument("--horizon-bars", type=int, default=40, help="Hold each lot for this many hours")
    p.add_argument("--min-bars", type=int, default=25, help="Min hourly closes for RSI score")
    p.add_argument(
        "--lookback-days",
        type=int,
        default=14,
        help="How far back to request hourly bars (need >= min-bars)",
    )
    p.add_argument(
        "--force-entries",
        action="store_true",
        help="Allow another entry batch in the same UTC hour (not usually needed)",
    )
    p.add_argument(
        "--status",
        action="store_true",
        help="Print Alpaca account JSON and open lots from state file, then exit",
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

    cfg = load_config()
    key = (cfg.get("alpaca_api_key_id") or "").strip()
    secret = (cfg.get("alpaca_api_secret_key") or "").strip()
    paper = bool(cfg.get("alpaca_paper", True))
    feed = (cfg.get("alpaca_data_feed") or "iex").strip().lower()

    if args.status:
        if not key or not secret:
            raise SystemExit("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY (or ALPACA_* aliases).")
        c = AlpacaRest(key, secret, paper=paper)
        acct = c.get_account()
        print(json.dumps({"account": acct}, indent=2, default=str))
        sp = Path(cfg.get("rsi_alpaca_state_path") or (DATA_DIR / "rsi_alpaca_state.json"))
        st = load_state(sp)
        print(json.dumps({"state_file": str(sp), "state": st}, indent=2, default=str))
        return

    if not key or not secret:
        raise SystemExit("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env (see .env.example).")

    client = AlpacaRest(key, secret, paper=paper)

    if args.loop and not args.once:
        logger.info("Loop mode: paper=%s feed=%s", paper, feed)
        while True:
            _sleep_until_next_hour_utc()
            out = run_once(
                cfg,
                client=client,
                dry_run=bool(args.dry_run),
                per_leg_usd=float(args.per_leg),
                purchases_per_hour=int(args.purchases_per_hour),
                horizon_bars=int(args.horizon_bars),
                min_bars=int(args.min_bars),
                force_entries=bool(args.force_entries),
                lookback_days=int(args.lookback_days),
                feed=feed,
            )
            print(json.dumps(out, indent=2, default=str))
    else:
        out = run_once(
            cfg,
            client=client,
            dry_run=bool(args.dry_run),
            per_leg_usd=float(args.per_leg),
            purchases_per_hour=int(args.purchases_per_hour),
            horizon_bars=int(args.horizon_bars),
            min_bars=int(args.min_bars),
            force_entries=bool(args.force_entries),
            lookback_days=int(args.lookback_days),
            feed=feed,
        )
        print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
