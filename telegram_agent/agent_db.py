"""SQLite storage for the agent (news, instruments, prices, memory, recs)."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import NewsItem

logger = logging.getLogger(__name__)


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
"""


@dataclass
class DbConfig:
    path: Path


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA_SQL)
    con.commit()


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


def upsert_news_items(con: sqlite3.Connection, items: Sequence[NewsItem]) -> int:
    if not items:
        return 0
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
    return len(rows)


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


def upsert_memory(con: sqlite3.Connection, *, horizon_months: int, text: str, meta: Optional[Dict[str, Any]] = None) -> int:
    con.execute(
        "INSERT INTO memories(ts_utc, horizon_months, text, meta_json) VALUES(?, ?, ?, ?)",
        (_utc_iso(datetime.now(timezone.utc)), int(horizon_months), text, json.dumps(meta or {})),
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
    duration: str,
    forecast_usd: Optional[float],
    forecast_pct: Optional[float],
    confidence: Optional[float],
    rationale: str,
    meta: Optional[Dict[str, Any]] = None,
) -> int:
    ensure_instruments(con, [symbol])
    con.execute(
        """
        INSERT INTO recommendations(ts_utc, symbol, duration, forecast_usd, forecast_pct, confidence, rationale, meta_json)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _utc_iso(datetime.now(timezone.utc)),
            symbol.upper(),
            duration,
            forecast_usd,
            forecast_pct,
            confidence,
            rationale,
            json.dumps(meta or {}),
        ),
    )
    con.commit()
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


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

