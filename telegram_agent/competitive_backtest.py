"""
Walk-forward backtests for the three competitive bot scorers on stored OHLCV.

Uses distinct intervals from ``prices`` plus ``1h``/``1m`` when ``prices_hourly`` /
``prices_minute`` have data (intraday is stored there, not in ``prices``).
Cross-section: reference timeline = densest symbol’s bars;
at each evaluation time, rank the universe, equal-weight top-K picks, measure forward
return over a horizon in **bars** (short horizon, cadence-appropriate).

Evaluation times are **subsampled** with stride so the grid size stays near
``COMPETITIVE_BACKTEST_MAX_EVAL_POINTS`` (default 2000). More 1m bars increase
stride, not ``n_periods``; raise ``COMPETITIVE_BACKTEST_MAX_EVAL_POINTS`` if you
need more minute-step evaluations.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
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


def resolve_backtest_symbols(cfg: dict, con) -> Tuple[List[str], Optional[str]]:
    """
    Symbol universe for walk-forward backtest.
    Modes: ``universe`` (default), ``env`` (COMPETITIVE_BACKTEST_SYMBOLS), ``p0-full-coverage``.
    Returns (symbols, error_message).
    """
    mode = (cfg.get("competitive_backtest_symbol_mode") or "universe").strip().lower()
    if mode in ("env", "from_env", "symbols_env"):
        raw = (cfg.get("competitive_backtest_symbols") or "").strip()
        if not raw:
            return [], "competitive_backtest_symbols_empty:set COMPETITIVE_BACKTEST_SYMBOLS or use another --backtest-symbols mode"
        syms = [x.strip().upper() for x in raw.split(",") if x.strip()]
        if not syms:
            return [], "competitive_backtest_symbols_empty"
        return syms, None
    if mode in ("p0-full-coverage", "p0_full_coverage", "full-coverage-p0"):
        from telegram_agent.competitive_coverage import list_p0_full_coverage_symbols

        syms = list_p0_full_coverage_symbols(cfg, con)
        if not syms:
            return [], "p0_full_coverage:no_symbols_meet_thresholds"
        return syms, None
    # default: same as competitive bot live run (priority-filtered universe)
    symbols_u = _symbols_for_competition(cfg)
    return symbols_u, None if symbols_u else "no_symbols_in_universe"

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


def _summarize_leg_returns(rets: List[float]) -> Dict[str, Any]:
    """Aggregate stats for a list of forward-return legs (one symbol)."""
    if not rets:
        return {
            "n_legs": 0,
            "mean_ret_pct": None,
            "std_ret_pct": None,
            "win_rate": None,
            "sharpe_like": None,
        }
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / max(1, (len(rets) - 1))
    sd = math.sqrt(var) if var > 0 else 0.0
    wins = sum(1 for x in rets if x > 0)
    sharpe_like = (m / sd) if sd > 1e-12 else 0.0
    return {
        "n_legs": len(rets),
        "mean_ret_pct": round(m, 6),
        "std_ret_pct": round(sd, 6),
        "win_rate": round(wins / len(rets), 6),
        "sharpe_like": round(sharpe_like, 6),
    }


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
    per_ticker = bool(cfg.get("competitive_backtest_per_ticker", False))
    ticker_legs: Dict[str, List[float]] = defaultdict(list) if per_ticker else {}

    if len(ref_series) < min_bars + horizon + 2:
        err: Dict[str, Any] = {"error": "insufficient_reference_bars", "n_periods": 0}
        if per_ticker:
            err["by_ticker"] = {sym: _summarize_leg_returns([]) for sym in symbols}
        return err

    start_i = min_bars
    last_i = len(ref_series) - horizon - 1
    n_span = max(0, last_i - start_i + 1)
    # Cap how many evaluation times we visit (default ~2000). More bars (e.g. 1m vs 1h)
    # increases stride; n_periods stays ~max_eval, it does not scale with bar count.
    stride = max(1, n_span // max_eval) if n_span else 1
    eval_grid_points = len(range(start_i, last_i + 1, stride)) if n_span else 0

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
                if per_ticker:
                    ticker_legs[p.symbol].append(float(r))
        if not leg:
            continue
        rets.append(float(sum(leg) / len(leg)))

    wf_meta = {
        "reference_bars": len(ref_series),
        "walk_forward_span_bars": n_span,
        "max_eval_budget": max_eval,
        "eval_grid_points": eval_grid_points,
        "stride": stride,
        "horizon_bars": horizon,
        "subsampling": stride > 1,
        "subsampling_note": (
            "n_periods is capped by COMPETITIVE_BACKTEST_MAX_EVAL_POINTS via stride; "
            "minute data uses a larger stride than hourly, so period counts stay similar."
        ),
    }

    if not rets:
        out: Dict[str, Any] = {
            "n_periods": 0,
            "mean_ret_pct": None,
            "std_ret_pct": None,
            "win_rate": None,
            "sharpe_like": None,
            "note": "no_valid_periods",
        }
        out.update(wf_meta)
        if per_ticker:
            out["by_ticker"] = {sym: _summarize_leg_returns(list(ticker_legs.get(sym, []))) for sym in symbols}
        return out

    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / max(1, (len(rets) - 1))
    sd = math.sqrt(var) if var > 0 else 0.0
    wins = sum(1 for x in rets if x > 0)
    sharpe_like = (m / sd) if sd > 1e-12 else 0.0
    out_ok: Dict[str, Any] = {
        "n_periods": len(rets),
        "mean_ret_pct": round(m, 6),
        "std_ret_pct": round(sd, 6),
        "win_rate": round(wins / len(rets), 6),
        "sharpe_like": round(sharpe_like, 6),
    }
    out_ok.update(wf_meta)
    if per_ticker:
        out_ok["by_ticker"] = {
            sym: _summarize_leg_returns(list(ticker_legs.get(sym, []))) for sym in symbols
        }
    return out_ok


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
    symbols_u, sym_err = resolve_backtest_symbols(cfg, con)
    if not symbols_u:
        con.close()
        return {
            "ok": False,
            "error": sym_err or "no_symbols",
            "symbol_mode": cfg.get("competitive_backtest_symbol_mode", "universe"),
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
        "symbol_mode": cfg.get("competitive_backtest_symbol_mode", "universe"),
        "symbol_pool_size": len(symbols_u),
        "per_ticker_enabled": bool(cfg.get("competitive_backtest_per_ticker", False)),
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
