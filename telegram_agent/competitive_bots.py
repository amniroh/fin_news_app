"""
Competitive systematic “bots” on priority-filtered universe symbols.

We ship three well-known equity factor styles commonly used in quant competitions
and short-horizon systematic trading (see e.g. Jegadeesh & Titman momentum; RSI mean
reversion; Donchian/breakout trend). Each run inserts concrete-plan recommendations
tagged with ``meta_json.competitive_bot_id`` and evaluates them with the same
``run_suggestion_tests`` machinery as research suggestions (per-bot aggregate in kv_state).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telegram_agent.agent_db import (
    connect,
    get_adj_closes_series,
    init_db,
    insert_competitive_bot_run,
    insert_recommendation,
)
from telegram_agent.agent_tester import load_strategy_test_aggregate, run_suggestion_tests
from telegram_agent.symbol_universe import load_symbol_universe

logger = logging.getLogger(__name__)

# Stable ids stored in meta_json.competitive_bot_id and competitive_bot_runs.bot_id
COMPETITIVE_BOT_SPECS: Tuple[Tuple[str, str], ...] = (
    (
        "jt_momentum_5d",
        "5d cross-sectional momentum (classic short-horizon momentum factor)",
    ),
    (
        "rsi_mean_reversion",
        "RSI(14) oversold long bias after mild pullback (mean-reversion tilt)",
    ),
    (
        "donchian_breakout_20",
        "20d price breakout / Donchian-style trend continuation",
    ),
)


def _rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    avg_g = gains / period
    avg_l = losses / period
    if avg_l <= 1e-12:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _momentum_score(closes: Sequence[float]) -> Optional[float]:
    if len(closes) < 6:
        return None
    c0, c5 = closes[-1], closes[-6]
    if c5 <= 0:
        return None
    return (c0 / c5 - 1.0) * 100.0


def _mean_reversion_score(closes: Sequence[float]) -> Optional[float]:
    if len(closes) < 20:
        return None
    r = _rsi(list(closes), 14)
    if r is None:
        return None
    mom5 = _momentum_score(closes)
    if mom5 is None:
        return None
    # Prefer mild oversold + recent dip (contrarian long)
    if r > 42 or r < 18:
        return None
    if mom5 > 2.0:
        return None
    return (40.0 - r) * (1.0 + min(5.0, abs(mom5)) / 5.0)


def _breakout_score(closes: Sequence[float]) -> Optional[float]:
    if len(closes) < 21:
        return None
    body = list(closes[-21:-1])
    last = closes[-1]
    mx = max(body)
    if mx <= 0:
        return None
    if last < mx * 0.998:
        return None
    return (last / mx - 1.0) * 100.0 + 2.0  # small bonus for new highs


_SCORERS = {
    "jt_momentum_5d": _momentum_score,
    "rsi_mean_reversion": _mean_reversion_score,
    "donchian_breakout_20": _breakout_score,
}


@dataclass
class _Candidate:
    symbol: str
    score: float


def _symbols_for_competition(cfg: dict) -> List[str]:
    max_pri = int(cfg.get("competitive_bots_max_priority", 1))
    cfg2 = {**cfg, "max_priority": max_pri}
    syms = load_symbol_universe(cfg2)
    return list(syms) if syms else []


def _rank_candidates(
    con,
    cfg: dict,
    bot_id: str,
    symbols: Sequence[str],
    now: datetime,
) -> List[_Candidate]:
    scorer = _SCORERS.get(bot_id)
    if not scorer:
        return []
    min_bars = int(cfg.get("competitive_bots_min_bars", 25))
    out: List[_Candidate] = []
    for sym in symbols:
        series = get_adj_closes_series(
            con, sym, now, interval="1d", limit=max(80, min_bars + 5)
        )
        if len(series) < min_bars:
            continue
        closes = [p for _, p in series]
        raw = scorer(closes)
        if raw is None:
            continue
        if not math.isfinite(raw):
            continue
        out.append(_Candidate(symbol=sym, score=float(raw)))
    out.sort(key=lambda x: x.score, reverse=True)
    return out


def _confidence_from_rank(rank: int, n: int) -> float:
    base = 0.82 - min(0.22, rank * 0.06)
    return max(0.52, min(0.88, base))


def _insert_picks_for_bot(
    con,
    cfg: dict,
    bot_id: str,
    description: str,
    candidates: Sequence[_Candidate],
    now: datetime,
    batch_tag: str,
) -> Tuple[List[int], List[Dict[str, Any]]]:
    max_picks = max(1, int(cfg.get("competitive_bots_max_picks", 3)))
    horizon_days = max(3, int(cfg.get("competitive_bots_review_horizon_days", 12)))
    entry_span_days = max(1, int(cfg.get("competitive_bots_entry_span_days", 3)))

    picks = candidates[:max_picks]
    rec_ids: List[int] = []
    summaries: List[Dict[str, Any]] = []
    for i, c in enumerate(picks):
        fc = max(-8.0, min(18.0, c.score))
        conf = _confidence_from_rank(i, len(picks))
        sug = now
        ew0 = now
        ew1 = now + timedelta(days=entry_span_days)
        ex = now + timedelta(days=horizon_days)
        rationale = (
            f"[{bot_id}] {description} — score={c.score:.3f} (batch {batch_tag}). "
            "Systematic rule; not investment advice."
        )
        meta = {
            "competitive_bot_id": bot_id,
            "source": "competitive_bot",
            "competitive_batch": batch_tag,
            "score": round(c.score, 6),
            "description": description,
        }
        rid = insert_recommendation(
            con,
            symbol=c.symbol,
            duration="plan",
            forecast_pct=round(fc, 4),
            confidence=conf,
            rationale=rationale,
            meta=meta,
            ts_utc=now,
            suggestion_ts_utc=sug,
            entry_window_start_utc=ew0,
            entry_window_end_utc=ew1,
            execute_review_utc=ex,
        )
        rec_ids.append(rid)
        summaries.append(
            {
                "id": rid,
                "symbol": c.symbol,
                "forecast_pct": round(fc, 4),
                "confidence": conf,
                "score": c.score,
            }
        )
    return rec_ids, summaries


def run_competitive_cycle(cfg: dict, *, cadence_label: str) -> Dict[str, Any]:
    """
    Run all three bots: insert top picks per bot, evaluate with ``run_suggestion_tests``
    scoped to each ``competitive_bot_id``, persist rows in ``competitive_bot_runs``.
    """
    cfg = {**cfg}
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    now = datetime.now(timezone.utc)
    batch_tag = now.strftime("%Y%m%dT%H%M%SZ")
    symbols = _symbols_for_competition(cfg)
    if not symbols:
        logger.warning("competitive_bots: no symbols (check universe + max_priority)")
        return {"ok": False, "error": "no_symbols", "cadence_label": cadence_label}

    con = connect(db)
    init_db(con)
    results: List[Dict[str, Any]] = []

    try:
        for bot_id, desc in COMPETITIVE_BOT_SPECS:
            candidates = _rank_candidates(con, cfg, bot_id, symbols, now)
            rec_ids, pick_summaries = _insert_picks_for_bot(
                con, cfg, bot_id, desc, candidates, now, batch_tag
            )

            n_tested = run_suggestion_tests(
                cfg,
                asof_utc=now,
                concluded_only=False,
                competitive_bot_id=bot_id,
            )

            agg = load_strategy_test_aggregate(con, competitive_bot_id=bot_id) or {}
            opt_m = agg.get("optimization_metric")
            opt_v = agg.get("optimization_value")
            n_legs = agg.get("n_legs")

            insert_competitive_bot_run(
                con,
                bot_id=bot_id,
                cadence_label=cadence_label,
                run_ts_utc=now,
                n_recommendations=len(rec_ids),
                n_tester_legs=int(n_legs or n_tested),
                optimization_metric=str(opt_m) if opt_m is not None else None,
                optimization_value=float(opt_v) if opt_v is not None else None,
                aggregate=agg,
                picks=pick_summaries,
            )

            results.append(
                {
                    "bot_id": bot_id,
                    "description": desc,
                    "recommendation_ids": rec_ids,
                    "n_tester_rows": n_tested,
                    "aggregate": agg,
                    "picks": pick_summaries,
                }
            )
    finally:
        con.close()

    out: Dict[str, Any] = {
        "ok": True,
        "cadence_label": cadence_label,
        "batch_tag": batch_tag,
        "run_ts_utc": now.isoformat(),
        "universe_size": len(symbols),
        "bots": results,
    }
    logger.info(
        "competitive_bots cycle done cadence=%s batch=%s bots=%s",
        cadence_label,
        batch_tag,
        len(results),
    )
    return out


def format_competitive_summary_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=str)
