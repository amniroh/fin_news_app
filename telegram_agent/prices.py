"""Fetch OHLCV via yfinance and store in agent DB."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from telegram_agent.agent_db import (
    INTRADAY_TABLE_BY_YF_INTERVAL,
    connect,
    get_latest_intraday_ts,
    get_latest_price_ts,
    init_db,
    list_mentioned_symbols,
    upsert_intraday_rows,
    upsert_price_rows,
)
from telegram_agent.symbol_universe import symbol_universe_set, universe_priority_map

logger = logging.getLogger(__name__)


def _yf_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    # Common upstream formats (news / social) may prefix tickers with $ or #.
    while s.startswith("$") or s.startswith("#"):
        s = s[1:]
    if s.endswith("-USD") and not s.startswith("^"):
        # yfinance crypto: BTC-USD
        return s
    # yfinance uses dashes for share-class tickers (e.g. BRK.B -> BRK-B)
    if "." in s and len(s) <= 8:
        s = s.replace(".", "-")
    return s


def _universe_entry_ticker_type(entry: dict) -> tuple[str, str]:
    t = str(entry.get("ticker") or entry.get("symbol") or "").strip()
    typ = str(entry.get("type") or "").strip().lower()
    return (t, typ)


def _load_typed_universe_from_path(path: Path) -> Optional[List[Tuple[str, str]]]:
    """
    Returns list[(ticker, type)] if JSON looks like a typed universe,
    otherwise None (caller can fall back to symbol_universe_set()).
    """
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, list):
            return None
        out: List[Tuple[str, str]] = []
        for x in data:
            if not isinstance(x, dict):
                return None
            ticker, typ = _universe_entry_ticker_type(x)
            if not ticker:
                continue
            out.append((ticker, typ))
        return out or None
    except Exception:
        return None


def _price_fetch_symbol(canonical: str, typ: str) -> str:
    """
    Map canonical symbols to Yahoo Finance tickers for fetching.
    We still store prices under the canonical symbol in our DB.
    """
    c = canonical.strip().upper()
    typ = (typ or "").strip().lower()
    # Crypto in our universe is like "ADA" but Yahoo expects "ADA-USD".
    if typ == "crypto":
        if not c.endswith("-USD") and not c.startswith("^") and "=X" not in c:
            c = f"{c}-USD"
    return _yf_symbol(c)


def resolve_yfinance_ticker(symbol: str, cfg: Optional[dict] = None) -> str:
    """Map canonical universe symbol to Yahoo Finance ticker (crypto → ``SYMBOL-USD``)."""
    from telegram_agent.symbol_universe import load_symbol_type_map, normalize_symbol

    sym = normalize_symbol(symbol)
    typ = load_symbol_type_map().get(sym, "")
    if not typ and cfg:
        path_raw = cfg.get("symbol_universe_path")
        if path_raw:
            typ = load_symbol_type_map(Path(path_raw).expanduser()).get(sym, "")
    return _price_fetch_symbol(sym, typ)


def yfinance_ticker_for_symbol(symbol: str) -> str:
    """Public helper when ``cfg`` is unavailable (e.g. value_metrics API)."""
    return resolve_yfinance_ticker(symbol, None)


def price_job_symbols(cfg: dict, con) -> Tuple[List[str], Dict[str, str]]:
    """
    Canonical symbols and optional type map (same resolution as daily backfill).
    Typed universe file is loaded whenever ``symbol_universe_path`` is set so
    crypto tickers map to ``…-USD`` even in mention-only mode when symbols overlap.
    """
    explicit = cfg.get("prices_symbols")
    if explicit:
        canon_syms = sorted({str(s).strip().upper() for s in explicit if str(s).strip()})
        typed_map: Dict[str, str] = {}
        path_raw = cfg.get("symbol_universe_path")
        if path_raw:
            typed_universe = _load_typed_universe_from_path(Path(path_raw).expanduser())
            if typed_universe:
                typed_map = {s.strip().upper(): t for s, t in typed_universe if s and str(s).strip()}
        return canon_syms, typed_map

    uni = symbol_universe_set(cfg)
    use_universe = uni is not None
    typed_universe: Optional[List[Tuple[str, str]]] = None
    path_raw = cfg.get("symbol_universe_path")
    if path_raw:
        typed_universe = _load_typed_universe_from_path(Path(path_raw).expanduser())
    if use_universe and typed_universe:
        max_pr = cfg.get("max_priority")
        try:
            max_pr_i = int(max_pr) if max_pr is not None else None
        except Exception:
            max_pr_i = None
        if max_pr_i is not None:
            allowed = symbol_universe_set(cfg) or set()
            canon_syms = sorted(
                {
                    s.strip().upper()
                    for s, _t in typed_universe
                    if s and str(s).strip() and s.strip().upper() in allowed
                }
            )
        else:
            canon_syms = sorted({s.strip().upper() for s, _t in typed_universe if s and str(s).strip()})
    else:
        canon_syms = sorted(uni) if use_universe else list_mentioned_symbols(con)
    typed_map: Dict[str, str] = {}
    if typed_universe:
        typed_map = {s.strip().upper(): t for s, t in typed_universe if s and str(s).strip()}

    pr_exact = cfg.get("prices_priority")
    if pr_exact is not None:
        try:
            want = int(pr_exact)
        except Exception:
            want = None
        if want is not None:
            pmap = universe_priority_map(cfg)
            if pmap is None:
                logger.warning(
                    "prices: --priority ignored (need a JSON symbol universe with per-symbol priority, "
                    "not SYMBOL_UNIVERSE_ENV-only list)"
                )
            else:
                before = len(canon_syms)
                canon_syms = [s for s in canon_syms if pmap.get(s) == want]
                logger.info(
                    "prices: exact priority %s: %s -> %s symbol(s)",
                    want,
                    before,
                    len(canon_syms),
                )
    return canon_syms, typed_map


def _weekend_fetch_ok_by_type(fetch_sym: str, typ: str) -> bool:
    """Crypto/FX often have weekend bars; most equities/ETFs do not."""
    typ = (typ or "").strip().lower()
    if typ in ("crypto", "forex"):
        return True
    s = (fetch_sym or "").strip().upper()
    while s.startswith("$") or s.startswith("#"):
        s = s[1:]
    return s.endswith("-USD") or s.endswith("=X")


def _download_day_bars(
    con,
    symbols: Sequence[Tuple[str, str]],
    *,
    day_start_utc: datetime,
) -> int:
    """
    Batch yfinance download for one UTC day for many tickers, then upsert any bars returned.
    Returns number of upserted price rows.
    """
    import yfinance as yf

    if day_start_utc.tzinfo is None:
        day_start_utc = day_start_utc.replace(tzinfo=timezone.utc)
    day_start_utc = day_start_utc.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day_end_excl = day_start_utc + timedelta(days=1)

    # Map yfinance-normalized symbols back to our canonical symbol.
    mapping: Dict[str, str] = {}
    yf_syms: List[str] = []
    for canonical, typ in symbols:
        canon = canonical.strip().upper()
        if not canon:
            continue
        yf_s = _price_fetch_symbol(canon, typ)
        if not yf_s:
            continue
        if yf_s in mapping:
            continue
        mapping[yf_s] = canon.lstrip("$").lstrip("#")
        yf_syms.append(yf_s)
    if not yf_syms:
        return 0

    df = yf.download(
        tickers=yf_syms,
        start=day_start_utc.date(),
        end=day_end_excl.date(),
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        threads=True,
        progress=False,
    )
    if df is None or getattr(df, "empty", False):
        return 0

    # df shape: multiindex columns when multiple symbols; single-index when one symbol.
    up_total = 0
    if hasattr(df.columns, "levels") and len(getattr(df.columns, "levels", [])) >= 2:
        # MultiIndex: (ticker, field)
        for yf_sym in mapping.keys():
            if yf_sym not in df.columns.get_level_values(0):
                continue
            sub = df[yf_sym]
            if sub is None or sub.empty:
                continue
            for idx, row in sub.iterrows():
                ts = idx.to_pydatetime()
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                else:
                    ts = ts.astimezone(timezone.utc)
                ts_iso = ts.replace(microsecond=0).isoformat()
                c = float(row.get("Close")) if row.get("Close") == row.get("Close") else None
                if c is None:
                    continue
                o = float(row.get("Open")) if row.get("Open") == row.get("Open") else None
                h = float(row.get("High")) if row.get("High") == row.get("High") else None
                l = float(row.get("Low")) if row.get("Low") == row.get("Low") else None
                adj = float(row.get("Adj Close")) if "Adj Close" in row and row["Adj Close"] == row["Adj Close"] else None
                vol = float(row.get("Volume")) if "Volume" in row and row["Volume"] == row["Volume"] else None
                up_total += upsert_price_rows(
                    con,
                    mapping[yf_sym],
                    [(ts_iso, o or c, h or c, l or c, c, adj, vol)],
                    interval="1d",
                    source="yfinance",
                )
    else:
        # Single symbol DataFrame
        yf_sym = yf_syms[0]
        for idx, row in df.iterrows():
            ts = idx.to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            ts_iso = ts.replace(microsecond=0).isoformat()
            c = float(row.get("Close")) if row.get("Close") == row.get("Close") else None
            if c is None:
                continue
            o = float(row.get("Open")) if row.get("Open") == row.get("Open") else None
            h = float(row.get("High")) if row.get("High") == row.get("High") else None
            l = float(row.get("Low")) if row.get("Low") == row.get("Low") else None
            adj = float(row.get("Adj Close")) if "Adj Close" in row and row["Adj Close"] == row["Adj Close"] else None
            vol = float(row.get("Volume")) if "Volume" in row and row["Volume"] == row["Volume"] else None
            up_total += upsert_price_rows(
                con,
                mapping[yf_sym],
                [(ts_iso, o or c, h or c, l or c, c, adj, vol)],
                interval="1d",
                source="yfinance",
            )

    return int(up_total)


def _intraday_dataframe_to_rows(df) -> List[Tuple[str, float, float, float, float, Optional[float], Optional[float]]]:
    import pandas as pd

    rows: List[Tuple[str, float, float, float, float, Optional[float], Optional[float]]] = []
    if df is None or getattr(df, "empty", True):
        return rows
    for idx, row in df.iterrows():
        ts = pd.Timestamp(idx)
        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        dt = ts.to_pydatetime().replace(microsecond=0, tzinfo=timezone.utc)
        ts_iso = dt.isoformat()
        c = row.get("Close")
        if c is None or pd.isna(c):
            continue
        c = float(c)
        o = row.get("Open")
        h = row.get("High")
        l = row.get("Low")
        o = float(o) if o is not None and not pd.isna(o) else None
        h = float(h) if h is not None and not pd.isna(h) else None
        l = float(l) if l is not None and not pd.isna(l) else None
        adj = row.get("Adj Close")
        if adj is None or pd.isna(adj):
            adj = c
        else:
            adj = float(adj)
        vol = row.get("Volume")
        vol = float(vol) if vol is not None and not pd.isna(vol) else None
        rows.append((ts_iso, o or c, h or c, l or c, c, adj, vol))
    return rows


def fetch_and_store_intraday_history(
    con,
    canonical_symbol: str,
    fetch_sym: str,
    table: str,
    *,
    yf_interval: str,
    start: datetime,
    end: datetime,
    chunk_days: float,
    auto_adjust: bool,
    prepost: bool,
    sleep_seconds: float,
) -> int:
    import time

    import yfinance as yf

    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if start >= end:
        return 0
    total = 0
    cur = start
    while cur < end:
        nxt = min(end, cur + timedelta(days=chunk_days))
        try:
            # Creating a fresh Ticker per chunk avoids some yfinance internal state bugs
            # (observed as sporadic "'NoneType' object is not subscriptable" errors).
            t = yf.Ticker(fetch_sym)
            df = t.history(
                start=cur,
                end=nxt,
                interval=yf_interval,
                auto_adjust=auto_adjust,
                prepost=prepost,
            )
        except Exception as e:
            # Fallback: yfinance sometimes fails inside Ticker.history; try download().
            df = None
            try:
                df = yf.download(
                    fetch_sym,
                    start=cur,
                    end=nxt,
                    interval=yf_interval,
                    auto_adjust=auto_adjust,
                    prepost=prepost,
                    progress=False,
                    threads=False,
                )
            except Exception:
                df = None
            logger.warning(
                "yfinance intraday %s %s %s-%s: %s",
                canonical_symbol,
                yf_interval,
                cur.isoformat(),
                nxt.isoformat(),
                e,
            )
            # If fallback produced data, continue without sleeping/advancing early.
            if df is not None and not getattr(df, "empty", True):
                rows = _intraday_dataframe_to_rows(df)
                if rows:
                    total += upsert_intraday_rows(con, table, canonical_symbol, rows, source="yfinance")
                cur = nxt
                if sleep_seconds > 0 and cur < end:
                    time.sleep(sleep_seconds)
                continue
            cur = nxt
            if sleep_seconds > 0 and cur < end:
                time.sleep(sleep_seconds)
            continue
        rows = _intraday_dataframe_to_rows(df)
        if rows:
            total += upsert_intraday_rows(con, table, canonical_symbol, rows, source="yfinance")
        cur = nxt
        if sleep_seconds > 0 and cur < end:
            time.sleep(sleep_seconds)
    return total


def fetch_and_store_history(
    con,
    symbol: str,
    *,
    start: datetime,
    end: Optional[datetime] = None,
    interval: str = "1d",
    fetch_symbol: Optional[str] = None,
) -> int:
    import yfinance as yf

    end = end or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    sym = fetch_symbol.strip() if fetch_symbol else _yf_symbol(symbol)
    try:
        t = yf.Ticker(sym)
        df = t.history(start=start.date(), end=(end + timedelta(days=1)).date(), interval="1d", auto_adjust=False)
    except Exception as e:
        logger.warning("yfinance failed for %s: %s", sym, e)
        return 0
    if df is None or df.empty:
        logger.info("No price rows for %s", sym)
        return 0

    rows: List[tuple] = []
    for idx, row in df.iterrows():
        ts = idx.to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        ts_iso = ts.replace(microsecond=0).isoformat()
        o = float(row["Open"]) if row.get("Open") == row.get("Open") else None
        h = float(row["High"]) if row.get("High") == row.get("High") else None
        l = float(row["Low"]) if row.get("Low") == row.get("Low") else None
        c = float(row["Close"]) if row.get("Close") == row.get("Close") else None
        adj = float(row["Adj Close"]) if "Adj Close" in row and row["Adj Close"] == row["Adj Close"] else None
        vol = float(row["Volume"]) if "Volume" in row and row["Volume"] == row["Volume"] else None
        if c is None:
            continue
        rows.append((ts_iso, o or c, h or c, l or c, c, adj, vol))

    return upsert_price_rows(con, symbol, rows, interval=interval)


def backfill_all_mentioned(cfg: dict, *, days: int = 400) -> None:
    """Fetch daily history for symbols.

    In fixed-universe mode, this fetches for the configured universe (top-1000).
    Otherwise it fetches for all symbols seen in `news_mentions`.
    """
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    uni = symbol_universe_set(cfg)
    use_universe = uni is not None
    syms, typed_map = price_job_symbols(cfg, con)
    if not syms:
        if use_universe:
            logger.info(
                "No symbols to price (universe empty or failed to load). Check SYMBOL_UNIVERSE_PATH / JSON."
            )
        else:
            logger.info("No symbols in news_mentions; set SYMBOL_UNIVERSE_PATH or run extract after ingest.")
        con.close()
        return
    if use_universe:
        logger.info("Backfilling prices for %s universe symbols (%s days)", len(syms), days)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    force = bool(cfg.get("prices_force", False))
    batch_size = int(cfg.get("prices_yf_batch_size", 80))
    batch_size = max(1, min(200, batch_size))
    sleep_s = float(cfg.get("prices_yf_sleep_seconds", 0.5))

    from telegram_agent.agent_db import list_price_symbols_for_day_utc

    day0 = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    dayN = end.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    reverse = bool(cfg.get("prices_backfill_reverse", False))
    d = dayN if reverse else day0
    def _advance(dt: datetime) -> datetime:
        return dt - timedelta(days=1) if reverse else dt + timedelta(days=1)

    while (d >= day0) if reverse else (d <= dayN):
        is_weekend = d.weekday() >= 5
        candidates = syms

        # Avoid trying to fetch "today" for equities/ETFs/indices: the 1d bar is often not
        # available until after the market close, and yfinance will emit noisy "no price data"
        # errors. We still allow today's fetch for crypto/FX (weekend-like trading).
        if d.date() == dayN.date() and not force:
            if typed_map:
                candidates = [
                    s
                    for s in candidates
                    if _weekend_fetch_ok_by_type(_price_fetch_symbol(s, typed_map.get(s, "")), typed_map.get(s, ""))
                ]
            else:
                candidates = [s for s in candidates if _weekend_fetch_ok_by_type(_yf_symbol(s), "")]
            if not candidates:
                d = _advance(d)
                continue

        if is_weekend:
            # Only fetch for symbols likely to have weekend bars.
            if typed_map:
                candidates = [
                    s
                    for s in syms
                    if _weekend_fetch_ok_by_type(_price_fetch_symbol(s, typed_map.get(s, "")), typed_map.get(s, ""))
                ]
            else:
                # Heuristic for non-universe mode: only tickers already in Yahoo weekend-trading formats.
                candidates = [s for s in syms if _weekend_fetch_ok_by_type(_yf_symbol(s), "")]
            if not candidates:
                d = _advance(d)
                continue

        if not force:
            present = set(list_price_symbols_for_day_utc(con, day_utc=d, interval="1d"))
            missing = [s for s in candidates if s.upper().lstrip("$").lstrip("#") not in present]
        else:
            missing = list(candidates)

        if not missing:
            d = _advance(d)
            continue

        # Batch yfinance calls for this day.
        total_up = 0
        for i in range(0, len(missing), batch_size):
            chunk = missing[i : i + batch_size]
            try:
                if typed_map:
                    payload = [(s, typed_map.get(s.strip().upper(), "")) for s in chunk]
                else:
                    payload = [(s, "") for s in chunk]
                total_up += _download_day_bars(con, payload, day_start_utc=d)
            except Exception as e:
                logger.warning("yfinance batch failed day=%s n=%s: %s", d.date().isoformat(), len(chunk), e)
            if sleep_s > 0 and i + batch_size < len(missing):
                import time
                time.sleep(sleep_s)
        if total_up:
            logger.info("Prices day %s: upserted %s rows (missing=%s)", d.date().isoformat(), total_up, len(missing))

        d = _advance(d)
    con.close()


def incremental_prices(cfg: dict) -> None:
    """Update price history.

    In fixed-universe mode, it updates prices for the configured universe.
    Otherwise it updates for all symbols seen in `news_mentions`.
    """
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    end = datetime.now(timezone.utc)
    uni = symbol_universe_set(cfg)
    use_universe = uni is not None
    syms, typed_map = price_job_symbols(cfg, con)
    if not syms:
        if use_universe:
            logger.info(
                "No symbols to price (universe empty or failed to load). Check SYMBOL_UNIVERSE_PATH / JSON."
            )
        else:
            logger.info("No symbols in news_mentions; set SYMBOL_UNIVERSE_PATH or run extract after ingest.")
        con.close()
        return
    if use_universe:
        logger.info("Incremental prices for %s universe symbols", len(syms))
    force = bool(cfg.get("prices_force", False))
    for sym in syms:
        fetch_sym = _price_fetch_symbol(sym, typed_map.get(sym, ""))
        latest = get_latest_price_ts(con, sym)
        start = (latest - timedelta(days=2)) if latest else end - timedelta(days=400)
        if not force and latest and latest >= (end - timedelta(days=1)):
            # Already have very recent bars.
            continue
        n = fetch_and_store_history(con, sym, start=start, end=end, fetch_symbol=fetch_sym)
        if n:
            logger.info("Incremental %s: %s rows", sym, n)
    con.close()


def backfill_intraday_all_mentioned(cfg: dict, *, yf_interval: str, days: int) -> None:
    """Fetch 1h or 1m OHLCV into ``prices_hourly`` / ``prices_minute`` (not ``prices``)."""
    table = INTRADAY_TABLE_BY_YF_INTERVAL[yf_interval]
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    uni = symbol_universe_set(cfg)
    use_universe = uni is not None
    syms, typed_map = price_job_symbols(cfg, con)
    if not syms:
        if use_universe:
            logger.info(
                "No symbols for intraday prices (universe empty or failed to load). Check SYMBOL_UNIVERSE_PATH / JSON."
            )
        else:
            logger.info("No symbols in news_mentions; set SYMBOL_UNIVERSE_PATH or run extract after ingest.")
        con.close()
        return
    cap_1h = int(cfg.get("prices_intraday_1h_max_backfill_days", 730))
    cap_1m = int(cfg.get("prices_intraday_1m_max_backfill_days", 30))
    if yf_interval == "1h":
        lookback_days = min(int(days), cap_1h)
        chunk_days = float(cfg.get("prices_intraday_chunk_days_1h", 120))
    elif yf_interval == "1m":
        lookback_days = min(int(days), cap_1m)
        chunk_days = float(cfg.get("prices_intraday_chunk_days_1m", 7))
    else:
        con.close()
        raise ValueError("yf_interval must be 1h or 1m")
    auto_adjust = bool(cfg.get("prices_intraday_auto_adjust", True))
    prepost = bool(cfg.get("prices_intraday_prepost", True))
    sleep_s = float(cfg.get("prices_yf_sleep_seconds", 0.5))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    logger.info(
        "Intraday backfill interval=%s table=%s symbols=%s lookback_days=%s chunk_days=%s",
        yf_interval,
        table,
        len(syms),
        lookback_days,
        chunk_days,
    )
    for sym in syms:
        fetch_sym = _price_fetch_symbol(sym, typed_map.get(sym, ""))
        n = fetch_and_store_intraday_history(
            con,
            sym,
            fetch_sym,
            table,
            yf_interval=yf_interval,
            start=start,
            end=end,
            chunk_days=chunk_days,
            auto_adjust=auto_adjust,
            prepost=prepost,
            sleep_seconds=0.0,
        )
        if n:
            logger.info("Intraday backfill %s %s: %s rows", yf_interval, sym, n)
        if sleep_s > 0:
            import time

            time.sleep(sleep_s)
    con.close()


def incremental_intraday_prices(cfg: dict, *, yf_interval: str) -> None:
    table = INTRADAY_TABLE_BY_YF_INTERVAL[yf_interval]
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    uni = symbol_universe_set(cfg)
    use_universe = uni is not None
    syms, typed_map = price_job_symbols(cfg, con)
    if not syms:
        if use_universe:
            logger.info(
                "No symbols for intraday prices (universe empty or failed to load). Check SYMBOL_UNIVERSE_PATH / JSON."
            )
        else:
            logger.info("No symbols in news_mentions; set SYMBOL_UNIVERSE_PATH or run extract after ingest.")
        con.close()
        return
    auto_adjust = bool(cfg.get("prices_intraday_auto_adjust", True))
    prepost = bool(cfg.get("prices_intraday_prepost", True))
    sleep_s = float(cfg.get("prices_yf_sleep_seconds", 0.5))
    force = bool(cfg.get("prices_force", False))
    end = datetime.now(timezone.utc)
    if yf_interval == "1h":
        chunk_days = float(cfg.get("prices_intraday_chunk_days_1h", 120))
        default_lookback = int(cfg.get("prices_intraday_1h_incremental_lookback_days", 14))
        stale_after = timedelta(hours=2)
    else:
        chunk_days = float(cfg.get("prices_intraday_chunk_days_1m", 7))
        default_lookback = int(cfg.get("prices_intraday_1m_incremental_lookback_days", 7))
        stale_after = timedelta(minutes=45)
    for sym in syms:
        fetch_sym = _price_fetch_symbol(sym, typed_map.get(sym, ""))
        latest = get_latest_intraday_ts(con, sym, table=table)
        if not force and latest and (end - latest) <= stale_after:
            continue
        if latest:
            start = latest - (timedelta(hours=4) if yf_interval == "1h" else timedelta(hours=2))
        else:
            start = end - timedelta(days=default_lookback)
        # Avoid huge catch-up windows (Yahoo limits + noise); widen with explicit backfill.
        start = max(start, end - timedelta(days=default_lookback * 4))
        n = fetch_and_store_intraday_history(
            con,
            sym,
            fetch_sym,
            table,
            yf_interval=yf_interval,
            start=start,
            end=end,
            chunk_days=chunk_days,
            auto_adjust=auto_adjust,
            prepost=prepost,
            sleep_seconds=0.0,
        )
        if n:
            logger.info("Incremental intraday %s %s: %s rows", yf_interval, sym, n)
        if sleep_s > 0:
            import time

            time.sleep(sleep_s)
    con.close()


def run_prices(cfg: dict, *, mode: str, days: int, intervals: str) -> None:
    """
    Run daily and/or intraday price jobs. ``intervals`` is comma-separated: 1d, 1h, 1m.
    Intraday data is stored in ``prices_hourly`` / ``prices_minute`` only.
    """
    parts: Set[str] = {p.strip().lower() for p in intervals.split(",") if p.strip()}
    valid = {"1d", "1h", "1m"}
    unknown = parts - valid
    if unknown:
        raise ValueError(f"Unknown interval(s): {sorted(unknown)}; allowed: 1d, 1h, 1m")
    if not parts:
        parts = {"1d"}
    for iv in ("1d", "1h", "1m"):
        if iv not in parts:
            continue
        if iv == "1d":
            if mode == "backfill":
                backfill_all_mentioned(cfg, days=days)
            else:
                incremental_prices(cfg)
        elif iv == "1h":
            if mode == "backfill":
                backfill_intraday_all_mentioned(cfg, yf_interval="1h", days=days)
            else:
                incremental_intraday_prices(cfg, yf_interval="1h")
        else:
            if mode == "backfill":
                backfill_intraday_all_mentioned(cfg, yf_interval="1m", days=days)
            else:
                incremental_intraday_prices(cfg, yf_interval="1m")
