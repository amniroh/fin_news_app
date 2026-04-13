"""
Hourly RSI mean-reversion paper simulator: P0 universe, up to **3 new purchases per hour**
(top signals), **$1k per purchase**, independent lots held ``horizon_bars`` then sold. Total
open lots are **unbounded** (limited only by cash). Records suggestions and readable
buy/sell lines + realized P&L on sells.

Does not use the competitive backtest JSON; replays rules from ``competitive_bots._mean_reversion_score``.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telegram_agent.agent_db import connect, get_full_adj_close_series_asc, init_db
from telegram_agent.competitive_bots import _mean_reversion_score
from telegram_agent.config import DATA_DIR, load_config
from telegram_agent.symbol_universe import normalize_symbol, symbols_with_exact_priority

logger = logging.getLogger(__name__)


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


def _price_at_or_before(series: Sequence[Tuple[datetime, float]], t: datetime) -> Optional[float]:
    last: Optional[float] = None
    for ts, px in series:
        if ts > t:
            break
        last = float(px)
    return last


@dataclass
class OpenPosition:
    symbol: str
    entry_bar_index: int
    entry_time: datetime
    shares: float
    cost_basis_usd: float


def _m2m_open_lots(
    lots: Sequence[OpenPosition], cache: Dict[str, List[Tuple[datetime, float]]], t: datetime
) -> float:
    m = 0.0
    for pos in lots:
        px = _price_at_or_before(cache.get(pos.symbol) or [], t)
        if px and px > 0:
            m += pos.shares * px
    return m


@dataclass
class TransactionRecord:
    kind: str  # "buy" | "sell"
    symbol: str
    ts_utc: str
    notional_usd: float
    shares: float
    price: float
    line: str
    realized_pnl_usd: Optional[float] = None


@dataclass
class SuggestionSnapshot:
    bar_index: int
    ts_utc: str
    top_picks: List[Dict[str, Any]] = field(default_factory=list)


def _advance_closes_to_t(
    cache: Dict[str, List[Tuple[datetime, float]]],
    symbols: Sequence[str],
    ptr: Dict[str, int],
    deques: Dict[str, deque],
    t_entry: datetime,
) -> None:
    """Advance per-symbol pointers so deques contain closes through ``t_entry`` (inclusive of last bar ≤ t)."""
    for sym in symbols:
        ser = cache.get(sym) or []
        p = ptr[sym]
        while p < len(ser) and ser[p][0] <= t_entry:
            deques[sym].append(ser[p][1])
            p += 1
        ptr[sym] = p


def _rank_rsi_top_k_from_deques(
    symbols: Sequence[str],
    deques: Dict[str, deque],
    min_bars: int,
    k: int,
) -> List[Tuple[str, float]]:
    scored: List[Tuple[str, float]] = []
    for sym in symbols:
        closes = list(deques[sym])
        if len(closes) < min_bars:
            continue
        raw = _mean_reversion_score(closes)
        if raw is None or not math.isfinite(raw):
            continue
        scored.append((sym, float(raw)))
    scored.sort(key=lambda x: -x[1])
    return scored[:k]


def _parse_competitive_backtest_symbols(cfg: dict) -> List[str]:
    """
    Parse COMPETITIVE_BACKTEST_SYMBOLS-style list (comma-separated tickers) from config.
    """
    raw = (cfg.get("competitive_backtest_symbols") or "").strip()
    if not raw:
        return []
    syms = [normalize_symbol(x) for x in raw.split(",") if x.strip()]
    return sorted({s for s in syms if s})


def run_rsi_p0_hourly_simulation(
    cfg: dict,
    *,
    start: datetime,
    end: Optional[datetime],
    initial_cash_usd: float,
    per_leg_usd: float,
    purchases_per_hour: int,
    horizon_bars: int,
    min_bars: int,
    symbols: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """
    Walk forward on the reference hourly timeline. Each hour: take top ``purchases_per_hour``
    RSI signals and open that many **separate $per_leg_usd lots** (if cash allows). Lots stack
    across hours; there is **no** cap on concurrent holdings. Each lot exits after ``horizon_bars``
    on the reference timeline.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    if end is not None:
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        end = end.astimezone(timezone.utc)

    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)

    if symbols is None:
        symbols = symbols_with_exact_priority(cfg, 0)
        if not symbols:
            con.close()
            raise RuntimeError(
                "No priority-0 symbols (need symbol universe JSON with priority: 0). "
                "Check SYMBOL_UNIVERSE_PATH and SYMBOL_UNIVERSE_ENABLED."
            )
    symbols = [normalize_symbol(s) for s in symbols if str(s).strip()]
    symbols = sorted(set([s for s in symbols if s]))
    if not symbols:
        con.close()
        raise RuntimeError("Empty symbol list for simulation.")

    cache: Dict[str, List[Tuple[datetime, float]]] = {}
    for sym in symbols:
        ser = get_full_adj_close_series_asc(con, sym, "1h")
        if ser:
            cache[sym] = ser

    con.close()

    if not cache:
        raise RuntimeError("No hourly price series found for any requested symbol (prices_hourly).")

    # Reference timeline: densest symbol after start
    ref_sym = max(cache.keys(), key=lambda s: len([x for x in cache[s] if x[0] >= start]))
    ref_series = [(t, p) for t, p in cache[ref_sym] if t >= start]
    if end is not None:
        ref_series = [(t, p) for t, p in ref_series if t <= end]
    if len(ref_series) < min_bars + horizon_bars + 2:
        raise RuntimeError(
            f"Insufficient hourly bars after {start.date()} for reference {ref_sym}: len={len(ref_series)}"
        )

    cash = float(initial_cash_usd)
    lots: List[OpenPosition] = []
    tx: List[TransactionRecord] = []
    suggestions: List[SuggestionSnapshot] = []
    equity_curve: List[Dict[str, Any]] = []

    times = [t for t, _ in ref_series]
    n = len(times)
    # Last bar index where we may still *open* a new lot and exit before series end.
    last_entry_bar = n - 1 - horizon_bars

    k = max(1, int(purchases_per_hour))
    ptr: Dict[str, int] = {sym: 0 for sym in symbols}
    deques: Dict[str, deque] = {sym: deque(maxlen=80) for sym in symbols}

    for i in range(min_bars, n):
        t = times[i]
        _advance_closes_to_t(cache, symbols, ptr, deques, t)
        ranked = _rank_rsi_top_k_from_deques(symbols, deques, min_bars, k)
        top_payload = [{"symbol": s, "score": round(sc, 6)} for s, sc in ranked]
        suggestions.append(
            SuggestionSnapshot(bar_index=i, ts_utc=t.isoformat(), top_picks=top_payload)
        )

        # 1) Mature exits: each lot independently after horizon_bars
        still: List[OpenPosition] = []
        for pos in lots:
            if i - pos.entry_bar_index >= horizon_bars:
                sym = pos.symbol
                px = _price_at_or_before(cache.get(sym) or [], t)
                if px is None or px <= 0:
                    logger.warning("No price for %s at %s — holding lot", sym, t)
                    still.append(pos)
                    continue
                proceeds = pos.shares * px
                realized = proceeds - pos.cost_basis_usd
                cash += proceeds
                line = (
                    f"sold ${proceeds:,.2f} worth of {sym} at {t.isoformat()} "
                    f"(realized P&L ${realized:+,.2f})"
                )
                tx.append(
                    TransactionRecord(
                        kind="sell",
                        symbol=sym,
                        ts_utc=t.isoformat(),
                        notional_usd=round(proceeds, 2),
                        shares=pos.shares,
                        price=px,
                        line=line,
                        realized_pnl_usd=round(realized, 2),
                    )
                )
            else:
                still.append(pos)
        lots = still

        # 2) Buys: up to `k` purchases this hour (top signals), if cash and data allow
        if i <= last_entry_bar:
            purchases_this_bar = 0
            for sym, sc in ranked:
                if purchases_this_bar >= k:
                    break
                if cash < per_leg_usd:
                    break
                px = _price_at_or_before(cache.get(sym) or [], t)
                if px is None or px <= 0:
                    continue
                spend = float(per_leg_usd)
                shares = spend / px
                cash -= spend
                lots.append(
                    OpenPosition(
                        symbol=sym,
                        entry_bar_index=i,
                        entry_time=t,
                        shares=shares,
                        cost_basis_usd=spend,
                    )
                )
                purchases_this_bar += 1
                line = f"bought ${spend:,.2f} worth of {sym} at {t.isoformat()}"
                tx.append(
                    TransactionRecord(
                        kind="buy",
                        symbol=sym,
                        ts_utc=t.isoformat(),
                        notional_usd=round(spend, 2),
                        shares=shares,
                        price=px,
                        line=line,
                        realized_pnl_usd=None,
                    )
                )

        eq = cash + _m2m_open_lots(lots, cache, t)
        equity_curve.append(
            {
                "ts_utc": t.isoformat(),
                "bar_index": i,
                "equity_usd": round(eq, 2),
                "cash_usd": round(cash, 2),
                "open_lots": len(lots),
            }
        )

    # Mark-to-market at last bar
    last_t = times[-1]
    m2m = 0.0
    for pos in lots:
        px = _price_at_or_before(cache.get(pos.symbol) or [], last_t)
        if px and px > 0:
            m2m += pos.shares * px

    total_realized = sum(
        (r.realized_pnl_usd or 0.0) for r in tx if r.kind == "sell" and r.realized_pnl_usd is not None
    )
    ending_value = cash + m2m
    pnl_vs_seed = ending_value - initial_cash_usd

    open_pos_out: List[Dict[str, Any]] = []
    for p in lots:
        d = asdict(p)
        d["entry_time"] = p.entry_time.isoformat()
        open_pos_out.append(d)

    return {
        "parameters": {
            "start_utc": start.isoformat(),
            "end_utc": end.isoformat() if end else None,
            "initial_cash_usd": initial_cash_usd,
            "per_leg_usd": per_leg_usd,
            "purchases_per_hour": k,
            "horizon_bars": horizon_bars,
            "min_bars": min_bars,
            "reference_symbol": ref_sym,
            "symbol_count": len(symbols),
        },
        "summary": {
            "ending_cash_usd": round(cash, 2),
            "open_lots_count": len(lots),
            "open_positions_m2m_usd": round(m2m, 2),
            "ending_total_value_usd": round(ending_value, 2),
            "realized_pnl_from_sells_usd": round(total_realized, 2),
            "unrealized_approx_usd": round(pnl_vs_seed - total_realized, 2),
            "total_pnl_vs_seed_usd": round(pnl_vs_seed, 2),
            "open_positions": open_pos_out,
        },
        "suggestions": [asdict(s) for s in suggestions],
        "transactions": [asdict(r) for r in tx],
        "equity_curve": equity_curve,
    }


def _write_equity_chart_png(out_path: Path, equity_curve: List[Dict[str, Any]], initial_cash: float) -> bool:
    if not equity_curve:
        return False
    try:
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib not installed; skip equity chart PNG")
        return False
    xs = [datetime.fromisoformat(x["ts_utc"].replace("Z", "+00:00")) for x in equity_curve]
    ys = [float(x["equity_usd"]) for x in equity_curve]
    fig, ax = plt.subplots(figsize=(11, 4.5), dpi=120)
    ax.plot(xs, ys, color="#1f77b4", linewidth=1.0, label="Total equity")
    ax.axhline(y=initial_cash, color="#888", linestyle="--", linewidth=0.8, label="Initial cash")
    ax.set_title("RSI P0 hourly simulator — portfolio value (cash + mark-to-market)")
    ax.set_xlabel("UTC time")
    ax.set_ylabel("USD")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return True


def _write_outputs(
    out_dir: Path,
    payload: Dict[str, Any],
    *,
    write_chart: bool = True,
) -> Tuple[Path, Path, Optional[Path], Optional[Path]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jpath = out_dir / "rsi_p0_hourly_simulation.json"
    tpath = out_dir / "rsi_p0_hourly_simulation_transactions.txt"
    jpath.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    lines = []
    lines.append("# RSI mean-reversion hourly simulator (P0, ≤3 buys/hour, $1k/lot, stacked lots, time exit)")
    lines.append(json.dumps(payload.get("parameters"), indent=2))
    lines.append("")
    lines.append("# Summary")
    lines.append(json.dumps(payload.get("summary"), indent=2))
    lines.append("")
    lines.append("# Transactions (chronological)")
    for r in payload.get("transactions") or []:
        lines.append(r.get("line", ""))
    lines.append("")
    lines.append(f"# Transaction count: {len(payload.get('transactions') or [])}")
    lines.append(f"# Suggestion snapshots: {len(payload.get('suggestions') or [])}")
    ec = payload.get("equity_curve") or []
    if ec:
        lines.append("# Equity curve: rsi_p0_hourly_equity.csv + rsi_p0_hourly_equity.png (and equity_curve in JSON)")
    tpath.write_text("\n".join(lines), encoding="utf-8")

    curve = payload.get("equity_curve") or []
    csv_path: Optional[Path] = None
    png_path: Optional[Path] = None
    if curve:
        csv_path = out_dir / "rsi_p0_hourly_equity.csv"
        with csv_path.open("w", encoding="utf-8") as f:
            f.write("ts_utc,bar_index,equity_usd,cash_usd,open_lots\n")
            for row in curve:
                f.write(
                    f"{row['ts_utc']},{row['bar_index']},{row['equity_usd']},"
                    f"{row['cash_usd']},{row['open_lots']}\n"
                )
        ic = float((payload.get("parameters") or {}).get("initial_cash_usd") or 0)
        png_path = out_dir / "rsi_p0_hourly_equity.png"
        if write_chart:
            if not _write_equity_chart_png(png_path, curve, ic):
                png_path = None
        else:
            png_path = None

    return jpath, tpath, csv_path, png_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="RSI mean-reversion P0 hourly portfolio simulator")
    p.add_argument("--start", type=str, default="2025-03-10", help="UTC date YYYY-MM-DD")
    p.add_argument("--end", type=str, default=None, help="Optional UTC end date (default: last bar in DB)")
    p.add_argument("--capital", type=float, default=50_000.0, help="Starting cash USD")
    p.add_argument("--per-leg", type=float, default=1_000.0, help="Notional per purchase USD")
    p.add_argument(
        "--purchases-per-hour",
        type=int,
        default=3,
        help="Max new purchases each hour (top RSI signals); total open lots uncapped",
    )
    p.add_argument(
        "--horizon-bars",
        type=int,
        default=40,
        help="Hold each position for this many hourly bars (matches competitive 1h backtest default)",
    )
    p.add_argument(
        "--min-bars",
        type=int,
        default=25,
        help="Minimum history bars for scoring (>=20 required by RSI rule)",
    )
    p.add_argument(
        "--out-dir",
        type=str,
        default=None,
        help=f"Output directory (default: {DATA_DIR})",
    )
    p.add_argument(
        "--no-chart",
        action="store_true",
        help="Skip writing PNG chart (CSV + JSON equity_curve still written)",
    )
    p.add_argument(
        "--competitive-backtest-symbols-only",
        action="store_true",
        help="Run on COMPETITIVE_BACKTEST_SYMBOLS only (instead of priority-0 universe)",
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
    start = datetime.combine(date.fromisoformat(args.start), time.min, tzinfo=timezone.utc)
    end: Optional[datetime] = None
    if args.end:
        end = datetime.combine(date.fromisoformat(args.end), time.max, tzinfo=timezone.utc)

    universe_override: Optional[List[str]] = None
    if bool(args.competitive_backtest_symbols_only):
        universe_override = _parse_competitive_backtest_symbols(cfg)
        if not universe_override:
            raise SystemExit(
                "COMPETITIVE_BACKTEST_SYMBOLS is empty. Set it in .env (comma-separated tickers)."
            )

    out = run_rsi_p0_hourly_simulation(
        cfg,
        start=start,
        end=end,
        initial_cash_usd=float(args.capital),
        per_leg_usd=float(args.per_leg),
        purchases_per_hour=int(args.purchases_per_hour),
        horizon_bars=int(args.horizon_bars),
        min_bars=int(args.min_bars),
        symbols=universe_override,
    )
    odir = Path(args.out_dir) if args.out_dir else DATA_DIR
    jp, tp, csvp, pngp = _write_outputs(odir, out, write_chart=not args.no_chart)
    print(json.dumps(out["summary"], indent=2))
    print("")
    print(f"Wrote {jp}")
    print(f"Wrote {tp}")
    if csvp:
        print(f"Wrote {csvp}")
    if pngp:
        print(f"Wrote {pngp}")


if __name__ == "__main__":
    main()
