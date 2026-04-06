"""SQLite storage for the agent (news, instruments, prices, memory, recs)."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import NewsItem

logger = logging.getLogger(__name__)


@dataclass
class NewsUpsertStats:
    """Result of upsert_news_items: totals and per-source duplicate counts (id already in DB)."""

    total: int
    new_count: int
    duplicate_count: int
    duplicates_by_source: Dict[str, int]


def _existing_news_ids(con: sqlite3.Connection, ids: Sequence[str]) -> set[str]:
    """Chunked IN query — SQLite limits on number of bound variables per statement."""
    out: set[str] = set()
    uniq = list({i for i in ids if i})
    chunk = 500
    for i in range(0, len(uniq), chunk):
        part = uniq[i : i + chunk]
        ph = ",".join("?" * len(part))
        cur = con.execute(f"SELECT id FROM news_items WHERE id IN ({ph})", part)
        out.update(str(r["id"]) for r in cur.fetchall())
    return out


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(s: str) -> datetime:
    # Stored as ISO-8601 with offset; datetime.fromisoformat handles it.
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Simple key-value store for incremental cursors & agent state.
CREATE TABLE IF NOT EXISTS kv_state (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL,
  updated_ts_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_items (
  id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_name TEXT NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  url TEXT,
  ts_utc TEXT NOT NULL,
  condensed TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news_items(ts_utc);
CREATE INDEX IF NOT EXISTS idx_news_source ON news_items(source_type, source_name);

-- Normalized set of instruments/assets we track over time.
-- Examples: AAPL (equity), BTC-USD (crypto), SPY (ETF), ^TNX (rates proxy), etc.
CREATE TABLE IF NOT EXISTS instruments (
  symbol TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'unknown', -- equity|crypto|etf|fx|rate|commodity|bond|index|unknown
  name TEXT,
  meta_json TEXT
);

-- Link news -> mentioned instrument(s)
CREATE TABLE IF NOT EXISTS news_mentions (
  news_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  mention_type TEXT NOT NULL DEFAULT 'unknown', -- ticker|name|entity|regex|llm
  confidence REAL,
  PRIMARY KEY (news_id, symbol),
  FOREIGN KEY(news_id) REFERENCES news_items(id) ON DELETE CASCADE,
  FOREIGN KEY(symbol) REFERENCES instruments(symbol) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mentions_symbol ON news_mentions(symbol);

-- Price series (daily by default; can store intraday later with interval column).
CREATE TABLE IF NOT EXISTS prices (
  symbol TEXT NOT NULL,
  ts_utc TEXT NOT NULL,
  interval TEXT NOT NULL DEFAULT '1d',
  open REAL,
  high REAL,
  low REAL,
  close REAL,
  adj_close REAL,
  volume REAL,
  source TEXT NOT NULL DEFAULT 'yfinance',
  PRIMARY KEY(symbol, ts_utc, interval),
  FOREIGN KEY(symbol) REFERENCES instruments(symbol) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_prices_symbol_ts ON prices(symbol, ts_utc);

-- Agent memory snapshots (summarized macro/micro highlights).
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  horizon_months INTEGER NOT NULL,
  text TEXT NOT NULL,
  meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_ts ON memories(ts_utc);

-- Recommendations produced by the agent.
CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  symbol TEXT NOT NULL,
  duration TEXT NOT NULL, -- short|mid|long
  forecast_usd REAL,
  forecast_pct REAL,
  confidence REAL,
  rationale TEXT,
  meta_json TEXT,
  FOREIGN KEY(symbol) REFERENCES instruments(symbol) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_recs_ts ON recommendations(ts_utc);
CREATE INDEX IF NOT EXISTS idx_recs_symbol ON recommendations(symbol);

-- Narrative tracker outputs (hourly/daily/weekly/monthly/annual).
CREATE TABLE IF NOT EXISTS horizon_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  horizon TEXT NOT NULL, -- hourly|daily|weekly|monthly|annual
  start_utc TEXT NOT NULL,
  end_utc TEXT NOT NULL,
  report_text TEXT NOT NULL,
  report_json TEXT,
  meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_horizon_ts ON horizon_reports(horizon, ts_utc);

-- Liquidity classification cache for symbols.
CREATE TABLE IF NOT EXISTS liquidity_cache (
  symbol TEXT PRIMARY KEY,
  liquidity_class TEXT NOT NULL, -- liquid|illiquid|unknown
  market_cap_usd REAL,
  avg_volume_usd REAL,
  source TEXT NOT NULL,
  updated_ts_utc TEXT NOT NULL
);
"""


@dataclass
class DbConfig:
    path: Path


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA_SQL)
    con.commit()
    _migrate_recommendations_extra_columns(con)


def _migrate_recommendations_extra_columns(con: sqlite3.Connection) -> None:
    """Add concrete-plan columns for recommendations (ignore if already present)."""
    for col, typ in (
        ("suggestion_ts_utc", "TEXT"),
        ("entry_window_start_utc", "TEXT"),
        ("entry_window_end_utc", "TEXT"),
        ("execute_review_utc", "TEXT"),
    ):
        try:
            con.execute(f"ALTER TABLE recommendations ADD COLUMN {col} {typ}")
            con.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                logger.debug("ALTER recommendations %s: %s", col, e)


def kv_get(con: sqlite3.Connection, key: str) -> Optional[str]:
    cur = con.execute("SELECT v FROM kv_state WHERE k = ?", (key,))
    row = cur.fetchone()
    return row["v"] if row else None


def kv_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        """
        INSERT INTO kv_state(k, v, updated_ts_utc)
        VALUES(?, ?, ?)
        ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_ts_utc=excluded.updated_ts_utc
        """,
        (key, str(value), _utc_iso(datetime.now(timezone.utc))),
    )
    con.commit()


def store_horizon_report(
    con: sqlite3.Connection,
    *,
    horizon: str,
    start_utc: datetime,
    end_utc: datetime,
    report_text: str,
    report_json: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> int:
    con.execute(
        """
        INSERT INTO horizon_reports(ts_utc, horizon, start_utc, end_utc, report_text, report_json, meta_json)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _utc_iso(datetime.now(timezone.utc)),
            horizon,
            _utc_iso(start_utc),
            _utc_iso(end_utc),
            report_text,
            json.dumps(report_json) if report_json is not None else None,
            json.dumps(meta or {}),
        ),
    )
    con.commit()
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def latest_horizon_report(con: sqlite3.Connection, horizon: str) -> Optional[sqlite3.Row]:
    cur = con.execute(
        "SELECT * FROM horizon_reports WHERE horizon=? ORDER BY ts_utc DESC LIMIT 1",
        (horizon,),
    )
    return cur.fetchone()


def upsert_liquidity_cache(
    con: sqlite3.Connection,
    *,
    symbol: str,
    liquidity_class: str,
    market_cap_usd: Optional[float],
    avg_volume_usd: Optional[float],
    source: str,
) -> None:
    con.execute(
        """
        INSERT INTO liquidity_cache(symbol, liquidity_class, market_cap_usd, avg_volume_usd, source, updated_ts_utc)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
          liquidity_class=excluded.liquidity_class,
          market_cap_usd=excluded.market_cap_usd,
          avg_volume_usd=excluded.avg_volume_usd,
          source=excluded.source,
          updated_ts_utc=excluded.updated_ts_utc
        """,
        (
            symbol.upper(),
            liquidity_class,
            market_cap_usd,
            avg_volume_usd,
            source,
            _utc_iso(datetime.now(timezone.utc)),
        ),
    )
    con.commit()


def get_liquidity_cache(con: sqlite3.Connection, symbol: str) -> Optional[sqlite3.Row]:
    cur = con.execute("SELECT * FROM liquidity_cache WHERE symbol=?", (symbol.upper(),))
    return cur.fetchone()


def upsert_news_items(con: sqlite3.Connection, items: Sequence[NewsItem]) -> NewsUpsertStats:
    if not items:
        return NewsUpsertStats(0, 0, 0, {})

    ids = [it.id for it in items]
    pre_existing = _existing_news_ids(con, ids)

    duplicates_by_source: Dict[str, int] = {}
    new_count = 0
    for it in items:
        sid = it.id
        label = (it.source_name or "").strip() or it.source_type or "unknown"
        if sid in pre_existing:
            duplicates_by_source[label] = duplicates_by_source.get(label, 0) + 1
        else:
            new_count += 1

    rows = [
        (
            it.id,
            it.source_type,
            it.source_name,
            it.title,
            it.content,
            it.url,
            _utc_iso(it.timestamp),
            it.condensed,
        )
        for it in items
    ]
    con.executemany(
        """
        INSERT INTO news_items(id, source_type, source_name, title, content, url, ts_utc, condensed)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          source_type=excluded.source_type,
          source_name=excluded.source_name,
          title=excluded.title,
          content=excluded.content,
          url=excluded.url,
          ts_utc=excluded.ts_utc,
          condensed=COALESCE(excluded.condensed, news_items.condensed)
        """,
        rows,
    )
    con.commit()

    dup_total = sum(duplicates_by_source.values())
    return NewsUpsertStats(
        total=len(rows),
        new_count=new_count,
        duplicate_count=dup_total,
        duplicates_by_source=duplicates_by_source,
    )


def list_news_ids_since(con: sqlite3.Connection, since_utc: datetime) -> List[str]:
    cur = con.execute(
        "SELECT id FROM news_items WHERE ts_utc >= ? ORDER BY ts_utc DESC",
        (_utc_iso(since_utc),),
    )
    return [r["id"] for r in cur.fetchall()]


def get_latest_news_ts(con: sqlite3.Connection) -> Optional[datetime]:
    cur = con.execute("SELECT ts_utc FROM news_items ORDER BY ts_utc DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return None
    return _parse_dt(row["ts_utc"])


def clear_news_items(con: sqlite3.Connection) -> int:
    """Delete all ingested news rows. Cascades to news_mentions (FK ON DELETE CASCADE)."""
    cur = con.execute("DELETE FROM news_items")
    con.commit()
    return int(cur.rowcount or 0)


def clear_ingest_kv_cursors(con: sqlite3.Connection) -> int:
    """
    Remove ingest incremental/backfill cursor keys so the next ingest run does not
    assume prior fetch windows (see ingest.py: ingest:last_run_ts, ingest:last_backfill_ts).
    """
    cur = con.execute(
        """
        DELETE FROM kv_state
        WHERE k IN ('ingest:last_run_ts', 'ingest:last_backfill_ts')
        """
    )
    con.commit()
    return int(cur.rowcount or 0)


def clear_news_mentions(con: sqlite3.Connection) -> int:
    """Delete all rows in news_mentions (extracted tickers per news item)."""
    cur = con.execute("DELETE FROM news_mentions")
    con.commit()
    return int(cur.rowcount or 0)


def clear_research_outputs(con: sqlite3.Connection) -> Tuple[int, int]:
    """
    Delete all research recommendations and all memory snapshots.

    The `memories` table holds both structured research memory (snapshots) and rows written
    by `python -m telegram_agent.agent memory`; this clears all of it.
    """
    cur_r = con.execute("DELETE FROM recommendations")
    n_rec = int(cur_r.rowcount or 0)
    cur_m = con.execute("DELETE FROM memories")
    n_mem = int(cur_m.rowcount or 0)
    con.commit()
    return n_mem, n_rec


def clear_orphan_instruments(con: sqlite3.Connection) -> Tuple[int, int]:
    """
    Remove instruments that are not referenced by prices or recommendations.
    Also removes liquidity_cache rows whose symbol no longer exists in instruments.
    Call after clear_news_mentions if you want a clean instrument list for re-extraction.
    """
    cur = con.execute(
        """
        DELETE FROM instruments
        WHERE NOT EXISTS (SELECT 1 FROM prices p WHERE p.symbol = instruments.symbol)
          AND NOT EXISTS (SELECT 1 FROM recommendations r WHERE r.symbol = instruments.symbol)
        """
    )
    n_inst = int(cur.rowcount or 0)
    cur2 = con.execute(
        """
        DELETE FROM liquidity_cache
        WHERE NOT EXISTS (SELECT 1 FROM instruments i WHERE i.symbol = liquidity_cache.symbol)
        """
    )
    n_liq = int(cur2.rowcount or 0)
    con.commit()
    return n_inst, n_liq


def ensure_instruments(con: sqlite3.Connection, symbols: Iterable[str], *, kind: str = "unknown") -> int:
    syms = [s.strip().upper() for s in symbols if s and str(s).strip()]
    if not syms:
        return 0
    con.executemany(
        "INSERT OR IGNORE INTO instruments(symbol, kind) VALUES(?, ?)",
        [(s, kind) for s in syms],
    )
    con.commit()
    return len(syms)


def add_mentions(
    con: sqlite3.Connection,
    news_id: str,
    mentions: Sequence[Tuple[str, str, Optional[float]]],
) -> int:
    """
    mentions: [(symbol, mention_type, confidence), ...]
    """
    if not mentions:
        return 0
    ensure_instruments(con, [m[0] for m in mentions])
    con.executemany(
        """
        INSERT OR REPLACE INTO news_mentions(news_id, symbol, mention_type, confidence)
        VALUES(?, ?, ?, ?)
        """,
        [(news_id, s.strip().upper(), t or "unknown", c) for (s, t, c) in mentions],
    )
    con.commit()
    return len(mentions)


def upsert_memory(
    con: sqlite3.Connection,
    *,
    horizon_months: int,
    text: str,
    meta: Optional[Dict[str, Any]] = None,
    ts_utc: Optional[datetime] = None,
) -> int:
    """Insert a memory snapshot. Use ts_utc for backtests/simulated dates; default is now."""
    run_ts = ts_utc if ts_utc is not None else datetime.now(timezone.utc)
    con.execute(
        "INSERT INTO memories(ts_utc, horizon_months, text, meta_json) VALUES(?, ?, ?, ?)",
        (_utc_iso(run_ts), int(horizon_months), text, json.dumps(meta or {})),
    )
    con.commit()
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def fetch_news_rows_between(
    con: sqlite3.Connection, start_utc: datetime, end_utc: datetime, *, limit: int = 500
) -> List[sqlite3.Row]:
    cur = con.execute(
        """
        SELECT id, source_type, source_name, title, content, ts_utc, condensed
        FROM news_items
        WHERE ts_utc >= ? AND ts_utc <= ?
        ORDER BY ts_utc DESC
        LIMIT ?
        """,
        (_utc_iso(start_utc), _utc_iso(end_utc), int(limit)),
    )
    return list(cur.fetchall())


def fetch_news_rows_calendar_day_utc(
    con: sqlite3.Connection, day_start_utc: datetime, *, limit: int = 500
) -> List[sqlite3.Row]:
    """News items with ts_utc in [day_start_utc, day_start_utc + 1 day) (UTC calendar day)."""
    if day_start_utc.tzinfo is None:
        day_start_utc = day_start_utc.replace(tzinfo=timezone.utc)
    day_start_utc = day_start_utc.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_excl = day_start_utc + timedelta(days=1)
    cur = con.execute(
        """
        SELECT id, source_type, source_name, title, content, ts_utc, condensed
        FROM news_items
        WHERE ts_utc >= ? AND ts_utc < ?
        ORDER BY ts_utc DESC
        LIMIT ?
        """,
        (_utc_iso(day_start_utc), _utc_iso(end_excl), int(limit)),
    )
    return list(cur.fetchall())


def list_mentioned_symbols(con: sqlite3.Connection) -> List[str]:
    cur = con.execute("SELECT DISTINCT symbol FROM news_mentions ORDER BY symbol")
    return [r["symbol"] for r in cur.fetchall()]


def top_mentioned_symbols(con: sqlite3.Connection, limit: int = 25) -> List[Tuple[str, int]]:
    cur = con.execute(
        """
        SELECT symbol, COUNT(*) AS c FROM news_mentions
        GROUP BY symbol ORDER BY c DESC LIMIT ?
        """,
        (int(limit),),
    )
    return [(r["symbol"], int(r["c"])) for r in cur.fetchall()]


def list_symbols_needing_prices(
    con: sqlite3.Connection, *, interval: str = "1d", min_bars: int = 2
) -> List[str]:
    """Symbols in mentions with fewer than min_bars price rows (rough gap detection)."""
    cur = con.execute(
        """
        SELECT nm.symbol, COUNT(p.ts_utc) AS n
        FROM news_mentions nm
        LEFT JOIN prices p ON p.symbol = nm.symbol AND p.interval = ?
        GROUP BY nm.symbol
        HAVING n < ?
        """,
        (interval, min_bars),
    )
    return [r["symbol"] for r in cur.fetchall()]


def upsert_price_rows(
    con: sqlite3.Connection,
    symbol: str,
    rows: Sequence[Tuple[str, float, float, float, float, Optional[float], Optional[float]]],
    *,
    interval: str = "1d",
    source: str = "yfinance",
) -> int:
    """
    rows: (ts_utc_iso, open, high, low, close, adj_close, volume)
    """
    if not rows:
        return 0
    ensure_instruments(con, [symbol])
    sym = symbol.strip().upper()
    data = [
        (sym, ts, interval, o, h, l, c, adj, vol, source)
        for (ts, o, h, l, c, adj, vol) in rows
    ]
    con.executemany(
        """
        INSERT INTO prices(symbol, ts_utc, interval, open, high, low, close, adj_close, volume, source)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, ts_utc, interval) DO UPDATE SET
          open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
          adj_close=excluded.adj_close, volume=excluded.volume, source=excluded.source
        """,
        data,
    )
    con.commit()
    return len(data)


def get_latest_price_ts(con: sqlite3.Connection, symbol: str, *, interval: str = "1d") -> Optional[datetime]:
    cur = con.execute(
        "SELECT ts_utc FROM prices WHERE symbol = ? AND interval = ? ORDER BY ts_utc DESC LIMIT 1",
        (symbol.upper(), interval),
    )
    row = cur.fetchone()
    return _parse_dt(row["ts_utc"]) if row else None


def get_close_at_or_before(
    con: sqlite3.Connection, symbol: str, ts: datetime, *, interval: str = "1d"
) -> Optional[float]:
    cur = con.execute(
        """
        SELECT close, adj_close FROM prices
        WHERE symbol = ? AND interval = ? AND ts_utc <= ?
        ORDER BY ts_utc DESC LIMIT 1
        """,
        (symbol.upper(), interval, _utc_iso(ts)),
    )
    row = cur.fetchone()
    if not row:
        return None
    return float(row["adj_close"] if row["adj_close"] is not None else row["close"])


def insert_recommendation(
    con: sqlite3.Connection,
    *,
    symbol: str,
    duration: str = "plan",
    forecast_usd: Optional[float] = None,
    forecast_pct: Optional[float] = None,
    confidence: Optional[float] = None,
    rationale: str = "",
    meta: Optional[Dict[str, Any]] = None,
    ts_utc: Optional[datetime] = None,
    suggestion_ts_utc: Optional[datetime] = None,
    entry_window_start_utc: Optional[datetime] = None,
    entry_window_end_utc: Optional[datetime] = None,
    execute_review_utc: Optional[datetime] = None,
) -> int:
    ensure_instruments(con, [symbol])
    run_ts = ts_utc or datetime.now(timezone.utc)

    def _iso(x: Optional[datetime]) -> Optional[str]:
        return _utc_iso(x) if x else None

    con.execute(
        """
        INSERT INTO recommendations(
          ts_utc, symbol, duration, forecast_usd, forecast_pct, confidence, rationale, meta_json,
          suggestion_ts_utc, entry_window_start_utc, entry_window_end_utc, execute_review_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _utc_iso(run_ts),
            symbol.upper(),
            duration,
            forecast_usd,
            forecast_pct,
            confidence,
            rationale,
            json.dumps(meta or {}),
            _iso(suggestion_ts_utc),
            _iso(entry_window_start_utc),
            _iso(entry_window_end_utc),
            _iso(execute_review_utc),
        ),
    )
    con.commit()
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def update_recommendation_meta(con: sqlite3.Connection, rec_id: int, meta: Dict[str, Any]) -> None:
    con.execute(
        "UPDATE recommendations SET meta_json = ? WHERE id = ?",
        (json.dumps(meta), int(rec_id)),
    )
    con.commit()


def list_recommendations(con: sqlite3.Connection, *, since_utc: Optional[datetime] = None) -> List[sqlite3.Row]:
    if since_utc:
        cur = con.execute(
            "SELECT * FROM recommendations WHERE ts_utc >= ? ORDER BY ts_utc ASC",
            (_utc_iso(since_utc),),
        )
    else:
        cur = con.execute("SELECT * FROM recommendations ORDER BY ts_utc ASC")
    return list(cur.fetchall())


def latest_memory(con: sqlite3.Connection) -> Optional[sqlite3.Row]:
    cur = con.execute("SELECT * FROM memories ORDER BY ts_utc DESC LIMIT 1")
    return cur.fetchone()


def latest_memory_before(con: sqlite3.Connection, before_ts_utc: datetime) -> Optional[sqlite3.Row]:
    """Latest memory snapshot strictly before before_ts_utc (for simulated daily backfill)."""
    if before_ts_utc.tzinfo is None:
        before_ts_utc = before_ts_utc.replace(tzinfo=timezone.utc)
    before_ts_utc = before_ts_utc.astimezone(timezone.utc)
    cur = con.execute(
        "SELECT * FROM memories WHERE ts_utc < ? ORDER BY ts_utc DESC LIMIT 1",
        (_utc_iso(before_ts_utc),),
    )
    return cur.fetchone()


def count_memories_before(con: sqlite3.Connection, before_ts_utc: datetime) -> int:
    """How many research memory snapshots exist strictly before before_ts_utc (epistemic 'run depth')."""
    if before_ts_utc.tzinfo is None:
        before_ts_utc = before_ts_utc.replace(tzinfo=timezone.utc)
    before_ts_utc = before_ts_utc.astimezone(timezone.utc)
    cur = con.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE ts_utc < ?",
        (_utc_iso(before_ts_utc),),
    )
    row = cur.fetchone()
    return int(row["c"]) if row else 0


def list_recommendations_with_tester_for_prompt(
    con: sqlite3.Connection,
    *,
    before_ts_utc: datetime,
    limit: int = 50,
    overfetch: int = 200,
) -> List[sqlite3.Row]:
    """
    Recommendations at or before simulated time, newest first, only rows with meta_json.tester.
    Used to feed backtest outcomes into the research prompt.
    """
    if before_ts_utc.tzinfo is None:
        before_ts_utc = before_ts_utc.replace(tzinfo=timezone.utc)
    before_ts_utc = before_ts_utc.astimezone(timezone.utc)
    cur = con.execute(
        """
        SELECT * FROM recommendations
        WHERE ts_utc <= ?
        ORDER BY ts_utc DESC
        LIMIT ?
        """,
        (_utc_iso(before_ts_utc), int(overfetch)),
    )
    rows: List[sqlite3.Row] = list(cur.fetchall())
    out: List[sqlite3.Row] = []
    for r in rows:
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        if not isinstance(meta.get("tester"), dict):
            continue
        if meta["tester"].get("skipped"):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def top_mentioned_symbols_for_news_window(
    con: sqlite3.Connection,
    news_start_utc: datetime,
    news_end_exclusive_utc: datetime,
    limit: int = 80,
) -> List[Tuple[str, int]]:
    """Mention counts restricted to news items in [start, end_exclusive)."""
    cur = con.execute(
        """
        SELECT nm.symbol, COUNT(*) AS c
        FROM news_mentions nm
        JOIN news_items ni ON ni.id = nm.news_id
        WHERE ni.ts_utc >= ? AND ni.ts_utc < ?
        GROUP BY nm.symbol
        ORDER BY c DESC
        LIMIT ?
        """,
        (_utc_iso(news_start_utc), _utc_iso(news_end_exclusive_utc), int(limit)),
    )
    return [(r["symbol"], int(r["c"])) for r in cur.fetchall()]

