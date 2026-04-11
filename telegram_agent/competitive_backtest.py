"""
Walk-forward backtests for the three competitive bot scorers on stored OHLCV.

Uses every distinct ``prices.interval`` present in the DB (typically only ``1d`` unless
you have ingested intraday). Cross-section: reference timeline = densest symbol’s bars;
at each evaluation time, rank the universe, equal-weight top-K picks, measure forward
return over a horizon in **bars** (short horizon, cadence-appropriate).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telegram_agent.agent_db import (
    connect,
    count_prices_for_symbol_interval,
    get_full_adj_close_series_asc,
    init_db,
    kv_set,
    list_distinct_price_intervals,
)
from telegram_agent.competitive_bots import (
    COMPETITIVE_BOT_SPECS,
    _Candidate,
    _SCORERS,
    _symbols_for_competition,
)

logger = logging.getLogger(__name__)

COMPETITIVE_BACKTEST_KV_KEY = "competitive_backtest:last_v1"

# Forward horizon in bars (short-term); tuned per cadence when data exists.
HORIZON_BARS_DEFAULT: Dict[str, int] = {
    "1d": 5,
    "1wk": 1,
    "1mo": 1,
    "1h": 40,
    "60m": 40,
    "30m": 78,
    "15m": 104,
    "5m": 156,
    "2m": 260,
    "1m": 390,
    "1Min": 390,
    "60Min": 40,
}


def _horizon_bars(interval: str, cfg: dict) -> int:
    raw = cfg.get("competitive_backtest_horizon_overrides") or ""
    if isinstance(raw, str) and raw.strip():
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and interval in d:
                return int(d[interval])
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return int(HORIZON_BARS_DEFAULT.get(interval, 5))


def _price_at_or_before(
    series: Sequence[Tuple[datetime, float]], t: datetime
) -> Optional[float]:
    last: Optional[float] = None
    for ts, px in series:
        if ts > t:
            break
        last = float(px)
    return last


def _closes_as_of(
    series: Sequence[Tuple[datetime, float]], t_end: datetime
) -> Optional[List[float]]:
    out: List[float] = []
    for ts, px in series:
        if ts > t_end:
            break
        out.append(float(px))
    return out if out else None


def _rank_at_time(
    cache: Dict[str, List[Tuple[datetime, float]]],
    bot_id: str,
    symbols: Sequence[str],
    t_entry: datetime,
    min_bars: int,
) -> List[_Candidate]:
    scorer = _SCORERS.get(bot_id)
    if not scorer:
        return []
    out: List[_Candidate] = []
    for sym in symbols:
        series = cache.get(sym) or []
        closes = _closes_as_of(series, t_entry)
        if not closes or len(closes) < min_bars:
            continue
        raw = scorer(closes)
        if raw is None or not math.isfinite(raw):
            continue
        out.append(_Candidate(symbol=sym, score=float(raw)))
    out.sort(key=lambda x: x.score, reverse=True)
    return out


def _forward_pct(
    cache: Dict[str, List[Tuple[datetime, float]]],
    sym: str,
    t0: datetime,
    t1: datetime,
) -> Optional[float]:
    s = cache.get(sym)
    if not s:
        return None
    p0 = _price_at_or_before(s, t0)
    p1 = _price_at_or_before(s, t1)
    if p0 is None or p1 is None or p0 <= 0:
        return None
    return (p1 / p0 - 1.0) * 100.0


def _walk_forward_one_bot(
    cfg: dict,
    *,
    bot_id: str,
    interval: str,
    cache: Dict[str, List[Tuple[datetime, float]]],
    ref_series: List[Tuple[datetime, float]],
    symbols: Sequence[str],
) -> Dict[str, Any]:
    min_bars = max(25, int(cfg.get("competitive_bots_min_bars", 25)))
    horizon = _horizon_bars(interval, cfg)
    max_picks = max(1, int(cfg.get("competitive_bots_max_picks", 3)))
    max_eval = max(50, int(cfg.get("competitive_backtest_max_eval_points", 2000)))

    if len(ref_series) < min_bars + horizon + 2:
        return {"error": "insufficient_reference_bars", "n_periods": 0}

    start_i = min_bars
    last_i = len(ref_series) - horizon - 1
    n_span = max(0, last_i - start_i + 1)
    stride = max(1, n_span // max_eval) if n_span else 1

    rets: List[float] = []
    for i in range(start_i, last_i + 1, stride):
        t0 = ref_series[i][0]
        t1 = ref_series[i + horizon][0]
        ranked = _rank_at_time(cache, bot_id, symbols, t0, min_bars)
        picks = ranked[:max_picks]
        if not picks:
            continue
        leg: List[float] = []
        for p in picks:
            r = _forward_pct(cache, p.symbol, t0, t1)
            if r is not None:
                leg.append(r)
        if not leg:
            continue
        rets.append(float(sum(leg) / len(leg)))

    if not rets:
        return {
            "n_periods": 0,
            "mean_ret_pct": None,
            "std_ret_pct": None,
            "win_rate": None,
            "sharpe_like": None,
            "note": "no_valid_periods",
        }

    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / max(1, (len(rets) - 1))
    sd = math.sqrt(var) if var > 0 else 0.0
    wins = sum(1 for x in rets if x > 0)
    sharpe_like = (m / sd) if sd > 1e-12 else 0.0
    return {
        "n_periods": len(rets),
        "mean_ret_pct": round(m, 6),
        "std_ret_pct": round(sd, 6),
        "win_rate": round(wins / len(rets), 6),
        "sharpe_like": round(sharpe_like, 6),
        "horizon_bars": horizon,
        "stride": stride,
    }


def run_competitive_backtest_all_intervals(cfg: dict) -> Dict[str, Any]:
    """
    For each distinct price interval in the DB, run walk-forward backtests for all
    three bots. Stores JSON in kv_state under ``competitive_backtest:last_v1``.
    """
    cfg = {**cfg}
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    max_sym = max(5, int(cfg.get("competitive_backtest_max_symbols", 50)))
    max_rows_soft = int(cfg.get("competitive_backtest_max_total_rows", 3_000_000))

    con = connect(db)
    init_db(con)
    intervals = list_distinct_price_intervals(con)
    symbols_u = _symbols_for_competition(cfg)
    if not symbols_u:
        con.close()
        return {
            "ok": False,
            "error": "no_symbols",
            "intervals": intervals,
            "results": {},
        }

    now = datetime.now(timezone.utc)
    out_results: Dict[str, Any] = {"by_interval": {}}
    skipped: List[str] = []

    for interval in intervals:
        horizon = _horizon_bars(interval, cfg)
        min_need = max(25, int(cfg.get("competitive_bots_min_bars", 25))) + horizon + 5

        scored: List[Tuple[int, str]] = []
        for sym in symbols_u:
            n = count_prices_for_symbol_interval(con, sym, interval)
            if n >= min_need:
                scored.append((n, sym))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            skipped.append(f"{interval}:no_symbols_with_enough_bars")
            continue

        take = [s for _, s in scored[:max_sym]]
        ref_sym = take[0]
        est_rows = sum(c for c, s in scored if s in take)
        if est_rows > max_rows_soft:
            take = take[: max(3, max_sym // 4)]
            skipped.append(
                f"{interval}:subsampled_symbols_due_to_row_budget(est={est_rows})"
            )

        cache: Dict[str, List[Tuple[datetime, float]]] = {}
        total_rows = 0
        for sym in take:
            ser = get_full_adj_close_series_asc(con, sym, interval)
            cache[sym] = ser
            total_rows += len(ser)

        ref_series = cache.get(ref_sym) or []
        if len(ref_series) < min_need:
            skipped.append(f"{interval}:weak_reference_series")
            continue

        per_bot: Dict[str, Any] = {}
        for bot_id, desc in COMPETITIVE_BOT_SPECS:
            per_bot[bot_id] = {
                "description": desc,
                **_walk_forward_one_bot(
                    cfg,
                    bot_id=bot_id,
                    interval=interval,
                    cache=cache,
                    ref_series=ref_series,
                    symbols=take,
                ),
            }

        out_results["by_interval"][interval] = {
            "reference_symbol": ref_sym,
            "symbols_used": take,
            "symbol_count": len(take),
            "total_bar_rows_loaded": total_rows,
            "bots": per_bot,
        }

    payload = {
        "ok": True,
        "run_ts_utc": now.isoformat(),
        "intervals_found": intervals,
        "skipped": skipped,
        "results": out_results,
    }
    try:
        kv_set(con, COMPETITIVE_BACKTEST_KV_KEY, json.dumps(payload, default=str))
    except Exception as e:
        logger.warning("Could not save competitive backtest kv: %s", e)
    con.close()
    logger.info(
        "competitive backtest done intervals=%s skipped=%s",
        intervals,
        len(skipped),
    )
    return payload
