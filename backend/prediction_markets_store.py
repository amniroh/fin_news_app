"""SQLite persistence for prediction-market signals (Polymarket, Kalshi)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from value_metrics_store import connect, init_db

PREDICTION_MARKETS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pm_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  category TEXT,
  status TEXT NOT NULL,
  close_time_utc TEXT,
  blockchain_ref TEXT,
  token_ids_json TEXT,
  event_url TEXT,
  raw_json TEXT,
  first_seen_ts_utc TEXT NOT NULL,
  last_seen_ts_utc TEXT NOT NULL,
  UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_pm_signals_source_status ON pm_signals(source, status);
CREATE INDEX IF NOT EXISTS idx_pm_signals_close ON pm_signals(close_time_utc);

CREATE TABLE IF NOT EXISTS pm_signal_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id INTEGER NOT NULL,
  asof_ts_utc TEXT NOT NULL,
  yes_price REAL,
  no_price REAL,
  volume REAL,
  volume_24h REAL,
  liquidity REAL,
  transaction_volume REAL,
  trade_count INTEGER,
  FOREIGN KEY(signal_id) REFERENCES pm_signals(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pm_snapshots_signal_ts ON pm_signal_snapshots(signal_id, asof_ts_utc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pm_snapshots_unique_ts ON pm_signal_snapshots(signal_id, asof_ts_utc);

CREATE TABLE IF NOT EXISTS pm_signal_analytics (
  signal_id INTEGER PRIMARY KEY,
  is_time_sensitive INTEGER,
  time_sensitive_reason TEXT,
  days_to_close_at_first REAL,
  entry_yes_price REAL,
  latest_yes_price REAL,
  settlement_result TEXT,
  settlement_yes_value REAL,
  signal_won INTEGER,
  profit_if_followed REAL,
  amount_won REAL,
  amount_lost REAL,
  wins INTEGER NOT NULL DEFAULT 0,
  losses INTEGER NOT NULL DEFAULT 0,
  pending INTEGER NOT NULL DEFAULT 1,
  win_rate REAL,
  volume_total REAL,
  volume_24h REAL,
  transaction_volume REAL,
  trade_count INTEGER,
  relevance_score REAL,
  updated_ts_utc TEXT NOT NULL,
  FOREIGN KEY(signal_id) REFERENCES pm_signals(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pm_source_sync (
  source TEXT PRIMARY KEY,
  last_sync_ts_utc TEXT,
  markets_fetched INTEGER,
  meta_json TEXT
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_prediction_markets_db(con: sqlite3.Connection) -> None:
    init_db(con)
    con.executescript(PREDICTION_MARKETS_SCHEMA_SQL)
    _migrate_prediction_markets_columns(con)
    con.commit()


def _migrate_prediction_markets_columns(con: sqlite3.Connection) -> None:
    """Add columns introduced after initial deploy (SQLite has no IF NOT EXISTS for columns)."""
    snap_cols = {r[1] for r in con.execute("PRAGMA table_info(pm_signal_snapshots)").fetchall()}
    for col, typ in (
        ("volume_24h", "REAL"),
        ("transaction_volume", "REAL"),
        ("trade_count", "INTEGER"),
    ):
        if col not in snap_cols:
            con.execute(f"ALTER TABLE pm_signal_snapshots ADD COLUMN {col} {typ}")
    ana_cols = {r[1] for r in con.execute("PRAGMA table_info(pm_signal_analytics)").fetchall()}
    for col, typ in (
        ("volume_total", "REAL"),
        ("volume_24h", "REAL"),
        ("transaction_volume", "REAL"),
        ("trade_count", "INTEGER"),
        ("relevance_score", "REAL"),
        ("amount_won", "REAL"),
        ("amount_lost", "REAL"),
    ):
        if col not in ana_cols:
            con.execute(f"ALTER TABLE pm_signal_analytics ADD COLUMN {col} {typ}")
    # Backfill won/lost split from existing profit_if_followed when columns are empty.
    con.execute(
        """
        UPDATE pm_signal_analytics
        SET
          amount_won = CASE
            WHEN profit_if_followed IS NULL THEN amount_won
            WHEN profit_if_followed > 0 THEN profit_if_followed
            ELSE 0
          END,
          amount_lost = CASE
            WHEN profit_if_followed IS NULL THEN amount_lost
            WHEN profit_if_followed < 0 THEN ABS(profit_if_followed)
            ELSE 0
          END
        WHERE profit_if_followed IS NOT NULL
          AND (amount_won IS NULL OR amount_lost IS NULL)
        """
    )
    con.commit()


def upsert_signal(con: sqlite3.Connection, row: Dict[str, Any]) -> int:
    now = _utcnow_iso()
    source = str(row["source"]).strip().lower()
    external_id = str(row["external_id"]).strip()
    title = str(row.get("title") or external_id)
    existing = con.execute(
        "SELECT id, first_seen_ts_utc FROM pm_signals WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    first_seen = str(existing["first_seen_ts_utc"]) if existing else now
    con.execute(
        """
        INSERT INTO pm_signals(
          source, external_id, title, description, category, status, close_time_utc,
          blockchain_ref, token_ids_json, event_url, raw_json,
          first_seen_ts_utc, last_seen_ts_utc
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, external_id) DO UPDATE SET
          title=excluded.title,
          description=excluded.description,
          category=excluded.category,
          status=excluded.status,
          close_time_utc=excluded.close_time_utc,
          blockchain_ref=excluded.blockchain_ref,
          token_ids_json=excluded.token_ids_json,
          event_url=excluded.event_url,
          raw_json=excluded.raw_json,
          last_seen_ts_utc=excluded.last_seen_ts_utc
        """,
        (
            source,
            external_id,
            title,
            row.get("description"),
            row.get("category"),
            str(row.get("status") or "unknown"),
            row.get("close_time_utc"),
            row.get("blockchain_ref"),
            json.dumps(row.get("token_ids") or []),
            row.get("event_url"),
            json.dumps(row.get("raw") or {}),
            first_seen,
            now,
        ),
    )
    con.commit()
    cur = con.execute(
        "SELECT id FROM pm_signals WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    return int(cur["id"])


def insert_snapshot(con: sqlite3.Connection, *, signal_id: int, snap: Dict[str, Any]) -> bool:
    """Insert a snapshot. Returns False if (signal_id, asof_ts_utc) already exists."""
    cur = con.execute(
        """
        INSERT OR IGNORE INTO pm_signal_snapshots(
          signal_id, asof_ts_utc, yes_price, no_price, volume, volume_24h, liquidity, transaction_volume, trade_count
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(signal_id),
            str(snap.get("asof_ts_utc") or _utcnow_iso()),
            snap.get("yes_price"),
            snap.get("no_price"),
            snap.get("volume"),
            snap.get("volume_24h"),
            snap.get("liquidity"),
            snap.get("transaction_volume"),
            snap.get("trade_count"),
        ),
    )
    con.commit()
    return int(cur.rowcount or 0) > 0


def upsert_analytics(con: sqlite3.Connection, signal_id: int, analytics: Dict[str, Any]) -> None:
    con.execute(
        """
        INSERT INTO pm_signal_analytics(
          signal_id, is_time_sensitive, time_sensitive_reason, days_to_close_at_first,
          entry_yes_price, latest_yes_price, settlement_result, settlement_yes_value,
          signal_won, profit_if_followed, amount_won, amount_lost,
          wins, losses, pending, win_rate,
          volume_total, volume_24h, transaction_volume, trade_count, relevance_score,
          updated_ts_utc
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signal_id) DO UPDATE SET
          is_time_sensitive=excluded.is_time_sensitive,
          time_sensitive_reason=excluded.time_sensitive_reason,
          days_to_close_at_first=excluded.days_to_close_at_first,
          entry_yes_price=excluded.entry_yes_price,
          latest_yes_price=excluded.latest_yes_price,
          settlement_result=excluded.settlement_result,
          settlement_yes_value=excluded.settlement_yes_value,
          signal_won=excluded.signal_won,
          profit_if_followed=excluded.profit_if_followed,
          amount_won=excluded.amount_won,
          amount_lost=excluded.amount_lost,
          wins=excluded.wins,
          losses=excluded.losses,
          pending=excluded.pending,
          win_rate=excluded.win_rate,
          volume_total=excluded.volume_total,
          volume_24h=excluded.volume_24h,
          transaction_volume=excluded.transaction_volume,
          trade_count=excluded.trade_count,
          relevance_score=excluded.relevance_score,
          updated_ts_utc=excluded.updated_ts_utc
        """,
        (
            int(signal_id),
            1 if analytics.get("is_time_sensitive") else 0,
            analytics.get("time_sensitive_reason"),
            analytics.get("days_to_close_at_first"),
            analytics.get("entry_yes_price"),
            analytics.get("latest_yes_price"),
            analytics.get("settlement_result"),
            analytics.get("settlement_yes_value"),
            1 if analytics.get("signal_won") else (0 if analytics.get("signal_won") is False else None),
            analytics.get("profit_if_followed"),
            analytics.get("amount_won"),
            analytics.get("amount_lost"),
            int(analytics.get("wins") or 0),
            int(analytics.get("losses") or 0),
            int(analytics.get("pending") or 0),
            analytics.get("win_rate"),
            analytics.get("volume_total"),
            analytics.get("volume_24h"),
            analytics.get("transaction_volume"),
            analytics.get("trade_count"),
            analytics.get("relevance_score"),
            str(analytics.get("updated_ts_utc") or _utcnow_iso()),
        ),
    )
    con.commit()


def record_source_sync(con: sqlite3.Connection, source: str, *, markets_fetched: int, meta: Optional[Dict[str, Any]] = None) -> None:
    con.execute(
        """
        INSERT INTO pm_source_sync(source, last_sync_ts_utc, markets_fetched, meta_json)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
          last_sync_ts_utc=excluded.last_sync_ts_utc,
          markets_fetched=excluded.markets_fetched,
          meta_json=excluded.meta_json
        """,
        (source, _utcnow_iso(), int(markets_fetched), json.dumps(meta or {})),
    )
    con.commit()


def query_signals(
    con: sqlite3.Connection,
    *,
    source: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    where = ["1=1"]
    params: List[Any] = []
    if source:
        where.append("s.source = ?")
        params.append(str(source).strip().lower())
    if status:
        where.append("s.status = ?")
        params.append(str(status).strip().lower())
    params.extend([int(limit), int(offset)])
    sql = f"""
      SELECT s.*,
             a.is_time_sensitive, a.time_sensitive_reason, a.days_to_close_at_first,
             a.entry_yes_price, a.latest_yes_price, a.settlement_result, a.settlement_yes_value,
             a.signal_won, a.profit_if_followed, a.amount_won, a.amount_lost,
             a.wins, a.losses, a.pending, a.win_rate,
             a.volume_total, a.volume_24h, a.transaction_volume, a.trade_count, a.relevance_score,
             a.updated_ts_utc AS analytics_updated_ts_utc
      FROM pm_signals s
      LEFT JOIN pm_signal_analytics a ON a.signal_id = s.id
      WHERE {' AND '.join(where)}
      ORDER BY COALESCE(a.relevance_score, 0) DESC, COALESCE(a.volume_24h, 0) DESC, s.last_seen_ts_utc DESC
      LIMIT ? OFFSET ?
    """
    rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    for r in rows:
        try:
            r["token_ids"] = json.loads(r.pop("token_ids_json") or "[]")
        except Exception:
            r["token_ids"] = []
        try:
            r["raw"] = json.loads(r.pop("raw_json") or "{}")
        except Exception:
            r["raw"] = {}
        r["is_time_sensitive"] = bool(r.get("is_time_sensitive"))
    return rows


def query_sync_status(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    cur = con.execute("SELECT source, last_sync_ts_utc, markets_fetched, meta_json FROM pm_source_sync ORDER BY source")
    out = []
    for r in cur.fetchall():
        d = dict(r)
        try:
            d["meta"] = json.loads(d.pop("meta_json") or "{}")
        except Exception:
            d["meta"] = {}
        out.append(d)
    return out


def count_signals(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT COUNT(*) AS c FROM pm_signals").fetchone()
    return int(row["c"]) if row else 0


def get_first_snapshot(con: sqlite3.Connection, signal_id: int) -> Optional[Dict[str, Any]]:
    row = con.execute(
        """
        SELECT * FROM pm_signal_snapshots
        WHERE signal_id = ?
        ORDER BY asof_ts_utc ASC
        LIMIT 1
        """,
        (int(signal_id),),
    ).fetchone()
    return dict(row) if row else None


def get_snapshots_in_window(con: sqlite3.Connection, signal_id: int, *, hours: int = 24) -> List[Dict[str, Any]]:
    rows = con.execute(
        """
        SELECT * FROM pm_signal_snapshots
        WHERE signal_id = ?
        ORDER BY asof_ts_utc ASC
        LIMIT 500
        """,
        (int(signal_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def repair_polymarket_event_urls(con: sqlite3.Connection) -> int:
    """Rewrite broken ``/event/{market_slug}`` URLs using market slug from ``raw_json``."""
    rows = con.execute(
        "SELECT id, event_url, raw_json FROM pm_signals WHERE source = 'polymarket'"
    ).fetchall()
    fixed = 0
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except Exception:
            continue
        slug = str(raw.get("slug") or "").strip() or None
        events = raw.get("events") if isinstance(raw.get("events"), list) else []
        event0 = events[0] if events and isinstance(events[0], dict) else {}
        event_slug = str(event0.get("slug") or "").strip() or None
        # /market/{slug} redirects to the correct event page; /event/{marketSlug} often 404s.
        if slug:
            new_url = f"https://polymarket.com/market/{slug}"
        elif event_slug:
            new_url = f"https://polymarket.com/event/{event_slug}"
        else:
            continue
        old = str(row["event_url"] or "")
        if old != new_url:
            con.execute("UPDATE pm_signals SET event_url = ? WHERE id = ?", (new_url, int(row["id"])))
            fixed += 1
    con.commit()
    return fixed


def delete_signals_not_in_categories(con: sqlite3.Connection, allowlist: Sequence[str]) -> Dict[str, int]:
    """Delete signals whose category/title do not match the allowlist (cascades snapshots)."""
    from prediction_markets_categories import infer_categories, matches_allowlist

    rows = con.execute("SELECT id, title, category, raw_json FROM pm_signals").fetchall()
    drop_ids: List[int] = []
    keep = 0
    for row in rows:
        tags: List[str] = []
        raw_category = row["category"]
        try:
            raw = json.loads(row["raw_json"] or "{}")
            if isinstance(raw, dict):
                events = raw.get("events") if isinstance(raw.get("events"), list) else []
                event0 = events[0] if events and isinstance(events[0], dict) else {}
                for t in event0.get("tags") or []:
                    if isinstance(t, dict) and t.get("label"):
                        tags.append(str(t["label"]))
                    elif isinstance(t, str):
                        tags.append(t)
                if not raw_category:
                    raw_category = raw.get("category") or event0.get("category")
                # Kalshi series-style fields sometimes on the market payload.
                if raw.get("category") and not tags:
                    tags.append(str(raw.get("category")))
        except Exception:
            pass
        cats = infer_categories(title=str(row["title"] or ""), category=raw_category, tags=tags)
        if row["category"]:
            for part in str(row["category"]).split(","):
                cats = sorted(set(cats) | set(infer_categories(category=part)))
        if matches_allowlist(cats, allowlist):
            # Persist inferred categories for display/filtering.
            if cats:
                con.execute(
                    "UPDATE pm_signals SET category = ? WHERE id = ?",
                    (",".join(cats), int(row["id"])),
                )
            keep += 1
        else:
            drop_ids.append(int(row["id"]))
    for sid in drop_ids:
        con.execute("DELETE FROM pm_signal_analytics WHERE signal_id = ?", (sid,))
        con.execute("DELETE FROM pm_signal_snapshots WHERE signal_id = ?", (sid,))
        con.execute("DELETE FROM pm_signals WHERE id = ?", (sid,))
    con.commit()
    return {"deleted_signals": len(drop_ids), "kept_signals": keep}

