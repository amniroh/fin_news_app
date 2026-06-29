#!/usr/bin/env python3
"""
Trend-following strategy with partial position exit on the *interesting stocks* universe.

Buy (end-of-day signal, executed at close):
  RVOL > 2, ADX > 25, MACD line > signal (bullish), close > EMA(20).
  Allocate 10% of available cash per new position (one open lot per symbol).

Exit (partial position / split):
  1) Take profit at 3:1 risk-reward (risk = 1× ATR(14) below entry) — sell 50% of shares.
  2) Move stop on remainder to breakeven (entry price).
  3) Trail the remainder with a 2× ATR stop from the highest close since the partial exit.

Requires complete daily technical indicator history (vm_technical_indicators) and OHLCV
(agent.sqlite daily prices) for every interesting-stock symbol in the backtest window.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
_BACKEND = _REPO / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from sp500_return_model import (  # noqa: E402
    DATA_DIR,
    _equity_curve_payload,
    _json_default,
    _summary_metrics,
    load_prices,
    split_indices_by_time_fractions,
)

logger = logging.getLogger(__name__)

STRATEGY_ID = "trend_v0_partial_position_exit"
TREND_DIR = DATA_DIR / "trend_v0_models"
TREND_DIR.mkdir(parents=True, exist_ok=True)

RVOL_MIN = 2.0
ADX_MIN = 25.0
BUY_CASH_FRACTION = 0.10
PARTIAL_EXIT_FRACTION = 0.50
RR_RATIO = 3.0
INITIAL_STOP_ATR_MULT = 1.0
TRAILING_STOP_ATR_MULT = 2.0
ATR_PERIOD = 14
INITIAL_CASH = 100_000.0
TC_SLIPPAGE = 0.0005
REQUIRED_TECH_FIELDS = ("close", "ema", "macd_line", "macd_signal", "adx", "rvol")


@dataclass
class TrendV0Config:
    years: float = 1.0
    split_train_frac: float = 0.5
    split_val_frac: float = 0.25
    split_test_frac: float = 0.25
    benchmark: str = "SPY"
    provider: str = "yfinance"
    min_coverage_frac: float = 1.0
    allow_partial_universe: bool = False
    min_eligible_symbols: int = 50


class DataValidationError(Exception):
    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("\n".join(messages[:50]))


@dataclass
class _Position:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: float
    initial_stop: float
    profit_target: float
    phase: str
    stop_price: float
    highest_close: float
    trail_atr: float


@dataclass
class TrendV0Result:
    cadence: str
    strategy_id: str
    weighting: str
    split_train_frac: float
    split_val_frac: float
    split_test_frac: float
    n_train: int
    n_val: int
    n_test: int
    train_metrics: Dict[str, Any]
    val_metrics: Dict[str, Any]
    test_metrics: Dict[str, Any]
    baseline_train_metrics: Dict[str, Any]
    baseline_val_metrics: Dict[str, Any]
    baseline_test_metrics: Dict[str, Any]
    train_curve: List[Dict[str, Any]]
    val_curve: List[Dict[str, Any]]
    test_curve: List[Dict[str, Any]]
    baseline_train_curve: List[Dict[str, Any]]
    baseline_val_curve: List[Dict[str, Any]]
    baseline_test_curve: List[Dict[str, Any]]
    current_top: List[Dict[str, Any]]
    turnover_test: float
    trained_at: str
    universe_size: int
    history_years: float
    train_ic: float = float("nan")
    val_ic: float = float("nan")
    test_ic: float = float("nan")
    params: Dict[str, Any] = field(default_factory=dict)
    data_validation: Dict[str, Any] = field(default_factory=dict)
    walkforward_folds: List[Dict[str, Any]] = field(default_factory=list)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _vm_db_path() -> Path:
    raw = (os.getenv("VALUE_METRICS_DB_PATH") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else _REPO / p
    return _BACKEND / "data" / "value_metrics.sqlite"


def _agent_db_path() -> Path:
    raw = (os.getenv("AGENT_DB_PATH") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else _REPO / p
    return _REPO / "telegram_agent" / "data" / "agent.sqlite"


def load_interesting_symbols(vm_con: sqlite3.Connection) -> List[str]:
    from value_metrics_store import list_interesting_stocks

    return sorted(
        {str(r["symbol"]).strip().upper() for r in list_interesting_stocks(vm_con) if r.get("symbol")}
    )


def _compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = ATR_PERIOD) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def load_ohlcv_daily(agent_con: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    from technical_indicators_backfill import query_ohlcv_daily

    rows = query_ohlcv_daily(agent_con, symbol)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    for c in ("close", "high", "low", "volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_technical_history(
    vm_con: sqlite3.Connection,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    *,
    provider: str = "yfinance",
) -> pd.DataFrame:
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not syms:
        return pd.DataFrame()
    ph = ",".join("?" * len(syms))
    sql = f"""
        SELECT symbol, asof_date, close, ema, macd_line, macd_signal, adx, rvol
        FROM vm_technical_indicators
        WHERE provider = ?
          AND symbol IN ({ph})
          AND asof_date >= ?
          AND asof_date <= ?
        ORDER BY asof_date, symbol
    """
    params: List[Any] = [provider] + list(syms) + [start_date, end_date]
    cur = vm_con.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def validate_backtest_data(
    cfg: TrendV0Config,
    *,
    vm_con: Optional[sqlite3.Connection] = None,
    agent_con: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    close_vm = close_agent = False
    if vm_con is None:
        vm_con = sqlite3.connect(str(_vm_db_path()))
        vm_con.row_factory = sqlite3.Row
        close_vm = True
    if agent_con is None:
        agent_con = sqlite3.connect(str(_agent_db_path()))
        agent_con.row_factory = sqlite3.Row
        close_agent = True

    errors: List[str] = []
    try:
        symbols = load_interesting_symbols(vm_con)
        if not symbols:
            raise DataValidationError(["No interesting stocks in vm_interesting_stocks"])

        end_d = date.today()
        start_d = end_d - timedelta(days=int(cfg.years * 365.25) + 60)
        start_s = start_d.isoformat()
        end_s = end_d.isoformat()

        tech = load_technical_history(vm_con, symbols, start_s, end_s, provider=cfg.provider)
        if tech.empty:
            errors.append(
                f"No vm_technical_indicators rows for universe in [{start_s}, {end_s}]. "
                "Run: python backend/technical_indicators_backfill.py"
            )
            raise DataValidationError(errors)

        tech_idx: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for row in tech.itertuples(index=False):
            key = (str(row.symbol), str(row.asof_date)[:10])
            tech_idx[key] = {
                "close": row.close,
                "ema": row.ema,
                "macd_line": row.macd_line,
                "macd_signal": row.macd_signal,
                "adx": row.adx,
                "rvol": row.rvol,
            }

        missing_price_syms: List[str] = []
        incomplete: List[str] = []
        eligible: List[str] = []

        for sym in symbols:
            ohlcv = load_ohlcv_daily(agent_con, sym)
            if ohlcv.empty:
                missing_price_syms.append(sym)
                continue
            ohlcv = ohlcv.loc[(ohlcv.index >= pd.Timestamp(start_s)) & (ohlcv.index <= pd.Timestamp(end_s))]
            if ohlcv.empty:
                missing_price_syms.append(sym)
                continue
            n_bars = len(ohlcv)
            n_ok = 0
            for dt, _ in ohlcv.iterrows():
                dstr = dt.strftime("%Y-%m-%d")
                rec = tech_idx.get((sym, dstr))
                if rec is None:
                    continue
                if any(
                    rec[f] is None or (isinstance(rec[f], float) and not math.isfinite(rec[f]))
                    for f in REQUIRED_TECH_FIELDS
                ):
                    continue
                n_ok += 1
            cov = n_ok / n_bars if n_bars else 0.0
            if cov >= cfg.min_coverage_frac:
                eligible.append(sym)
            else:
                incomplete.append(f"{sym}: {cov:.1%} complete ({n_ok}/{n_bars} bars)")

        excluded = missing_price_syms + [s.split(":")[0] for s in incomplete]

        if not cfg.allow_partial_universe:
            if missing_price_syms:
                errors.append(
                    f"{len(missing_price_syms)} symbols missing daily OHLCV in agent DB: "
                    + ", ".join(missing_price_syms[:20])
                    + ("…" if len(missing_price_syms) > 20 else "")
                )
            if incomplete:
                errors.append(
                    f"{len(incomplete)} symbols with incomplete technical indicators "
                    f"(need {cfg.min_coverage_frac:.0%} coverage): "
                    + "; ".join(incomplete[:15])
                    + ("…" if len(incomplete) > 15 else "")
                )
            if errors:
                raise DataValidationError(errors)
            use_symbols = symbols
        else:
            use_symbols = eligible
            if len(use_symbols) < cfg.min_eligible_symbols:
                errors.append(
                    f"Only {len(use_symbols)} symbols have complete data (need >= {cfg.min_eligible_symbols}). "
                    f"Excluded {len(excluded)} symbols. Run technical_indicators_backfill or fix price gaps."
                )
                raise DataValidationError(errors)

        return {
            "symbols": use_symbols,
            "n_symbols": len(use_symbols),
            "n_excluded": len(excluded),
            "excluded_sample": excluded[:30],
            "start_date": start_s,
            "end_date": end_s,
            "n_technical_rows": len(tech),
            "provider": cfg.provider,
        }
    finally:
        if close_vm:
            vm_con.close()
        if close_agent:
            agent_con.close()


def _bullish_macd(row: Dict[str, Any]) -> bool:
    return float(row["macd_line"]) > float(row["macd_signal"])


def _buy_signal(row: Dict[str, Any]) -> bool:
    if row.get("rvol") is None or row.get("adx") is None:
        return False
    if float(row["rvol"]) <= RVOL_MIN:
        return False
    if float(row["adx"]) <= ADX_MIN:
        return False
    if not _bullish_macd(row):
        return False
    return float(row["close"]) > float(row["ema"])


def _prepare_market_data(
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    *,
    vm_con: sqlite3.Connection,
    agent_con: sqlite3.Connection,
    provider: str,
) -> Tuple[pd.DatetimeIndex, Dict[str, pd.DataFrame], Dict[Tuple[str, str], Dict[str, Any]], Dict[str, pd.Series]]:
    tech = load_technical_history(vm_con, symbols, start_date, end_date, provider=provider)
    tech_idx: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in tech.itertuples(index=False):
        key = (str(row.symbol), str(row.asof_date)[:10])
        tech_idx[key] = {f: getattr(row, f) for f in REQUIRED_TECH_FIELDS}

    ohlcv_map: Dict[str, pd.DataFrame] = {}
    atr_map: Dict[str, pd.Series] = {}
    all_dates: Set[pd.Timestamp] = set()

    for sym in symbols:
        df = load_ohlcv_daily(agent_con, sym)
        if df.empty:
            continue
        df = df.loc[(df.index >= pd.Timestamp(start_date)) & (df.index <= pd.Timestamp(end_date))]
        if df.empty:
            continue
        ohlcv_map[sym] = df
        all_dates.update(df.index)
        atr_map[sym] = _compute_atr(df["high"], df["low"], df["close"], ATR_PERIOD)

    if not all_dates:
        raise RuntimeError("no OHLCV dates in backtest window")

    return pd.DatetimeIndex(sorted(all_dates)), ohlcv_map, tech_idx, atr_map


def _simulate_period(
    calendar: pd.DatetimeIndex,
    symbols: Sequence[str],
    ohlcv_map: Dict[str, pd.DataFrame],
    tech_idx: Dict[Tuple[str, str], Dict[str, Any]],
    atr_map: Dict[str, pd.Series],
) -> Tuple[pd.Series, float]:
    cash = INITIAL_CASH
    positions: Dict[str, _Position] = {}
    equity_rows: List[Tuple[pd.Timestamp, float]] = []
    turnover = 0.0

    for dt in calendar:
        dstr = dt.strftime("%Y-%m-%d")
        closed: List[str] = []

        for sym, pos in list(positions.items()):
            if pos.phase == "closed":
                closed.append(sym)
                continue
            bar = ohlcv_map.get(sym)
            if bar is None or dt not in bar.index:
                continue
            hi = float(bar.at[dt, "high"])
            lo = float(bar.at[dt, "low"])
            cl = float(bar.at[dt, "close"])
            exit_price: Optional[float] = None
            partial_fill: Optional[float] = None

            if pos.phase == "full":
                if lo <= pos.stop_price:
                    exit_price = pos.stop_price
                elif hi >= pos.profit_target:
                    partial_fill = pos.profit_target
            elif pos.phase == "partial":
                pos.highest_close = max(pos.highest_close, cl)
                trail = pos.highest_close - pos.trail_atr * TRAILING_STOP_ATR_MULT
                pos.stop_price = max(pos.stop_price, trail)
                if lo <= pos.stop_price:
                    exit_price = pos.stop_price

            if partial_fill is not None:
                sell_sh = pos.shares * PARTIAL_EXIT_FRACTION
                cash += sell_sh * partial_fill * (1.0 - TC_SLIPPAGE)
                pos.shares -= sell_sh
                pos.phase = "partial"
                pos.stop_price = pos.entry_price
                pos.highest_close = cl
                turnover += sell_sh * partial_fill / INITIAL_CASH

            if exit_price is not None:
                cash += pos.shares * exit_price * (1.0 - TC_SLIPPAGE)
                turnover += pos.shares * exit_price / INITIAL_CASH
                pos.phase = "closed"
                closed.append(sym)

        for sym in closed:
            positions.pop(sym, None)

        for sym in symbols:
            if sym in positions:
                continue
            row = tech_idx.get((sym, dstr))
            if row is None or not _buy_signal(row):
                continue
            bar = ohlcv_map.get(sym)
            if bar is None or dt not in bar.index:
                continue
            atr_s = atr_map.get(sym)
            if atr_s is None or dt not in atr_s.index or not math.isfinite(float(atr_s.at[dt])):
                continue
            atr_v = float(atr_s.at[dt])
            px = float(bar.at[dt, "close"])
            alloc = cash * BUY_CASH_FRACTION
            if alloc < px * 0.01:
                continue
            cost = alloc * (1.0 + TC_SLIPPAGE)
            if cost > cash:
                continue
            shares = alloc / px
            cash -= cost
            risk = atr_v * INITIAL_STOP_ATR_MULT
            positions[sym] = _Position(
                symbol=sym,
                entry_date=dt,
                entry_price=px,
                shares=shares,
                initial_stop=px - risk,
                profit_target=px + RR_RATIO * risk,
                phase="full",
                stop_price=px - risk,
                highest_close=px,
                trail_atr=atr_v,
            )
            turnover += cost / INITIAL_CASH

        mtm = cash
        for sym, pos in positions.items():
            bar = ohlcv_map.get(sym)
            if bar is not None and dt in bar.index:
                mtm += pos.shares * float(bar.at[dt, "close"])
            else:
                mtm += pos.shares * pos.entry_price
        equity_rows.append((dt, mtm))

    if len(equity_rows) < 2:
        return pd.Series(dtype=float), turnover

    idx = pd.DatetimeIndex([r[0] for r in equity_rows])
    eq = pd.Series([r[1] for r in equity_rows], index=idx)
    returns = eq.pct_change().dropna()
    turnover_avg = turnover / max(1, len(calendar))
    return returns, turnover_avg


def _slice_returns(returns: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    if returns.empty:
        return returns
    return returns.loc[(returns.index >= start) & (returns.index <= end)].dropna()


def _walkforward_folds(dates: pd.DatetimeIndex, max_folds: int = 4) -> List[Dict[str, Any]]:
    ts = pd.DatetimeIndex(dates)
    if ts.tz is not None:
        ts = ts.tz_localize(None)
    periods = sorted({(int(d.year), int(d.month)) for d in ts})
    folds: List[Dict[str, Any]] = []
    for i in range(2, len(periods)):
        test_y, test_m = periods[i]
        val_y, val_m = periods[i - 1]
        test_start = pd.Timestamp(year=test_y, month=test_m, day=1)
        if test_m == 12:
            test_end = pd.Timestamp(year=test_y + 1, month=1, day=1) - pd.Timedelta(days=1)
        else:
            test_end = pd.Timestamp(year=test_y, month=test_m + 1, day=1) - pd.Timedelta(days=1)
        val_start = pd.Timestamp(year=val_y, month=val_m, day=1)
        val_end = test_start - pd.Timedelta(days=1)
        train_end = val_start - pd.Timedelta(days=1)
        is_train = ts <= train_end
        is_val = (ts >= val_start) & (ts <= val_end)
        is_test = (ts >= test_start) & (ts <= test_end)
        n_tr, n_va, n_te = int(is_train.sum()), int(is_val.sum()), int(is_test.sum())
        if n_tr < 5 or n_va < 3 or n_te < 3:
            continue
        folds.append(
            {
                "test_year": test_y,
                "test_month": test_m,
                "val_year": val_y,
                "val_month": val_m,
                "train_end": train_end.strftime("%Y-%m-%d"),
                "val_start": val_start.strftime("%Y-%m-%d"),
                "val_end": val_end.strftime("%Y-%m-%d"),
                "test_start": test_start.strftime("%Y-%m-%d"),
                "test_end": test_end.strftime("%Y-%m-%d"),
                "n_train": n_tr,
                "n_val": n_va,
                "n_test": n_te,
            }
        )
    return folds[-max_folds:]


def evaluate_trend_v0(cfg: TrendV0Config) -> TrendV0Result:
    validation = validate_backtest_data(cfg)
    symbols: List[str] = validation["symbols"]
    start_date = validation["start_date"]
    end_date = validation["end_date"]

    vm_con = sqlite3.connect(str(_vm_db_path()))
    vm_con.row_factory = sqlite3.Row
    agent_con = sqlite3.connect(str(_agent_db_path()))
    agent_con.row_factory = sqlite3.Row
    try:
        calendar, ohlcv_map, tech_idx, atr_map = _prepare_market_data(
            symbols,
            start_date,
            end_date,
            vm_con=vm_con,
            agent_con=agent_con,
            provider=cfg.provider,
        )
    finally:
        vm_con.close()
        agent_con.close()

    full_returns, _ = _simulate_period(calendar, symbols, ohlcv_map, tech_idx, atr_map)
    if full_returns.empty or len(full_returns) < 10:
        raise RuntimeError("simulation produced insufficient return series")

    tr_idx, va_idx, te_idx = split_indices_by_time_fractions(
        full_returns.index,
        train_frac=cfg.split_train_frac,
        val_frac=cfg.split_val_frac,
        test_frac=cfg.split_test_frac,
    )
    train_dates = full_returns.index[tr_idx]
    val_dates = full_returns.index[va_idx]
    test_dates = full_returns.index[te_idx]
    if len(train_dates) < 5 or len(val_dates) < 3 or len(test_dates) < 3:
        raise RuntimeError(
            f"split too small for {cfg.years}y history: train={len(train_dates)} val={len(val_dates)} test={len(test_dates)}"
        )

    train_ret = full_returns.loc[train_dates.min() : train_dates.max()]
    val_ret = full_returns.loc[val_dates.min() : val_dates.max()]
    test_ret = full_returns.loc[test_dates.min() : test_dates.max()]

    _, turnover_test = _simulate_period(test_dates, symbols, ohlcv_map, tech_idx, atr_map)

    prices = load_prices([cfg.benchmark], years=max(cfg.years + 1, 2.0), refresh=False)
    spy = prices.get(cfg.benchmark)
    if spy is None or spy.empty:
        raise RuntimeError(f"benchmark {cfg.benchmark} prices missing for baseline")

    def _baseline(seg: pd.Series) -> pd.Series:
        c = pd.to_numeric(spy["Close"], errors="coerce")
        r = c.pct_change()
        return r.loc[(r.index >= seg.index.min()) & (r.index <= seg.index.max())].dropna()

    base_train = _baseline(train_ret)
    base_val = _baseline(val_ret)
    base_test = _baseline(test_ret)

    latest = calendar[-1]
    dstr = latest.strftime("%Y-%m-%d")
    current_top: List[Dict[str, Any]] = []
    rank = 0
    for sym in sorted(symbols):
        row = tech_idx.get((sym, dstr))
        if row and _buy_signal(row):
            rank += 1
            current_top.append(
                {
                    "symbol": sym,
                    "rank": rank,
                    "weight": BUY_CASH_FRACTION,
                    "rvol": float(row["rvol"]),
                    "adx": float(row["adx"]),
                    "close": float(row["close"]),
                    "ema": float(row["ema"]),
                }
            )

    wf_folds_out: List[Dict[str, Any]] = []
    for fold in _walkforward_folds(calendar):
        tr_end = pd.Timestamp(fold["train_end"])
        va_s = pd.Timestamp(fold["val_start"])
        va_e = pd.Timestamp(fold["val_end"])
        te_s = pd.Timestamp(fold["test_start"])
        te_e = pd.Timestamp(fold["test_end"])
        sub_cal = calendar[calendar <= te_e]
        sim_ret, _ = _simulate_period(sub_cal, symbols, ohlcv_map, tech_idx, atr_map)
        te_ret = _slice_returns(sim_ret, te_s, te_e)
        va_ret = _slice_returns(sim_ret, va_s, va_e)
        tr_ret = _slice_returns(sim_ret, sub_cal.min(), tr_end)
        base_te = _baseline(te_ret) if not te_ret.empty else pd.Series(dtype=float)
        wf_folds_out.append(
            {
                "test_year": fold["test_year"],
                "val_year": fold["val_year"],
                "test_month": fold["test_month"],
                "val_month": fold["val_month"],
                "n_train": fold["n_train"],
                "n_val": fold["n_val"],
                "n_test": fold["n_test"],
                "strategies": {
                    STRATEGY_ID: {
                        "test_metrics": _summary_metrics(te_ret),
                        "val_metrics": _summary_metrics(va_ret),
                        "train_metrics": _summary_metrics(tr_ret),
                        "baseline_test_metrics": _summary_metrics(base_te),
                    }
                },
            }
        )

    params = {
        "rvol_min": RVOL_MIN,
        "adx_min": ADX_MIN,
        "buy_cash_fraction": BUY_CASH_FRACTION,
        "partial_exit_fraction": PARTIAL_EXIT_FRACTION,
        "rr_ratio": RR_RATIO,
        "initial_stop_atr_mult": INITIAL_STOP_ATR_MULT,
        "trailing_stop_atr_mult": TRAILING_STOP_ATR_MULT,
        "atr_period": ATR_PERIOD,
    }

    return TrendV0Result(
        cadence="daily",
        strategy_id=STRATEGY_ID,
        weighting="partial_exit_trend",
        split_train_frac=float(cfg.split_train_frac),
        split_val_frac=float(cfg.split_val_frac),
        split_test_frac=float(cfg.split_test_frac),
        n_train=int(len(train_ret)),
        n_val=int(len(val_ret)),
        n_test=int(len(test_ret)),
        train_metrics=_summary_metrics(train_ret),
        val_metrics=_summary_metrics(val_ret),
        test_metrics=_summary_metrics(test_ret),
        baseline_train_metrics=_summary_metrics(base_train),
        baseline_val_metrics=_summary_metrics(base_val),
        baseline_test_metrics=_summary_metrics(base_test),
        train_curve=_equity_curve_payload(train_ret),
        val_curve=_equity_curve_payload(val_ret),
        test_curve=_equity_curve_payload(test_ret),
        baseline_train_curve=_equity_curve_payload(base_train),
        baseline_val_curve=_equity_curve_payload(base_val),
        baseline_test_curve=_equity_curve_payload(base_test),
        current_top=current_top,
        turnover_test=float(turnover_test),
        trained_at=_utcnow_iso(),
        universe_size=len(symbols),
        history_years=float(cfg.years),
        params=params,
        data_validation=validation,
        walkforward_folds=wf_folds_out,
    )


def metrics_path(cadence: str = "daily") -> Path:
    return TREND_DIR / f"trend_v0_model_{cadence}_metrics.json"


def walkforward_path(cadence: str = "daily") -> Path:
    return TREND_DIR / f"walkforward_{cadence}.json"


def save_artifacts(result: TrendV0Result) -> Path:
    p = metrics_path(result.cadence)
    p.write_text(json.dumps(asdict(result), indent=2, default=_json_default), encoding="utf-8")
    wf = {
        "cadence": result.cadence,
        "strategy": STRATEGY_ID,
        "benchmark": "SPY",
        "generated_at": result.trained_at,
        "folds": result.walkforward_folds,
        "aggregate": _aggregate_walkforward(result.walkforward_folds),
    }
    walkforward_path(result.cadence).write_text(json.dumps(wf, indent=2, default=_json_default), encoding="utf-8")
    return p


def _aggregate_walkforward(folds: List[Dict[str, Any]]) -> Dict[str, Any]:
    trs: List[float] = []
    for f in folds:
        m = (f.get("strategies") or {}).get(STRATEGY_ID, {}).get("test_metrics") or {}
        v = m.get("total_return")
        if v is not None and math.isfinite(float(v)):
            trs.append(float(v))
    if not trs:
        return {}
    return {
        "test_total_return_mean": float(np.mean(trs)),
        "test_total_return_std": float(np.std(trs, ddof=1)) if len(trs) > 1 else 0.0,
        "n_folds": len(trs),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = TrendV0Config()
    try:
        validate_backtest_data(cfg)
    except DataValidationError as e:
        logger.error("Data validation failed:\n%s", e)
        sys.exit(1)
    res = evaluate_trend_v0(cfg)
    path = save_artifacts(res)
    logger.info("Saved %s", path)
    logger.info("Test total return: %.2f%%", 100 * float(res.test_metrics.get("total_return", 0)))
