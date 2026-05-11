"""
S&P 500 forward-return regression model.

Trains one model per cadence (daily / weekly / monthly) that predicts the next-period
total return for every S&P 500 name from a standard set of cross-sectional features
(momentum, reversal, realized vol, price location, MA distances, RSI, MACD,
Bollinger %B, beta vs SPY, volume z-scores). At inference time we rank predictions
and return the top-N symbols with the highest predicted forward return — those are
used as the rebalance basket for the strategy backtests and the live portfolio.

Design notes
------------
* Cadence -> forward horizon (in *trading* days):
    daily   -> 1
    weekly  -> 5
    monthly -> 21
  Sample dates: every trading day for daily, every Friday close for weekly, every
  21st trading day (≈ month-end) for monthly. This avoids overlapping-target
  leakage in non-daily cadences.
* Feature extraction is purely from auto-adjusted closes / volumes / SPY (so
  splits and dividends are handled). We deliberately keep features price/technical
  only for v1 — fundamentals from the existing store are not point-in-time clean.
* Chronological split by **calendar-time fractions** (default **50% train / 25% validation /
  25% test** over the loaded history span): earliest segment trains, middle validates,
  latest tests. We fit on `train`, score on `val`, refit on `train+val`, then evaluate on `test`.
* Training loss uses **SPY risk–aligned sample weights** (optional): rows where the stock’s
  rolling annualized vol is close to SPY’s and beta is close to 1 are up-weighted so the
  regressor emphasizes fitting returns on names that can compose an SPY-like risk sleeve
  while still minimizing squared error on realized forward log returns (mean–variance intuition:
  maximize expected return subject to staying near benchmark risk characteristics).
* Model: LightGBM regressor when available (industry-standard for tabular
  cross-sectional alpha); falls back to sklearn HistGradientBoostingRegressor.
* Backtest: long-only equal-weight top-N held until next rebalance date. SPY
  buy-and-hold is reported as the baseline over the same window. We compute total
  return, CAGR, ann. vol, Sharpe (rf=0), max drawdown, hit rate of rolling 1y
  returns, median rolling 1y return, IC (rank-correlation between predictions and
  realised forward returns) and turnover.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "backend" / "data"
PRICE_CACHE_DIR = DATA_DIR / "sp500_prices"
MODEL_DIR = DATA_DIR / "sp500_return_models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


CADENCE_HORIZON_DAYS: Dict[str, int] = {"daily": 1, "weekly": 5, "monthly": 21}
CADENCES: Tuple[str, ...] = ("daily", "weekly", "monthly")


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------


def _rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    dn = (-delta).clip(lower=0.0)
    roll_up = up.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    roll_dn = dn.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = roll_up / roll_dn.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _macd_signal(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    # Signal-relative MACD normalised by price (scale invariance).
    return (macd - sig) / close.replace(0.0, np.nan)


def _bollinger_pct_b(close: pd.Series, window: int = 20, k: float = 2.0) -> pd.Series:
    ma = close.rolling(window, min_periods=window).mean()
    sd = close.rolling(window, min_periods=window).std(ddof=0)
    upper = ma + k * sd
    lower = ma - k * sd
    return (close - lower) / (upper - lower).replace(0.0, np.nan)


def build_per_symbol_features(
    df: pd.DataFrame,
    spy_close: pd.Series,
) -> pd.DataFrame:
    """Compute time-series features for a single symbol.

    ``df`` must have a DatetimeIndex and columns ``Close`` and ``Volume``.
    Returns a DataFrame indexed by the same dates with one column per feature.
    All features are causal (use only data up to and including the row's date).
    """
    if df is None or df.empty:
        return pd.DataFrame()
    close = pd.to_numeric(df["Close"], errors="coerce")
    volume = pd.to_numeric(df.get("Volume"), errors="coerce") if "Volume" in df.columns else None
    feats: Dict[str, pd.Series] = {}

    log_ret = np.log(close / close.shift(1))
    for w in (5, 21, 63, 126, 252):
        feats[f"ret_{w}d"] = close / close.shift(w) - 1.0
    feats["ret_1d"] = close / close.shift(1) - 1.0
    feats["mom_12_1"] = close.shift(21) / close.shift(252) - 1.0  # 12m skip-1m

    for w in (21, 63, 252):
        feats[f"vol_{w}d"] = log_ret.rolling(w, min_periods=max(10, w // 2)).std(ddof=0) * math.sqrt(252)

    ma50 = close.rolling(50, min_periods=20).mean()
    ma200 = close.rolling(200, min_periods=60).mean()
    feats["dist_ma50"] = close / ma50 - 1.0
    feats["dist_ma200"] = close / ma200 - 1.0

    high_252 = close.rolling(252, min_periods=60).max()
    low_252 = close.rolling(252, min_periods=60).min()
    feats["dist_52w_high"] = close / high_252 - 1.0
    feats["range_pos_52w"] = (close - low_252) / (high_252 - low_252).replace(0.0, np.nan)

    feats["rsi_14"] = _rsi(close, 14)
    feats["macd_norm"] = _macd_signal(close)
    feats["bb_pct_b"] = _bollinger_pct_b(close)

    if volume is not None:
        log_vol = np.log(volume.replace(0.0, np.nan))
        feats["log_vol_5d"] = log_vol.rolling(5, min_periods=3).mean()
        feats["log_vol_21d"] = log_vol.rolling(21, min_periods=10).mean()
        vol_z = (log_vol - log_vol.rolling(63, min_periods=20).mean()) / log_vol.rolling(63, min_periods=20).std(ddof=0)
        feats["vol_z_63d"] = vol_z

    spy_log_ret = np.log(spy_close / spy_close.shift(1))
    spy_aligned = spy_log_ret.reindex(close.index)
    win = 252
    cov = log_ret.rolling(win, min_periods=60).cov(spy_aligned)
    var_b = spy_aligned.rolling(win, min_periods=60).var(ddof=0)
    feats["beta_252d"] = cov / var_b.replace(0.0, np.nan)
    feats["idio_vol_252d"] = (log_ret - feats["beta_252d"] * spy_aligned).rolling(win, min_periods=60).std(ddof=0) * math.sqrt(252)

    out = pd.DataFrame(feats, index=close.index)
    return out


# ---------------------------------------------------------------------------
# Price loading & caching
# ---------------------------------------------------------------------------


def _yahoo_symbol(sym: str) -> str:
    s = (sym or "").strip().upper().replace(" ", "")
    if "." in s and len(s) <= 8:
        s = s.replace(".", "-")
    return s


def _cache_path(symbol: str) -> Path:
    return PRICE_CACHE_DIR / f"{_yahoo_symbol(symbol)}.parquet"


def _load_cached(symbol: str) -> Optional[pd.DataFrame]:
    p = _cache_path(symbol)
    if not p.is_file():
        return None
    try:
        df = pd.read_parquet(p)
        if df.empty or "Close" not in df.columns:
            return None
        df.index = pd.DatetimeIndex(df.index).tz_localize(None) if df.index.tz is not None else pd.DatetimeIndex(df.index)
        return df.sort_index()
    except Exception:
        return None


def _save_cache(symbol: str, df: pd.DataFrame) -> None:
    p = _cache_path(symbol)
    try:
        out = df.copy()
        out.index = pd.DatetimeIndex(out.index)
        out.to_parquet(p)
    except Exception:
        try:
            csv_path = p.with_suffix(".csv")
            df.to_csv(csv_path)
        except Exception:
            pass


def _download_batch(symbols: Sequence[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
    """Download adjusted OHLCV for a batch of symbols via yfinance."""
    import yfinance as yf  # local import: keeps module importable for tests/UI

    syms = [_yahoo_symbol(s) for s in symbols]
    if not syms:
        return {}
    px = yf.download(
        syms,
        start=start,
        end=end,
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
        group_by="ticker",
    )
    out: Dict[str, pd.DataFrame] = {}
    if px is None or px.empty:
        return out
    if isinstance(px.columns, pd.MultiIndex):
        for s in syms:
            try:
                sub = px[s].dropna(how="all")
            except Exception:
                continue
            if not sub.empty:
                sub = sub.copy()
                sub.index = pd.DatetimeIndex(sub.index).tz_localize(None) if sub.index.tz is not None else pd.DatetimeIndex(sub.index)
                out[s] = sub
    else:
        if len(syms) == 1:
            sub = px.dropna(how="all").copy()
            sub.index = pd.DatetimeIndex(sub.index).tz_localize(None) if sub.index.tz is not None else pd.DatetimeIndex(sub.index)
            out[syms[0]] = sub
    return out


def load_prices(
    symbols: Sequence[str],
    *,
    years: float = 10.0,
    refresh: bool = False,
    batch_size: int = 80,
    sleep_s: float = 0.4,
) -> Dict[str, pd.DataFrame]:
    """Load adjusted daily OHLCV for ``symbols``, caching on disk in parquet."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(years * 365.25) + 30)
    end_iso = (end + timedelta(days=1)).isoformat()
    start_iso = start.isoformat()
    out: Dict[str, pd.DataFrame] = {}
    todo: List[str] = []
    syms = [_yahoo_symbol(s) for s in symbols]
    # If the cache doesn't span the requested window we MUST re-download.
    earliest_required = pd.Timestamp(start) + pd.Timedelta(days=14)
    for s in syms:
        if not refresh:
            cached = _load_cached(s)
            if cached is not None and not cached.empty:
                last = pd.Timestamp(cached.index.max())
                first = pd.Timestamp(cached.index.min())
                fresh = last.date() >= end - timedelta(days=4)
                deep_enough = first <= earliest_required
                if fresh and deep_enough:
                    out[s] = cached
                    continue
        todo.append(s)
    if todo:
        for i in range(0, len(todo), batch_size):
            batch = todo[i : i + batch_size]
            try:
                got = _download_batch(batch, start_iso, end_iso)
            except Exception as e:
                logger.warning("price batch failed (%d-%d): %s", i, i + len(batch), e)
                got = {}
            for sym, df in got.items():
                if df is None or df.empty or "Close" not in df.columns:
                    continue
                _save_cache(sym, df)
                out[sym] = df
            time.sleep(sleep_s)
            print(f"  downloaded prices {min(i + batch_size, len(todo))}/{len(todo)}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Dataset assembly
# ---------------------------------------------------------------------------


@dataclass
class Dataset:
    X: pd.DataFrame  # rows indexed by integer position; columns = features
    y: pd.Series  # forward log return aligned with X (same row order)
    dates: pd.DatetimeIndex  # length == len(X)
    symbols: np.ndarray  # length == len(X), dtype object/str
    feature_names: List[str]
    # Positive weights for weighted MSE / gradient boosting; emphasizes SPY-like risk rows.
    spy_risk_sample_weight: Optional[np.ndarray] = None


def _sample_dates(all_dates: pd.DatetimeIndex, cadence: str) -> pd.DatetimeIndex:
    if cadence == "daily":
        return all_dates
    if cadence == "weekly":
        # one observation per week — Friday close, or last trading day of the week
        s = pd.Series(all_dates, index=all_dates)
        return pd.DatetimeIndex(s.groupby(s.dt.to_period("W")).max().values)
    if cadence == "monthly":
        s = pd.Series(all_dates, index=all_dates)
        return pd.DatetimeIndex(s.groupby(s.dt.to_period("M")).max().values)
    raise ValueError(f"unknown cadence {cadence!r}")


def build_dataset(
    prices: Dict[str, pd.DataFrame],
    *,
    cadence: str,
    benchmark: str = "SPY",
    min_history_days: int = 260,
    spy_risk_align_kappa_vol: float = 2.0,
    spy_risk_align_kappa_beta: float = 1.0,
) -> Dataset:
    """Materialise the (date, symbol) feature/target table for one cadence."""
    horizon = CADENCE_HORIZON_DAYS[cadence]
    spy = prices.get(_yahoo_symbol(benchmark))
    if spy is None or spy.empty:
        raise RuntimeError(f"benchmark {benchmark!r} prices not available")
    spy_close = pd.to_numeric(spy["Close"], errors="coerce")
    spy_log_ret = np.log(spy_close / spy_close.shift(1))
    spy_ann_vol = spy_log_ret.rolling(252, min_periods=60).std(ddof=0) * math.sqrt(252)

    all_dates: List[pd.Timestamp] = []
    feat_frames: List[pd.DataFrame] = []
    target_series: List[pd.Series] = []
    sym_index: List[str] = []

    for sym, df in prices.items():
        if sym == _yahoo_symbol(benchmark):
            continue
        if df is None or df.empty or len(df) < min_history_days:
            continue
        feats = build_per_symbol_features(df, spy_close)
        if feats.empty:
            continue
        close = pd.to_numeric(df["Close"], errors="coerce")
        # forward log return over the cadence horizon
        fwd = np.log(close.shift(-horizon) / close)
        # sample dates per cadence (intersect with this symbol's dates)
        sample = _sample_dates(feats.index, cadence)
        sample = sample.intersection(feats.index).intersection(fwd.dropna().index)
        if len(sample) == 0:
            continue
        feat_sample = feats.loc[sample].dropna(how="any")
        sample = feat_sample.index
        if len(sample) == 0:
            continue
        target_sample = fwd.loc[sample]
        feat_sample = feat_sample.copy()
        feat_sample["__sym__"] = sym
        feat_frames.append(feat_sample)
        target_series.append(target_sample)
        sym_index.extend([sym] * len(sample))
        all_dates.extend(sample.tolist())

    if not feat_frames:
        raise RuntimeError("no usable data after feature extraction")

    X = pd.concat(feat_frames, axis=0)
    X.index.name = "date"
    y = pd.concat(target_series, axis=0)
    y.index.name = "date"

    # SPY risk–aligned weights: favor stocks whose trailing vol matches SPY and beta≈1
    # (soft constraint toward benchmark risk while fitting forward returns).
    spy_vol_row = spy_ann_vol.reindex(X.index)
    stock_vol = pd.to_numeric(X["vol_252d"], errors="coerce")
    stock_beta = pd.to_numeric(X["beta_252d"], errors="coerce")
    denom = spy_vol_row.replace(0.0, np.nan).fillna(spy_vol_row.median())
    vol_gap_sq = ((stock_vol / (denom + 1e-9)) - 1.0) ** 2
    beta_gap_sq = (stock_beta - 1.0) ** 2
    vol_gap_sq = pd.Series(vol_gap_sq).fillna(1.0)
    beta_gap_sq = pd.Series(beta_gap_sq).fillna(1.0)
    kv = float(spy_risk_align_kappa_vol)
    kb = float(spy_risk_align_kappa_beta)
    if kv <= 0.0 and kb <= 0.0:
        risk_w = np.ones(len(X), dtype=float)
    else:
        risk_w = np.exp(-kv * vol_gap_sq.to_numpy(dtype=float)) * np.exp(-kb * beta_gap_sq.to_numpy(dtype=float))
        risk_w = np.nan_to_num(risk_w, nan=1.0, posinf=1.0, neginf=1.0)
        risk_w = np.clip(risk_w, 1e-6, None)

    feature_cols = [c for c in X.columns if c != "__sym__"]
    # Cross-sectional ranking: per date, percentile-rank each feature in (0, 1).
    X_ranked = (
        X.assign(__date__=X.index)
        .groupby("__date__")[feature_cols]
        .rank(pct=True, method="average")
    )
    # Reset to a clean integer index; align dates/symbols/y in arrays of the same length.
    dates_arr = pd.DatetimeIndex(X.index.values)
    syms_arr = X["__sym__"].astype(str).values
    X_feat = X_ranked.reset_index(drop=True)[feature_cols].astype(float)
    y_arr = y.reset_index(drop=True)
    return Dataset(
        X=X_feat,
        y=y_arr,
        dates=dates_arr,
        symbols=syms_arr,
        feature_names=feature_cols,
        spy_risk_sample_weight=risk_w,
    )


# ---------------------------------------------------------------------------
# Modelling
# ---------------------------------------------------------------------------


def _build_estimator(seed: int = 7):
    """Prefer LightGBM; fall back to sklearn."""
    try:
        import lightgbm as lgb  # type: ignore

        return lgb.LGBMRegressor(
            n_estimators=600,
            learning_rate=0.03,
            num_leaves=63,
            min_data_in_leaf=200,
            feature_fraction=0.85,
            bagging_fraction=0.85,
            bagging_freq=5,
            reg_lambda=1.0,
            random_state=seed,
            n_jobs=-1,
            verbosity=-1,
        )
    except Exception:
        from sklearn.ensemble import HistGradientBoostingRegressor

        return HistGradientBoostingRegressor(
            max_iter=500,
            learning_rate=0.05,
            max_leaf_nodes=63,
            min_samples_leaf=200,
            l2_regularization=1.0,
            random_state=seed,
        )


def _spearman_ic(pred: pd.Series, actual: pd.Series, dates: pd.DatetimeIndex) -> float:
    """Cross-sectional rank IC averaged across dates (standard quant metric)."""
    df = pd.DataFrame({"d": dates, "p": pred.values, "y": actual.values}).dropna()
    if df.empty:
        return float("nan")
    ic_vals: List[float] = []
    for _, g in df.groupby("d"):
        if len(g) < 5:
            continue
        rp = g["p"].rank(method="average")
        ry = g["y"].rank(method="average")
        if rp.std(ddof=0) < 1e-12 or ry.std(ddof=0) < 1e-12:
            continue
        ic_vals.append(float(rp.corr(ry)))
    return float(np.mean(ic_vals)) if ic_vals else float("nan")


# ---------------------------------------------------------------------------
# Backtesting
# ---------------------------------------------------------------------------


def _build_predictions_panel(
    dataset: Dataset,
    pred: np.ndarray,
) -> pd.DataFrame:
    """Return a DataFrame with columns [date, symbol, pred, y]."""
    return pd.DataFrame(
        {
            "date": dataset.dates,
            "symbol": dataset.symbols,
            "pred": pred,
            "y": dataset.y.values,
        }
    )


def _compute_basket_weights(scores: np.ndarray, *, weighting: str) -> np.ndarray:
    """Per-rebalance weights for a top-N basket.

    Conventions:
      - ``weighting='equal'``      → 1/N each.
      - ``weighting='score_weighted'`` → linear in score (predicted profit). We shift to
        non-negative before normalising so a basket with mixed-sign predictions still
        produces a long-only allocation that emphasises the highest expected return.
    """
    n = len(scores)
    if n == 0:
        return scores
    if weighting == "equal":
        return np.ones(n) / n
    if weighting == "score_weighted":
        s = np.asarray(scores, dtype=float)
        shifted = s - np.min(s) + 1e-6
        total = float(np.sum(shifted))
        if not np.isfinite(total) or total <= 0.0:
            return np.ones(n) / n
        return shifted / total
    raise ValueError(f"unknown weighting {weighting!r}")


def _run_strategy_backtest(
    panel: pd.DataFrame,
    prices: Dict[str, pd.DataFrame],
    *,
    cadence: str,
    top_n: int,
    weighting: str = "equal",
    score_col: str = "pred",
) -> Dict[str, Any]:
    """Long-only top-N backtest rebalanced at each cadence date.

    ``score_col`` is the column in ``panel`` used to rank symbols (higher = buy).
    ``weighting`` is forwarded to :func:`_compute_basket_weights`.
    """
    horizon = CADENCE_HORIZON_DAYS[cadence]
    use_syms = set(panel["symbol"].unique())
    closes = {}
    for s, df in prices.items():
        if s in use_syms:
            closes[s] = pd.to_numeric(df["Close"], errors="coerce")
    if not closes:
        raise RuntimeError("no prices for symbols in predictions panel")
    close_df = pd.concat(closes, axis=1).sort_index()

    rebal_dates = pd.DatetimeIndex(sorted(panel["date"].unique()))
    if len(rebal_dates) < 2:
        raise RuntimeError("not enough rebalance dates for backtest")

    daily_returns = close_df.pct_change()
    port_rets: List[pd.Series] = []
    turnover_vals: List[float] = []
    prev_basket: Optional[set] = None
    for i, dt in enumerate(rebal_dates):
        block = panel[panel["date"] == dt].sort_values(score_col, ascending=False)
        block = block[block["symbol"].isin(close_df.columns)]
        if block.empty:
            continue
        head = block.head(top_n)
        basket = head["symbol"].tolist()
        scores = head[score_col].values.astype(float)
        weights = _compute_basket_weights(scores, weighting=weighting)
        if prev_basket is None:
            turnover_vals.append(1.0)
        else:
            inter = len(set(basket) & prev_basket)
            turnover_vals.append(1.0 - inter / max(1, top_n))
        prev_basket = set(basket)
        next_dt = rebal_dates[i + 1] if i + 1 < len(rebal_dates) else None
        idx_mask = (daily_returns.index > dt)
        if next_dt is not None:
            idx_mask &= (daily_returns.index <= next_dt)
        else:
            cap = daily_returns.index[daily_returns.index > dt][: horizon]
            idx_mask = daily_returns.index.isin(cap)
        sub = daily_returns.loc[idx_mask, basket].fillna(0.0)
        if sub.empty:
            continue
        # Weighted portfolio return: sum_i w_i * r_i,t
        port = (sub.values * weights[None, :]).sum(axis=1)
        port_rets.append(pd.Series(port, index=sub.index))
    if not port_rets:
        raise RuntimeError("backtest produced no return observations")
    series = pd.concat(port_rets).sort_index()
    series.name = "ret"
    return {
        "returns": series,
        "rebal_dates": rebal_dates,
        "turnover_avg": float(np.mean(turnover_vals)) if turnover_vals else float("nan"),
        "weighting": weighting,
    }


def _spy_baseline_returns(prices: Dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    spy = prices.get("SPY")
    if spy is None:
        raise RuntimeError("SPY prices missing for baseline")
    c = pd.to_numeric(spy["Close"], errors="coerce")
    r = c.pct_change()
    return r.loc[(r.index >= start) & (r.index <= end)].dropna()


def _summary_metrics(returns: pd.Series, rolling_window_days: int = 252) -> Dict[str, Any]:
    """Compute the metrics we report to the user (validation/test/baseline)."""
    if returns is None or returns.empty:
        return {}
    eq = (1.0 + returns).cumprod()
    total_return = float(eq.iloc[-1] - 1.0)
    days = (returns.index[-1] - returns.index[0]).days
    years = max(days / 365.25, 1e-6)
    cagr = float(eq.iloc[-1] ** (1.0 / years) - 1.0) if eq.iloc[-1] > 0 else float("nan")
    ann_vol = float(returns.std(ddof=1) * math.sqrt(252)) if len(returns) > 2 else float("nan")
    sharpe = float(returns.mean() / returns.std(ddof=1) * math.sqrt(252)) if returns.std(ddof=1) > 0 else float("nan")
    drawdown = float((eq / eq.cummax() - 1.0).min())
    # Rolling 1y returns
    if len(eq) >= rolling_window_days + 1:
        roll = eq.pct_change(rolling_window_days).dropna()
        med_roll = float(roll.median())
        hit_roll = float((roll > 0).mean())
    else:
        med_roll = float("nan")
        hit_roll = float("nan")
    return {
        "total_return": total_return,
        "cagr": cagr,
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "max_drawdown": drawdown,
        "rolling_1y_median_return": med_roll,
        "rolling_1y_hit_rate": hit_roll,
        "n_days": int(len(returns)),
        "start": returns.index[0].isoformat(),
        "end": returns.index[-1].isoformat(),
    }


def _equity_curve_payload(returns: pd.Series, sample: int = 400) -> List[Dict[str, Any]]:
    if returns is None or returns.empty:
        return []
    eq = (1.0 + returns).cumprod()
    if len(eq) > sample:
        step = max(1, len(eq) // sample)
        eq = eq.iloc[::step]
    return [{"date": pd.Timestamp(idx).date().isoformat(), "equity": float(v)} for idx, v in eq.items()]


# ---------------------------------------------------------------------------
# Top-level training entry point
# ---------------------------------------------------------------------------


@dataclass
class TrainConfig:
    cadence: str
    symbols: List[str]
    years: float = 30.0
    """Fractions of the loaded date span: train / validation (dev) / test."""
    split_train_frac: float = 0.5
    split_val_frac: float = 0.25
    split_test_frac: float = 0.25
    """Weighted MSE toward SPY-like risk (rolling vol ratio ~1, beta ~1). Set both to 0 to disable."""
    spy_risk_align_kappa_vol: float = 2.0
    spy_risk_align_kappa_beta: float = 1.0
    top_n: int = 50
    benchmark: str = "SPY"
    seed: int = 7
    refresh_prices: bool = False
    # Which weighting variants to backtest+save. The model itself is the same; only the
    # portfolio construction differs. Each variant gets its own metrics JSON.
    weightings: Tuple[str, ...] = ("equal", "score_weighted")


@dataclass
class TrainResult:
    cadence: str
    strategy_id: str
    weighting: str
    n_train: int
    n_val: int
    n_test: int
    split_train_frac: float
    split_val_frac: float
    split_test_frac: float
    spy_risk_align_kappa_vol: float
    spy_risk_align_kappa_beta: float
    feature_names: List[str]
    val_metrics: Dict[str, Any]
    test_metrics: Dict[str, Any]
    train_metrics: Dict[str, Any]
    baseline_val_metrics: Dict[str, Any]
    baseline_test_metrics: Dict[str, Any]
    baseline_train_metrics: Dict[str, Any]
    train_ic: float
    val_ic: float
    test_ic: float
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


def split_indices_by_time_fractions(
    dates: pd.DatetimeIndex | np.ndarray,
    *,
    train_frac: float,
    val_frac: float,
    test_frac: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Chronological row indices for train / validation / test by calendar-time fractions."""
    tf, vf, sf = float(train_frac), float(val_frac), float(test_frac)
    if abs(tf + vf + sf - 1.0) > 1e-5:
        raise ValueError(f"split fractions must sum to 1, got {tf + vf + sf}")
    ts = pd.DatetimeIndex(dates)
    d0 = pd.Timestamp(ts.min()).normalize()
    d1 = pd.Timestamp(ts.max()).normalize()
    span_days = max(1, int((d1 - d0).days))
    train_end = d0 + pd.Timedelta(days=int(tf * span_days))
    val_end = d0 + pd.Timedelta(days=int((tf + vf) * span_days))
    is_train = ts < train_end
    is_val = (ts >= train_end) & (ts < val_end)
    is_test = ts >= val_end
    if not is_train.any() or not is_val.any() or not is_test.any():
        raise RuntimeError(
            f"split produced empty segment: train={int(is_train.sum())} val={int(is_val.sum())} "
            f"test={int(is_test.sum())} (span_days={span_days})"
        )
    return np.where(is_train)[0], np.where(is_val)[0], np.where(is_test)[0]


_STRATEGY_ID_BY_WEIGHTING = {
    "equal": "ml_equal",
    "score_weighted": "ml_pred_weighted",
}


def train_one_cadence(
    cfg: TrainConfig,
    prices: Optional[Dict[str, pd.DataFrame]] = None,
) -> Tuple[List[TrainResult], Any]:
    """Train the regression model for a cadence and produce one TrainResult per weighting.

    The model is fit once; backtests are then re-run for each weighting in
    ``cfg.weightings`` so callers can compare equal-weight vs predicted-return-weighted
    portfolios side-by-side. Returns the list of results (one per weighting, in input
    order) and the refit-on-train+val model.
    """
    if cfg.cadence not in CADENCE_HORIZON_DAYS:
        raise ValueError(f"unknown cadence {cfg.cadence!r}")
    if prices is None:
        prices = load_prices(
            list(set(cfg.symbols) | {cfg.benchmark}),
            years=cfg.years,
            refresh=cfg.refresh_prices,
        )
    if cfg.benchmark not in prices:
        raise RuntimeError(f"benchmark {cfg.benchmark!r} not loaded")

    ds = build_dataset(
        prices,
        cadence=cfg.cadence,
        benchmark=cfg.benchmark,
        spy_risk_align_kappa_vol=cfg.spy_risk_align_kappa_vol,
        spy_risk_align_kappa_beta=cfg.spy_risk_align_kappa_beta,
    )
    tr_idx, va_idx, te_idx = split_indices_by_time_fractions(
        ds.dates,
        train_frac=cfg.split_train_frac,
        val_frac=cfg.split_val_frac,
        test_frac=cfg.split_test_frac,
    )
    if len(tr_idx) == 0 or len(va_idx) == 0 or len(te_idx) == 0:
        raise RuntimeError(
            f"split too small: train={len(tr_idx)} val={len(va_idx)} test={len(te_idx)}"
        )

    Xtr, ytr = ds.X.iloc[tr_idx], ds.y.iloc[tr_idx]
    Xva, yva = ds.X.iloc[va_idx], ds.y.iloc[va_idx]
    Xte, yte = ds.X.iloc[te_idx], ds.y.iloc[te_idx]

    rw = ds.spy_risk_sample_weight
    if rw is None:
        fit_kw_tr: Dict[str, Any] = {}
        fit_kw_tv: Dict[str, Any] = {}
    else:
        sw_tr = rw[tr_idx].astype(float)
        sw_tr = sw_tr / max(float(np.mean(sw_tr)), 1e-12)
        idx_tv = np.concatenate([tr_idx, va_idx])
        sw_tv = rw[idx_tv].astype(float)
        sw_tv = sw_tv / max(float(np.mean(sw_tv)), 1e-12)
        fit_kw_tr = {"sample_weight": sw_tr}
        fit_kw_tv = {"sample_weight": sw_tv}

    model = _build_estimator(seed=cfg.seed)
    model.fit(Xtr.values, ytr.values, **fit_kw_tr)
    ptr = model.predict(Xtr.values)
    train_panel = pd.DataFrame(
        {
            "date": ds.dates[tr_idx],
            "symbol": ds.symbols[tr_idx],
            "pred": ptr,
            "y": ytr.values,
        }
    )
    train_ic = _spearman_ic(train_panel["pred"], train_panel["y"], pd.DatetimeIndex(train_panel["date"]))
    pva = model.predict(Xva.values)
    val_panel = pd.DataFrame(
        {
            "date": ds.dates[va_idx],
            "symbol": ds.symbols[va_idx],
            "pred": pva,
            "y": yva.values,
        }
    )
    val_ic = _spearman_ic(val_panel["pred"], val_panel["y"], pd.DatetimeIndex(val_panel["date"]))

    # Refit on train+val before scoring test.
    Xtv = pd.concat([Xtr, Xva], axis=0)
    ytv = pd.concat([ytr, yva], axis=0)
    model_full = _build_estimator(seed=cfg.seed)
    model_full.fit(Xtv.values, ytv.values, **fit_kw_tv)
    pte = model_full.predict(Xte.values)
    test_panel = pd.DataFrame(
        {
            "date": ds.dates[te_idx],
            "symbol": ds.symbols[te_idx],
            "pred": pte,
            "y": yte.values,
        }
    )
    test_ic = _spearman_ic(test_panel["pred"], test_panel["y"], pd.DatetimeIndex(test_panel["date"]))

    # Latest snapshot — used for the "current portfolio" per weighting.
    latest_dt = ds.dates.max()
    latest_idx = np.where(ds.dates == latest_dt)[0]
    latest_X = ds.X.iloc[latest_idx]
    latest_syms = ds.symbols[latest_idx]
    latest_pred = model_full.predict(latest_X.values)
    order = np.argsort(latest_pred)[::-1]

    universe_size = int(len([s for s in prices.keys() if s != cfg.benchmark]))

    results: List[TrainResult] = []
    for weighting in cfg.weightings:
        try:
            train_bt = _run_strategy_backtest(train_panel, prices, cadence=cfg.cadence, top_n=cfg.top_n, weighting=weighting)
            val_bt = _run_strategy_backtest(val_panel, prices, cadence=cfg.cadence, top_n=cfg.top_n, weighting=weighting)
            test_bt = _run_strategy_backtest(test_panel, prices, cadence=cfg.cadence, top_n=cfg.top_n, weighting=weighting)
        except Exception as e:
            logger.warning("backtest failed for weighting=%s: %s", weighting, e)
            continue

        train_metrics = _summary_metrics(train_bt["returns"])
        train_metrics["ic"] = train_ic
        train_metrics["turnover_avg"] = train_bt["turnover_avg"]
        val_metrics = _summary_metrics(val_bt["returns"])
        val_metrics["ic"] = val_ic
        val_metrics["turnover_avg"] = val_bt["turnover_avg"]
        test_metrics = _summary_metrics(test_bt["returns"])
        test_metrics["ic"] = test_ic
        test_metrics["turnover_avg"] = test_bt["turnover_avg"]

        base_train = _spy_baseline_returns(prices, train_bt["returns"].index[0], train_bt["returns"].index[-1])
        base_val = _spy_baseline_returns(prices, val_bt["returns"].index[0], val_bt["returns"].index[-1])
        base_test = _spy_baseline_returns(prices, test_bt["returns"].index[0], test_bt["returns"].index[-1])

        # Current top with the relevant weights so the snapshot is self-explanatory.
        head_pred = latest_pred[order[: cfg.top_n]]
        head_syms = latest_syms[order[: cfg.top_n]]
        weights = _compute_basket_weights(head_pred, weighting=weighting)
        current_top = [
            {
                "symbol": str(head_syms[k]),
                "predicted_return": float(head_pred[k]),
                "rank": k + 1,
                "weight": float(weights[k]),
            }
            for k in range(len(head_syms))
        ]

        results.append(
            TrainResult(
                cadence=cfg.cadence,
                strategy_id=_STRATEGY_ID_BY_WEIGHTING.get(weighting, f"ml_{weighting}"),
                weighting=weighting,
                n_train=int(len(tr_idx)),
                n_val=int(len(va_idx)),
                n_test=int(len(te_idx)),
                split_train_frac=float(cfg.split_train_frac),
                split_val_frac=float(cfg.split_val_frac),
                split_test_frac=float(cfg.split_test_frac),
                spy_risk_align_kappa_vol=float(cfg.spy_risk_align_kappa_vol),
                spy_risk_align_kappa_beta=float(cfg.spy_risk_align_kappa_beta),
                feature_names=list(ds.feature_names),
                train_metrics=train_metrics,
                val_metrics=val_metrics,
                test_metrics=test_metrics,
                baseline_train_metrics=_summary_metrics(base_train),
                baseline_val_metrics=_summary_metrics(base_val),
                baseline_test_metrics=_summary_metrics(base_test),
                train_ic=float(train_ic) if train_ic == train_ic else float("nan"),
                val_ic=float(val_ic) if val_ic == val_ic else float("nan"),
                test_ic=float(test_ic) if test_ic == test_ic else float("nan"),
                train_curve=_equity_curve_payload(train_bt["returns"]),
                val_curve=_equity_curve_payload(val_bt["returns"]),
                test_curve=_equity_curve_payload(test_bt["returns"]),
                baseline_train_curve=_equity_curve_payload(base_train),
                baseline_val_curve=_equity_curve_payload(base_val),
                baseline_test_curve=_equity_curve_payload(base_test),
                current_top=current_top,
                turnover_test=float(test_bt["turnover_avg"]),
                trained_at=datetime.now(timezone.utc).isoformat(),
                universe_size=universe_size,
                history_years=float(cfg.years),
            )
        )

    if not results:
        raise RuntimeError("training produced no successful weighting variants")
    return results, model_full


def model_artifact_paths(cadence: str, *, weighting: str = "equal") -> Tuple[Path, Path]:
    """Return ``(model_path, metrics_path)``.

    The model file is shared across weightings (same fitted estimator); only the
    metrics file changes. ``weighting='equal'`` keeps the original filename for
    backwards compatibility.
    """
    model_path = MODEL_DIR / f"sp500_return_model_{cadence}.joblib"
    if weighting == "equal":
        metrics_path = MODEL_DIR / f"sp500_return_model_{cadence}_metrics.json"
    else:
        metrics_path = MODEL_DIR / f"sp500_return_model_{cadence}_{weighting}_metrics.json"
    return model_path, metrics_path


def save_artifacts(results: Sequence[TrainResult], model: Any) -> List[Path]:
    """Save the shared model + one metrics JSON per TrainResult. Returns all paths written."""
    import joblib

    if not results:
        raise ValueError("no results to save")
    cadence = results[0].cadence
    feat_names = results[0].feature_names
    model_path = MODEL_DIR / f"sp500_return_model_{cadence}.joblib"
    joblib.dump({"model": model, "feature_names": feat_names}, model_path)
    written: List[Path] = [model_path]
    for r in results:
        _, metrics_path = model_artifact_paths(r.cadence, weighting=r.weighting)
        metrics_path.write_text(json.dumps(asdict(r), indent=2, default=_json_default), encoding="utf-8")
        written.append(metrics_path)
    return written


def load_artifacts(cadence: str, *, weighting: str = "equal") -> Tuple[Any, Dict[str, Any]]:
    import joblib

    model_path, metrics_path = model_artifact_paths(cadence, weighting=weighting)
    if not model_path.is_file():
        raise FileNotFoundError(f"no trained model for cadence {cadence!r}: {model_path}")
    bundle = joblib.load(model_path)
    metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.is_file() else {}
    return bundle, metrics


def predict_top_n(
    cadence: str,
    n: int = 50,
    *,
    weighting: str = "equal",
    prices: Optional[Dict[str, pd.DataFrame]] = None,
) -> List[Dict[str, Any]]:
    """Score the most recent feature row per symbol and return the top-N picks.

    When ``weighting='score_weighted'`` the result also carries the linear
    predicted-return weights (sum to 1) so the caller can size the basket.
    """
    bundle, _ = load_artifacts(cadence)
    model = bundle["model"]
    feat_names = bundle["feature_names"]

    symbols = [p.stem for p in PRICE_CACHE_DIR.glob("*.parquet")]
    if not symbols:
        raise RuntimeError("no cached prices; train the model or download prices first")
    if prices is None:
        prices = load_prices(symbols + ["SPY"], refresh=False)

    spy = prices.get("SPY")
    if spy is None or spy.empty:
        raise RuntimeError("SPY prices missing")
    spy_close = pd.to_numeric(spy["Close"], errors="coerce")

    rows = []
    for sym, df in prices.items():
        if sym == "SPY" or df is None or df.empty:
            continue
        feats = build_per_symbol_features(df, spy_close).dropna(how="any")
        if feats.empty:
            continue
        last = feats.iloc[[-1]]
        last["__sym__"] = sym
        rows.append(last)
    if not rows:
        raise RuntimeError("no rows to predict on")
    latest = pd.concat(rows, axis=0)
    feat_cols = [c for c in latest.columns if c != "__sym__"]
    ranked = latest[feat_cols].rank(pct=True, method="average").reindex(columns=feat_names)
    pred = model.predict(ranked.values)
    df = (
        pd.DataFrame({"symbol": latest["__sym__"].values, "pred": pred})
        .sort_values("pred", ascending=False)
        .head(int(n))
        .reset_index(drop=True)
    )
    weights = _compute_basket_weights(df["pred"].values.astype(float), weighting=weighting)
    return [
        {
            "symbol": str(row.symbol),
            "predicted_return": float(row.pred),
            "rank": int(i + 1),
            "weight": float(weights[i]),
        }
        for i, row in enumerate(df.itertuples(index=False))
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.floating, np.integer)):
        return float(o) if isinstance(o, np.floating) else int(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    raise TypeError(type(o))
