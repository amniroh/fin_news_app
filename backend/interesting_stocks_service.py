"""
Interesting stocks list, coverage checks, yfinance analyst ratings, and gap backfills.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yfinance as yf

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (_REPO_ROOT, _BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from value_metrics_store import (
    add_interesting_stock,
    batch_latest_analyst_ratings,
    batch_latest_value_trading_assessments,
    connect,
    count_analyst_rating_snapshots,
    count_daily_metrics_in_window,
    count_fundamental_points_in_window,
    count_interesting_stocks,
    init_db,
    latest_analyst_rating,
    latest_value_trading_assessment,
    list_interesting_stocks,
    query_analyst_ratings,
    query_fundamental_points,
    query_metric_points,
    query_value_trading_assessments,
    remove_interesting_stock,
    upsert_analyst_ratings,
    upsert_interesting_stocks,
)

logger = logging.getLogger(__name__)

COVERAGE_YEARS = 2
MIN_QUARTERLY_FUNDAMENTALS = 4
MIN_DAILY_METRICS = 200
MIN_LINKED_NEWS = 3


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coverage_window() -> Tuple[str, str, datetime, datetime]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(COVERAGE_YEARS * 365.25))
    return start.isoformat(), end.isoformat(), datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc), datetime.combine(
        end, datetime.max.time(), tzinfo=timezone.utc
    )


def default_universe_path() -> Path:
    env_p = (os.getenv("SYMBOL_UNIVERSE_PATH") or "").strip()
    if env_p:
        p = Path(env_p).expanduser()
        if p.is_file():
            return p
    for name in (
        "top1000_investments_prioritised.json",
        "top1000_investments.json",
        "data/symbol_universe_top1000.json",
    ):
        p = _REPO_ROOT / "telegram_agent" / name
        if p.is_file():
            return p
    return _REPO_ROOT / "telegram_agent" / "top1000_investments_prioritised.json"


def load_universe_priorities(path: Optional[Path] = None) -> Dict[str, int]:
    """Return {SYMBOL: priority} from universe JSON."""
    p = path or default_universe_path()
    if not p.is_file():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    out: Dict[str, int] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            sym = str(k).strip().upper()
            if not sym:
                continue
            try:
                out[sym] = int(v)
            except (TypeError, ValueError):
                out[sym] = 3
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                out[item.strip().upper()] = 3
            elif isinstance(item, dict) and item.get("ticker"):
                sym = str(item["ticker"]).strip().upper()
                out[sym] = int(item.get("priority", 3))
    return out


def seed_interesting_stocks_from_universe(
    vm_db: Path,
    *,
    universe_path: Optional[Path] = None,
    force: bool = False,
) -> int:
    priorities = load_universe_priorities(universe_path)
    if not priorities:
        raise FileNotFoundError(f"Universe file not found or empty: {universe_path or default_universe_path()}")
    con = connect(vm_db)
    init_db(con)
    try:
        if not force and count_interesting_stocks(con) > 0:
            return 0
        rows = [{"symbol": s, "universe_priority": pr} for s, pr in sorted(priorities.items())]
        return upsert_interesting_stocks(con, rows)
    finally:
        con.close()


def _agent_db_path() -> Path:
    raw = (os.getenv("AGENT_DB_PATH") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = _REPO_ROOT / p
        return p
    return _REPO_ROOT / "telegram_agent" / "data" / "agent.sqlite"


def _agent_connect():
    from telegram_agent.agent_db import connect as agent_connect, init_db as agent_init_db

    p = _agent_db_path()
    con = agent_connect(p)
    agent_init_db(con)
    return con


def _count_linked_news(symbol: str, start_s: str, end_s: str) -> int:
    try:
        con = _agent_connect()
    except Exception:
        return 0
    try:
        cur = con.execute(
            """
            SELECT COUNT(DISTINCT n.id) AS c
            FROM news_items n
            INNER JOIN symbol_news_linkage l ON l.news_id = n.id
            WHERE l.symbol = ?
              AND SUBSTR(n.ts_utc, 1, 10) >= ?
              AND SUBSTR(n.ts_utc, 1, 10) <= ?
            """,
            (symbol.upper(), start_s, end_s),
        )
        row = cur.fetchone()
        return int(row["c"]) if row else 0
    finally:
        con.close()


def coverage_for_symbol(
    vm_db: Path,
    symbol: str,
    *,
    years: float = COVERAGE_YEARS,
) -> Dict[str, Any]:
    start_s, end_s, start_utc, end_utc = _coverage_window()
    sym = str(symbol).strip().upper()
    con = connect(vm_db)
    init_db(con)
    try:
        n_fund = count_fundamental_points_in_window(
            con, symbol=sym, start_date=start_s, end_date=end_s, provider="yfinance", period="quarter"
        )
        n_metrics = count_daily_metrics_in_window(con, symbol=sym, start_date=start_s, end_date=end_s, provider="yfinance")
        has_analyst = latest_analyst_rating(con, symbol=sym) is not None
        n_analyst = count_analyst_rating_snapshots(con, sym)
    finally:
        con.close()

    prices_ok = False
    try:
        from telegram_agent.agent_db import has_prices_covering_window

        agent_con = _agent_connect()
        try:
            prices_ok = has_prices_covering_window(
                agent_con, symbol=sym, start_utc=start_utc, end_utc=end_utc, interval="1d"
            )
        finally:
            agent_con.close()
    except Exception:
        prices_ok = False

    n_news = _count_linked_news(sym, start_s, end_s)

    fundamentals_ok = n_fund >= MIN_QUARTERLY_FUNDAMENTALS
    metrics_ok = n_metrics >= MIN_DAILY_METRICS
    news_ok = n_news >= MIN_LINKED_NEWS
    analyst_ok = has_analyst

    gaps = []
    if not prices_ok:
        gaps.append("prices")
    if not fundamentals_ok or not metrics_ok:
        gaps.append("fundamentals")
    if not news_ok:
        gaps.append("news")
    if not analyst_ok:
        gaps.append("analyst_ratings")

    return {
        "symbol": sym,
        "window": {"start": start_s, "end": end_s, "years": years},
        "coverage": {
            "prices": prices_ok,
            "fundamentals": fundamentals_ok,
            "daily_metrics": metrics_ok,
            "news": news_ok,
            "analyst_ratings": analyst_ok,
        },
        "counts": {
            "quarterly_fundamentals": n_fund,
            "daily_metrics": n_metrics,
            "linked_news": n_news,
            "analyst_snapshots": n_analyst,
        },
        "gaps": gaps,
        "needs_backfill": len(gaps) > 0,
    }


def list_stocks_with_coverage(vm_db: Path) -> List[Dict[str, Any]]:
    seed_interesting_stocks_from_universe(vm_db)
    con = connect(vm_db)
    init_db(con)
    try:
        stocks = list_interesting_stocks(con)
        syms = [str(s["symbol"]) for s in stocks]
        latest_map = batch_latest_analyst_ratings(con, syms)
        vt_map = batch_latest_value_trading_assessments(con, syms)
    finally:
        con.close()
    out = []
    for s in stocks:
        sym = str(s["symbol"])
        cov = coverage_for_symbol(vm_db, sym)
        row = {**s, **cov}
        la = latest_map.get(sym)
        if la:
            row["latest_analyst"] = {
                "asof_date": la.get("asof_date"),
                "recommendation_key": la.get("recommendation_key"),
                "recommendation_mean": la.get("recommendation_mean"),
                "target_mean": la.get("target_mean"),
            }
        vt = vt_map.get(sym)
        if vt:
            row["latest_value_trading"] = {
                "produced_ts_utc": vt.get("produced_ts_utc"),
                "total_score": vt.get("total_score"),
                "investment_name": vt.get("investment_name"),
                "model": vt.get("model"),
                "overall_summary": vt.get("overall_summary"),
                "pillar_scores": {
                    "competitive_edge": vt.get("competitive_edge_score"),
                    "management_competence": vt.get("management_competence_score"),
                    "financial_fortress": vt.get("financial_fortress_score"),
                    "pricing_power": vt.get("pricing_power_score"),
                    "understandability": vt.get("understandability_score"),
                    "valuation": vt.get("valuation_score"),
                },
            }
        out.append(row)
    return out


def _asof_date_from_rec_period(period_label: str, ref: date) -> str:
    """Map yfinance recommendation period labels (0m, -1m, …) to approximate month-end dates."""
    label = str(period_label).strip().lower()
    m = re.match(r"^(-?\d+)m$", label)
    if not m:
        return ref.isoformat()
    months_back = abs(int(m.group(1)))
    y, mo = ref.year, ref.month - months_back
    while mo <= 0:
        mo += 12
        y -= 1
    # last day of target month (approximate with 28)
    return date(y, mo, 28).isoformat()


def recommendation_key_from_mean(mean: Optional[float]) -> Optional[str]:
    """Map Yahoo-style consensus mean (1=strong buy … 5=strong sell) to recommendation_key."""
    if mean is None:
        return None
    try:
        m = float(mean)
    except (TypeError, ValueError):
        return None
    if m <= 1.5:
        return "strong_buy"
    if m <= 2.5:
        return "buy"
    if m <= 3.5:
        return "hold"
    if m <= 4.5:
        return "sell"
    return "strong_sell"


def _consensus_mean_from_counts(
    strong_buy: int,
    buy: int,
    hold: int,
    sell: int,
    strong_sell: int,
) -> Optional[float]:
    total = strong_buy + buy + hold + sell + strong_sell
    if total <= 0:
        return None
    score = (
        strong_buy * 1 + buy * 2 + hold * 3 + sell * 4 + strong_sell * 5
    ) / float(total)
    return round(score, 4)


def fetch_yfinance_analyst_ratings(symbol: str) -> List[Dict[str, Any]]:
    """
    Fetch analyst consensus from yfinance.

    Returns one row per stored asof_date: today's snapshot plus monthly breakdown rows
    from the recommendations table (0m, -1m, …) when available.
    """
    sym = str(symbol).strip().upper()
    t = yf.Ticker(sym)
    info = getattr(t, "info", None) or {}
    rec_df = getattr(t, "recommendations", None)
    targets = getattr(t, "analyst_price_targets", None) or {}

    today = datetime.now(timezone.utc).date()
    fetched = _utcnow_iso()
    points: List[Dict[str, Any]] = []
    seen_dates: set[str] = set()

    def _append_point(
        asof: str,
        *,
        recommendation_key: Optional[str],
        recommendation_mean: Optional[float],
        num_analysts: Optional[int],
        strong_buy: Optional[int],
        buy: Optional[int],
        hold: Optional[int],
        sell: Optional[int],
        strong_sell: Optional[int],
        raw_extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        if asof in seen_dates:
            return
        seen_dates.add(asof)
        points.append(
            {
                "symbol": sym,
                "asof_date": asof,
                "provider": "yfinance",
                "recommendation_key": recommendation_key,
                "recommendation_mean": recommendation_mean,
                "num_analysts": num_analysts,
                "strong_buy": strong_buy,
                "buy": buy,
                "hold": hold,
                "sell": sell,
                "strong_sell": strong_sell,
                "target_current": targets.get("current") if asof == today.isoformat() else None,
                "target_high": targets.get("high") if asof == today.isoformat() else None,
                "target_low": targets.get("low") if asof == today.isoformat() else None,
                "target_mean": targets.get("mean") if asof == today.isoformat() else None,
                "target_median": targets.get("median") if asof == today.isoformat() else None,
                "raw": raw_extra or {},
                "fetched_ts_utc": fetched,
            }
        )

    if rec_df is not None and not getattr(rec_df, "empty", True):
        for idx, row in rec_df.iterrows():
            try:
                sb = int(row.get("strongBuy", 0) or 0)
                b = int(row.get("buy", 0) or 0)
                h = int(row.get("hold", 0) or 0)
                s = int(row.get("sell", 0) or 0)
                ss = int(row.get("strongSell", 0) or 0)
            except Exception:
                continue
            asof = _asof_date_from_rec_period(str(idx), today)
            mean_est = _consensus_mean_from_counts(sb, b, h, s, ss)
            _append_point(
                asof,
                recommendation_key=None,
                recommendation_mean=mean_est,
                num_analysts=sb + b + h + s + ss,
                strong_buy=sb,
                buy=b,
                hold=h,
                sell=s,
                strong_sell=ss,
                raw_extra={"recommendation_period": str(idx), "source": "recommendations_table"},
            )

    sb = buy = hold = sell = strong_sell = None
    if rec_df is not None and not getattr(rec_df, "empty", True):
        try:
            row0 = rec_df.iloc[0]
            sb = int(row0.get("strongBuy", 0) or 0)
            buy = int(row0.get("buy", 0) or 0)
            hold = int(row0.get("hold", 0) or 0)
            sell = int(row0.get("sell", 0) or 0)
            strong_sell = int(row0.get("strongSell", 0) or 0)
        except Exception:
            pass

    info_mean = info.get("recommendationMean")
    try:
        info_mean_f = float(info_mean) if info_mean is not None else None
    except (TypeError, ValueError):
        info_mean_f = None

    _append_point(
        today.isoformat(),
        recommendation_key=info.get("recommendationKey"),
        recommendation_mean=info_mean_f,
        num_analysts=info.get("numberOfAnalystOpinions"),
        strong_buy=sb,
        buy=buy,
        hold=hold,
        sell=sell,
        strong_sell=strong_sell,
        raw_extra={
            "recommendation_period": "current",
            "analyst_price_targets": targets,
            "recommendations_table": rec_df.to_dict() if rec_df is not None and hasattr(rec_df, "to_dict") else None,
        },
    )

    points.sort(key=lambda p: str(p["asof_date"]))
    return points


def fetch_yfinance_analyst_rating(symbol: str) -> Dict[str, Any]:
    """Single latest point (compat)."""
    pts = fetch_yfinance_analyst_ratings(symbol)
    return pts[-1] if pts else {}


def fetch_analyst_ratings_for_symbols(
    vm_db: Path,
    symbols: Sequence[str],
    *,
    sleep_seconds: float = 0.35,
) -> Dict[str, Any]:
    con = connect(vm_db)
    init_db(con)
    ok: List[str] = []
    failed: List[Dict[str, str]] = []
    try:
        for sym in symbols:
            s = str(sym).strip().upper()
            if not s:
                continue
            try:
                pts = fetch_yfinance_analyst_ratings(s)
                if pts:
                    upsert_analyst_ratings(con, pts)
                ok.append(s)
            except Exception as e:
                failed.append({"symbol": s, "error": str(e)})
            time.sleep(max(0.0, float(sleep_seconds)))
    finally:
        con.close()
    return {"ok": ok, "failed": failed, "n_ok": len(ok), "n_failed": len(failed)}


def fetch_analyst_ratings_priority_zero(vm_db: Path) -> Dict[str, Any]:
    seed_interesting_stocks_from_universe(vm_db)
    con = connect(vm_db)
    init_db(con)
    try:
        stocks = list_interesting_stocks(con)
    finally:
        con.close()
    p0 = [str(s["symbol"]) for s in stocks if int(s.get("universe_priority", 3)) == 0]
    return fetch_analyst_ratings_for_symbols(vm_db, p0)


def _backfill_prices(symbols: List[str], days: int = 730) -> Dict[str, Any]:
    if not symbols:
        return {"skipped": True, "reason": "no symbols"}
    try:
        from telegram_agent.config import load_config
        from telegram_agent.prices import run_prices

        cfg = load_config()
        cfg["prices_symbols"] = [str(s).strip().upper() for s in symbols if str(s).strip()]
        run_prices(cfg, mode="backfill", days=days, intervals="1d")
        return {"ok": True, "symbols": symbols, "days": days}
    except Exception as e:
        logger.exception("Price backfill failed")
        return {"ok": False, "error": str(e), "symbols": symbols}


def extend_recent_daily_metrics(
    vm_db: Path,
    *,
    symbols: Optional[Sequence[str]] = None,
    since_date: Optional[str] = None,
    provider: str = "yfinance",
) -> Dict[str, Any]:
    """
    Append daily ``vm_metric_points`` from each symbol's last stored asof_date + 1 through today.

    ``since_date`` (YYYY-MM-DD) sets a minimum start when catching up after missed job runs.
    """
    from value_metrics_daily_backfill import _compute_daily_metrics_for_symbol

    end_d = datetime.now(timezone.utc).date()
    end_s = end_d.isoformat()
    floor_d = date.fromisoformat(str(since_date)[:10]) if since_date else None

    con = connect(vm_db)
    init_db(con)
    try:
        if symbols:
            sym_list = [str(s).strip().upper() for s in symbols if str(s).strip()]
        else:
            sym_list = [
                str(r["symbol"]).strip().upper() for r in list_interesting_stocks(con) if r.get("symbol")
            ]
        if not sym_list:
            return {"skipped": True, "reason": "no symbols"}

        ph = ",".join("?" * len(sym_list))
        cur = con.execute(
            f"""
            SELECT symbol, MAX(asof_date) AS dmax
            FROM vm_metric_points
            WHERE period = 'daily' AND provider = ? AND symbol IN ({ph})
            GROUP BY symbol
            """,
            [provider] + sym_list,
        )
        last_map = {str(r["symbol"]): str(r["dmax"]) for r in cur.fetchall()}

        ok: List[str] = []
        skipped: List[str] = []
        failed: List[Dict[str, str]] = []
        n_rows = 0
        for sym in sym_list:
            last_s = last_map.get(sym)
            if last_s:
                start_d = date.fromisoformat(last_s[:10]) + timedelta(days=1)
            elif floor_d:
                start_d = floor_d
            else:
                skipped.append(sym)
                continue
            if floor_d and start_d < floor_d:
                start_d = floor_d
            if start_d > end_d:
                skipped.append(sym)
                continue
            try:
                n = _compute_daily_metrics_for_symbol(
                    con,
                    symbol=sym,
                    start_s=start_d.isoformat(),
                    end_s=end_s,
                    provider=provider,
                )
                n_rows += int(n)
                ok.append(sym)
            except Exception as e:
                failed.append({"symbol": sym, "error": str(e)})
            time.sleep(0.25)
    finally:
        con.close()

    return {
        "window_end": end_s,
        "since_date": since_date,
        "n_ok": len(ok),
        "n_skipped": len(skipped),
        "n_failed": len(failed),
        "daily_rows_upserted": n_rows,
        "failed_sample": failed[:20],
    }


def _backfill_fundamentals(vm_db: Path, symbols: List[str], start_s: str, end_s: str) -> Dict[str, Any]:
    from value_metrics_daily_backfill import _compute_daily_metrics_for_symbol, _ensure_fundamentals

    con = connect(vm_db)
    init_db(con)
    ok: List[str] = []
    failed: List[Dict[str, str]] = []
    try:
        for sym in symbols:
            try:
                _ensure_fundamentals(
                    con, symbol=sym, start_s=start_s, end_s=end_s, provider="yfinance", refresh=False
                )
                _compute_daily_metrics_for_symbol(
                    con, symbol=sym, start_s=start_s, end_s=end_s, provider="yfinance"
                )
                ok.append(sym)
            except Exception as e:
                failed.append({"symbol": sym, "error": str(e)})
            time.sleep(0.25)
    finally:
        con.close()
    return {"ok": ok, "failed": failed}


def _backfill_news(symbols: List[str], days: int = 730) -> Dict[str, Any]:
    """Per-symbol API news when Finnhub key is configured."""
    key = (os.getenv("FINNHUB_API_KEY") or "").strip()
    if not key:
        return {"skipped": True, "reason": "FINNHUB_API_KEY not set"}
    try:
        from telegram_agent.config import load_config
        from telegram_agent.collectors.finnhub_collector import collect_finnhub
        from telegram_agent.agent_db import connect as agent_connect, init_db as agent_init_db, upsert_news_items
        from telegram_agent.models import NewsItem

        cfg = load_config()
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)
        agent_con = agent_connect(_agent_db_path())
        agent_init_db(agent_con)
        total = 0
        for sym in symbols:
            cfg_run = {**cfg, "finnhub_symbols": sym}
            try:
                items = collect_finnhub(cfg_run, since=since, until=now, mode="backfill")
                if items:
                    total += upsert_news_items(agent_con, items)
            except Exception as e:
                logger.warning("Finnhub backfill %s: %s", sym, e)
            time.sleep(float(cfg.get("finnhub_sleep_seconds", 0.2)))
        agent_con.close()
        return {"ok": True, "upserted": total, "symbols": symbols}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def summarize_coverage_gaps(vm_db: Path) -> Dict[str, Any]:
    """Aggregate gap counts across interesting stocks (read-only)."""
    rows = list_stocks_with_coverage(vm_db)
    by_gap: Dict[str, int] = {}
    need_any = 0
    for r in rows:
        gaps = r.get("gaps") or []
        if gaps:
            need_any += 1
        for g in gaps:
            by_gap[g] = by_gap.get(g, 0) + 1
    return {
        "n_stocks": len(rows),
        "n_with_gaps": need_any,
        "gaps_by_type": by_gap,
        "rows": rows,
    }


def run_global_news_pipeline() -> Dict[str, Any]:
    """
    Incremental news ingest (telegram/rss/api per config) + universe preprocess linkage.
    Run once per daily job before per-symbol news backfill.
    """
    import asyncio

    from telegram_agent.config import load_config
    from telegram_agent.ingest import run_ingest
    from telegram_agent.news_universe_preprocess import run_news_universe_preprocess

    cfg = load_config()
    now = datetime.now(timezone.utc)
    try:
        ingest_n = asyncio.run(run_ingest(cfg, mode="incremental"))
    except Exception as e:
        logger.exception("Incremental ingest failed")
        ingest_n = 0
        ingest_err = str(e)
    else:
        ingest_err = None

    preprocess_out: Dict[str, Any] = {"skipped": True}
    try:
        agent_con = _agent_connect()
        try:
            preprocess_out = run_news_universe_preprocess(cfg, agent_con, max_ts_utc_inclusive=now)
        finally:
            agent_con.close()
    except Exception as e:
        logger.exception("Universe preprocess failed")
        preprocess_out = {"ok": False, "error": str(e)}

    return {
        "ingest_upserted": ingest_n,
        "ingest_error": ingest_err,
        "preprocess": preprocess_out,
    }


def run_daily_backfill_pipeline(
    vm_db: Path,
    *,
    only_gaps: bool = True,
    symbols: Optional[Sequence[str]] = None,
    run_ingest: bool = True,
    years: float = COVERAGE_YEARS,
) -> Dict[str, Any]:
    """
    Full daily pipeline: seed universe → optional global news ingest/preprocess → gap backfills.
    """
    global COVERAGE_YEARS
    prev_years = COVERAGE_YEARS
    try:
        COVERAGE_YEARS = float(years)
        seed_interesting_stocks_from_universe(vm_db)
        start_s, end_s, _, _ = _coverage_window()
        summary_before = summarize_coverage_gaps(vm_db)

        # Drop Alpaca daily bars for crypto (ETH/BTC ticker = equity ETF on Alpaca, not coin).
        try:
            from telegram_agent.agent_db import connect as agent_connect, delete_alpaca_daily_prices, init_db as agent_init_db
            from telegram_agent.symbol_universe import crypto_symbols_from_universe

            crypto = sorted(crypto_symbols_from_universe())
            if crypto:
                ac = agent_connect(_agent_db_path())
                agent_init_db(ac)
                try:
                    n_rm = delete_alpaca_daily_prices(ac, crypto)
                    if n_rm:
                        logger.info("Removed %s Alpaca daily bar(s) for crypto symbol collisions", n_rm)
                finally:
                    ac.close()
        except Exception as e:
            logger.warning("Crypto Alpaca cleanup skipped: %s", e)

        out: Dict[str, Any] = {
            "started_ts_utc": _utcnow_iso(),
            "window": {"start": start_s, "end": end_s, "years": years},
            "coverage_before": {
                "n_stocks": summary_before["n_stocks"],
                "n_with_gaps": summary_before["n_with_gaps"],
                "gaps_by_type": summary_before["gaps_by_type"],
            },
        }

        if run_ingest:
            logger.info("Running global news ingest + universe preprocess")
            out["global_news"] = run_global_news_pipeline()

        logger.info("Running gap backfills (only_gaps=%s)", only_gaps)
        out["backfill"] = run_gap_backfills(vm_db, symbols=symbols, only_gaps=only_gaps)

        summary_after = summarize_coverage_gaps(vm_db)
        out["coverage_after"] = {
            "n_stocks": summary_after["n_stocks"],
            "n_with_gaps": summary_after["n_with_gaps"],
            "gaps_by_type": summary_after["gaps_by_type"],
        }
        out["finished_ts_utc"] = _utcnow_iso()
        return out
    finally:
        COVERAGE_YEARS = prev_years


def run_gap_backfills(
    vm_db: Path,
    *,
    symbols: Optional[Sequence[str]] = None,
    only_gaps: bool = True,
) -> Dict[str, Any]:
    """Backfill prices, fundamentals, news, and analyst ratings for symbols missing 2y coverage."""
    seed_interesting_stocks_from_universe(vm_db)
    start_s, end_s, _, _ = _coverage_window()
    days = int(COVERAGE_YEARS * 365.25)

    if symbols:
        sym_list = [str(s).strip().upper() for s in symbols if str(s).strip()]
    else:
        con = connect(vm_db)
        init_db(con)
        try:
            sym_list = [str(r["symbol"]) for r in list_interesting_stocks(con)]
        finally:
            con.close()

    need_prices: List[str] = []
    need_fundamentals: List[str] = []
    need_news: List[str] = []
    need_analyst: List[str] = []
    per_symbol: Dict[str, Dict[str, Any]] = {}

    for sym in sym_list:
        cov = coverage_for_symbol(vm_db, sym)
        per_symbol[sym] = cov
        if not only_gaps or "prices" in cov["gaps"]:
            need_prices.append(sym)
        if not only_gaps or "fundamentals" in cov["gaps"]:
            need_fundamentals.append(sym)
        if not only_gaps or "news" in cov["gaps"]:
            need_news.append(sym)
        if not only_gaps or "analyst_ratings" in cov["gaps"]:
            need_analyst.append(sym)

    results: Dict[str, Any] = {
        "window": {"start": start_s, "end": end_s},
        "symbols_considered": len(sym_list),
        "jobs": {},
    }
    if need_prices:
        results["jobs"]["prices"] = _backfill_prices(need_prices, days=days)
    if need_fundamentals:
        results["jobs"]["fundamentals"] = _backfill_fundamentals(vm_db, need_fundamentals, start_s, end_s)
    if need_news:
        results["jobs"]["news"] = _backfill_news(need_news, days=days)
    if need_analyst:
        results["jobs"]["analyst_ratings"] = fetch_analyst_ratings_for_symbols(vm_db, need_analyst)

    results["per_symbol_coverage_before"] = per_symbol
    return results


def _agent_price_history(symbol: str, start_s: str, end_s: str) -> List[Dict[str, Any]]:
    try:
        from telegram_agent.agent_db import query_daily_prices

        con = _agent_connect()
    except Exception:
        return []
    try:
        return query_daily_prices(con, symbol, start_date=start_s, end_date=end_s)
    finally:
        con.close()


def _agent_news_for_symbol(symbol: str, limit: int = 40) -> List[Dict[str, Any]]:
    try:
        con = _agent_connect()
    except Exception:
        return []
    try:
        cur = con.execute(
            """
            SELECT n.id, n.ts_utc, n.source_type, n.source_name, n.title, n.url, n.content, n.condensed
            FROM news_items n
            INNER JOIN symbol_news_linkage l ON l.news_id = n.id
            WHERE l.symbol = ?
            ORDER BY n.ts_utc DESC
            LIMIT ?
            """,
            (symbol.upper(), int(limit)),
        )
        rows = []
        for r in cur.fetchall():
            body = (r["condensed"] or r["content"] or "") or ""
            rows.append(
                {
                    "id": r["id"],
                    "ts_utc": r["ts_utc"],
                    "source_type": r["source_type"],
                    "source_name": r["source_name"],
                    "title": r["title"],
                    "url": r["url"],
                    "snippet": body[:500] if body else None,
                }
            )
        return rows
    finally:
        con.close()


def _agent_recommendations_for_symbol(symbol: str, limit: int = 30) -> List[Dict[str, Any]]:
    try:
        con = _agent_connect()
    except Exception:
        return []
    try:
        cur = con.execute(
            """
            SELECT id, ts_utc, symbol, forecast_pct, confidence, rationale, meta_json,
                   suggestion_ts_utc, entry_window_start_utc, entry_window_end_utc
            FROM recommendations
            WHERE symbol = ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (symbol.upper(), int(limit)),
        )
        out = []
        for r in cur.fetchall():
            meta = {}
            try:
                meta = json.loads(r["meta_json"] or "{}")
            except json.JSONDecodeError:
                pass
            out.append(
                {
                    "id": r["id"],
                    "ts_utc": r["ts_utc"],
                    "symbol": r["symbol"],
                    "forecast_pct": r["forecast_pct"],
                    "confidence": r["confidence"],
                    "rationale": r["rationale"],
                    "suggestion_ts_utc": r["suggestion_ts_utc"],
                    "entry_window_start_utc": r["entry_window_start_utc"],
                    "entry_window_end_utc": r["entry_window_end_utc"],
                    "plan": meta.get("plan"),
                    "tester": meta.get("tester"),
                }
            )
        return out
    finally:
        con.close()


def ticker_detail(vm_db: Path, symbol: str) -> Dict[str, Any]:
    sym = str(symbol).strip().upper()
    if not sym:
        raise ValueError("symbol required")
    start_s, end_s, _, _ = _coverage_window()

    con = connect(vm_db)
    init_db(con)
    try:
        stocks = list_interesting_stocks(con)
        stock_row = next((s for s in stocks if str(s["symbol"]) == sym), None)
        metrics = query_metric_points(
            con,
            symbols=[sym],
            start_date=start_s,
            end_date=end_s,
            provider="yfinance",
            period="daily",
        )
        fundamentals = query_fundamental_points(
            con,
            symbols=[sym],
            start_date=start_s,
            end_date=end_s,
            provider="yfinance",
            period="quarter",
        )
        analyst = query_analyst_ratings(con, symbol=sym, start_date=start_s, end_date=end_s, limit=120)
        analyst.sort(key=lambda r: str(r.get("asof_date") or ""))
        value_trading = query_value_trading_assessments(con, symbol=sym, limit=10)
        value_trading_latest = latest_value_trading_assessment(con, symbol=sym)
    finally:
        con.close()

    prices = _agent_price_history(sym, start_s, end_s)
    if not prices:
        try:
            from value_metrics_price_history import fetch_price_history

            live = fetch_price_history(sym, interval="1d", start=start_s, end=end_s)
            prices = [{"ts": r["ts"], "close": r["close"], "volume": r.get("volume")} for r in live]
        except Exception:
            prices = []

    return {
        "symbol": sym,
        "stock": stock_row,
        "coverage": coverage_for_symbol(vm_db, sym),
        "window": {"start": start_s, "end": end_s},
        "prices": prices,
        "metrics": metrics,
        "fundamentals": fundamentals,
        "analyst_ratings": analyst,
        "value_trading": value_trading,
        "value_trading_latest": value_trading_latest,
        "news": _agent_news_for_symbol(sym),
        "recommendations": _agent_recommendations_for_symbol(sym),
    }
