from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS vm_users (
  user_id TEXT PRIMARY KEY,
  created_ts_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vm_watchlist (
  user_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  created_ts_utc TEXT NOT NULL,
  PRIMARY KEY (user_id, symbol),
  FOREIGN KEY(user_id) REFERENCES vm_users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vm_watchlist_user ON vm_watchlist(user_id);

CREATE TABLE IF NOT EXISTS vm_alert_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  metric TEXT NOT NULL,
  op TEXT NOT NULL,               -- lt|lte|gt|gte|eq|neq
  threshold REAL NOT NULL,
  cooldown_minutes INTEGER NOT NULL DEFAULT 240,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_state INTEGER,             -- 0/1, whether condition was met on last check
  last_triggered_ts_utc TEXT,
  created_ts_utc TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES vm_users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vm_alert_rules_user ON vm_alert_rules(user_id);
CREATE INDEX IF NOT EXISTS idx_vm_alert_rules_symbol ON vm_alert_rules(symbol);

CREATE TABLE IF NOT EXISTS vm_alert_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  rule_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  metric TEXT NOT NULL,
  op TEXT NOT NULL,
  threshold REAL NOT NULL,
  value REAL,
  triggered_ts_utc TEXT NOT NULL,
  meta_json TEXT,
  FOREIGN KEY(user_id) REFERENCES vm_users(user_id) ON DELETE CASCADE,
  FOREIGN KEY(rule_id) REFERENCES vm_alert_rules(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vm_alert_events_user_ts ON vm_alert_events(user_id, triggered_ts_utc);

-- Historical metric snapshots (e.g. quarterly/annual ratios) from a provider.
CREATE TABLE IF NOT EXISTS vm_metric_points (
  symbol TEXT NOT NULL,
  asof_date TEXT NOT NULL,         -- YYYY-MM-DD
  period TEXT NOT NULL,            -- annual|quarter|daily
  provider TEXT NOT NULL,          -- fmp|yfinance|...
  pe REAL,
  pb REAL,
  peg REAL,
  dividend_yield REAL,
  free_cash_flow_yield REAL,
  debt_to_equity REAL,
  roe REAL,
  current_ratio REAL,
  operating_margin REAL,
  ev_to_ebitda REAL,
  raw_json TEXT,
  fetched_ts_utc TEXT NOT NULL,
  PRIMARY KEY(symbol, asof_date, period, provider)
);
CREATE INDEX IF NOT EXISTS idx_vm_metric_points_symbol_date ON vm_metric_points(symbol, asof_date);

-- Underlying fundamentals snapshots used to compute daily metrics.
CREATE TABLE IF NOT EXISTS vm_fundamental_points (
  symbol TEXT NOT NULL,
  asof_date TEXT NOT NULL,         -- YYYY-MM-DD (statement column date)
  period TEXT NOT NULL,            -- annual|quarter
  provider TEXT NOT NULL,          -- yfinance|...
  revenue REAL,
  operating_income REAL,
  net_income REAL,
  eps REAL,
  ebitda REAL,
  equity REAL,
  debt REAL,
  current_assets REAL,
  current_liabilities REAL,
  cash REAL,
  free_cash_flow REAL,
  implied_shares REAL,
  raw_json TEXT,
  fetched_ts_utc TEXT NOT NULL,
  PRIMARY KEY(symbol, asof_date, period, provider)
);
CREATE INDEX IF NOT EXISTS idx_vm_fundamental_points_symbol_date ON vm_fundamental_points(symbol, asof_date);

-- Latest standard analytics snapshot for each symbol (DB-first website table reads).
CREATE TABLE IF NOT EXISTS vm_standard_metrics (
  symbol TEXT NOT NULL,
  provider TEXT NOT NULL,          -- yfinance|...
  fetched_ts_utc TEXT NOT NULL,
  pe REAL,
  pb REAL,
  peg REAL,
  dividend_yield REAL,
  free_cash_flow_yield REAL,
  debt_to_equity REAL,
  roe REAL,
  current_ratio REAL,
  operating_margin REAL,
  ev_to_ebitda REAL,
  total_return_1y REAL,
  total_return_3y REAL,
  total_return_5y REAL,
  total_return_10y REAL,
  high_52w REAL,
  low_52w REAL,
  range_position_52w REAL,
  ytd_return REAL,
  sharpe_ratio REAL,
  beta REAL,
  alpha REAL,
  volatility REAL,
  max_drawdown REAL,
  average_volume REAL,
  expense_ratio REAL,
  trailing_pe REAL,
  mean_rsi_7d REAL,
  mean_rsi_30d REAL,
  mean_rsi_3m REAL,
  mean_rsi_1y REAL,
  raw_json TEXT,
  PRIMARY KEY(symbol, provider)
);
CREATE INDEX IF NOT EXISTS idx_vm_standard_metrics_symbol ON vm_standard_metrics(symbol);
"""


@dataclass(frozen=True)
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


def upsert_metric_points(
    con: sqlite3.Connection,
    *,
    provider: str,
    period: str,
    points: List[Dict[str, Any]],
) -> int:
    """
    points entries must contain:
      symbol, asof_date (YYYY-MM-DD), fetched_ts_utc, and any metric columns.
    """
    if not points:
        return 0
    prov = str(provider).strip().lower()
    per = str(period).strip().lower()
    rows = []
    for p in points:
        sym = str(p.get("symbol") or "").strip().upper()
        d = str(p.get("asof_date") or "").strip()
        if not sym or not d:
            continue
        rows.append(
            (
                sym,
                d,
                per,
                prov,
                p.get("pe"),
                p.get("pb"),
                p.get("peg"),
                p.get("dividend_yield"),
                p.get("free_cash_flow_yield"),
                p.get("debt_to_equity"),
                p.get("roe"),
                p.get("current_ratio"),
                p.get("operating_margin"),
                p.get("ev_to_ebitda"),
                json.dumps(p.get("raw") or {}),
                str(p.get("fetched_ts_utc") or _utcnow_iso()),
            )
        )
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO vm_metric_points(
          symbol, asof_date, period, provider,
          pe, pb, peg, dividend_yield, free_cash_flow_yield,
          debt_to_equity, roe, current_ratio, operating_margin, ev_to_ebitda,
          raw_json, fetched_ts_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, asof_date, period, provider) DO UPDATE SET
          pe=excluded.pe,
          pb=excluded.pb,
          peg=excluded.peg,
          dividend_yield=excluded.dividend_yield,
          free_cash_flow_yield=excluded.free_cash_flow_yield,
          debt_to_equity=excluded.debt_to_equity,
          roe=excluded.roe,
          current_ratio=excluded.current_ratio,
          operating_margin=excluded.operating_margin,
          ev_to_ebitda=excluded.ev_to_ebitda,
          raw_json=excluded.raw_json,
          fetched_ts_utc=excluded.fetched_ts_utc
        """,
        rows,
    )
    con.commit()
    return len(rows)


def query_metric_points(
    con: sqlite3.Connection,
    *,
    symbols: List[str],
    start_date: Optional[str],
    end_date: Optional[str],
    provider: str,
    period: str,
) -> List[Dict[str, Any]]:
    prov = str(provider).strip().lower()
    per = str(period).strip().lower()
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not syms:
        return []
    ph = ",".join("?" * len(syms))
    where = [f"symbol IN ({ph})", "provider = ?", "period = ?"]
    params: List[Any] = list(syms) + [prov, per]
    if start_date:
        where.append("asof_date >= ?")
        params.append(str(start_date))
    if end_date:
        where.append("asof_date <= ?")
        params.append(str(end_date))
    sql = f"""
      SELECT symbol, asof_date, period, provider,
             pe, pb, peg, dividend_yield, free_cash_flow_yield,
             debt_to_equity, roe, current_ratio, operating_margin, ev_to_ebitda,
             raw_json, fetched_ts_utc
      FROM vm_metric_points
      WHERE {' AND '.join(where)}
      ORDER BY symbol ASC, asof_date ASC
    """
    cur = con.execute(sql, params)
    out: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        d = dict(r)
        try:
            d["raw"] = json.loads(d.pop("raw_json") or "{}")
        except Exception:
            d["raw"] = {}
        out.append(d)
    return out


def query_latest_daily_metric_points(
    con: sqlite3.Connection,
    *,
    symbols: List[str],
    provider: str,
) -> List[Dict[str, Any]]:
    """
    Latest period=daily row per symbol for the given provider (most recent asof_date).
    """
    prov = str(provider).strip().lower()
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not syms:
        return []
    ph = ",".join("?" * len(syms))
    sql = f"""
      SELECT m.symbol, m.asof_date, m.period, m.provider,
             m.pe, m.pb, m.peg, m.dividend_yield, m.free_cash_flow_yield,
             m.debt_to_equity, m.roe, m.current_ratio, m.operating_margin, m.ev_to_ebitda,
             m.raw_json, m.fetched_ts_utc
      FROM vm_metric_points m
      JOIN (
        SELECT symbol, MAX(asof_date) AS dmax
        FROM vm_metric_points
        WHERE period = 'daily'
          AND provider = ?
          AND symbol IN ({ph})
        GROUP BY symbol
      ) u ON m.symbol = u.symbol AND m.asof_date = u.dmax
      WHERE m.period = 'daily' AND m.provider = ?
    """
    params: List[Any] = [prov] + list(syms) + [prov]
    cur = con.execute(sql, params)
    out: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        d = dict(r)
        try:
            d["raw"] = json.loads(d.pop("raw_json") or "{}")
        except Exception:
            d["raw"] = {}
        out.append(d)
    return out


def query_yearly_metric_coverage(
    con: sqlite3.Connection,
    *,
    symbol: str,
    provider: str,
    start_date: str,
    end_date: str,
) -> List[Dict[str, Any]]:
    """
    Per calendar year: trading-day counts and non-null metric counts for daily vm_metric_points.
    """
    sym = str(symbol).strip().upper()
    prov = str(provider).strip().lower()
    metric_cols = (
        "pe",
        "pb",
        "peg",
        "dividend_yield",
        "free_cash_flow_yield",
        "debt_to_equity",
        "roe",
        "current_ratio",
        "operating_margin",
        "ev_to_ebitda",
    )
    agg_parts = ["COUNT(*) AS n_days"] + [
        f"SUM(CASE WHEN {c} IS NOT NULL THEN 1 ELSE 0 END) AS n_{c}" for c in metric_cols
    ]
    sql = f"""
      SELECT CAST(substr(asof_date, 1, 4) AS INTEGER) AS year,
             {", ".join(agg_parts)}
      FROM vm_metric_points
      WHERE symbol = ?
        AND provider = ?
        AND period = 'daily'
        AND asof_date >= ?
        AND asof_date <= ?
      GROUP BY substr(asof_date, 1, 4)
      ORDER BY year ASC
    """
    cur = con.execute(sql, (sym, prov, start_date, end_date))
    return [dict(r) for r in cur.fetchall()]


def upsert_fundamental_points(
    con: sqlite3.Connection,
    *,
    provider: str,
    period: str,
    points: List[Dict[str, Any]],
) -> int:
    """
    points entries must contain:
      symbol, asof_date (YYYY-MM-DD), fetched_ts_utc, and any fundamentals columns.
    """
    if not points:
        return 0
    prov = str(provider).strip().lower()
    per = str(period).strip().lower()
    rows = []
    for p in points:
        sym = str(p.get("symbol") or "").strip().upper()
        d = str(p.get("asof_date") or "").strip()
        if not sym or not d:
            continue
        rows.append(
            (
                sym,
                d,
                per,
                prov,
                p.get("revenue"),
                p.get("operating_income"),
                p.get("net_income"),
                p.get("eps"),
                p.get("ebitda"),
                p.get("equity"),
                p.get("debt"),
                p.get("current_assets"),
                p.get("current_liabilities"),
                p.get("cash"),
                p.get("free_cash_flow"),
                p.get("implied_shares"),
                json.dumps(p.get("raw") or {}),
                str(p.get("fetched_ts_utc") or _utcnow_iso()),
            )
        )
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO vm_fundamental_points(
          symbol, asof_date, period, provider,
          revenue, operating_income, net_income, eps, ebitda,
          equity, debt, current_assets, current_liabilities, cash,
          free_cash_flow, implied_shares,
          raw_json, fetched_ts_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, asof_date, period, provider) DO UPDATE SET
          revenue=excluded.revenue,
          operating_income=excluded.operating_income,
          net_income=excluded.net_income,
          eps=excluded.eps,
          ebitda=excluded.ebitda,
          equity=excluded.equity,
          debt=excluded.debt,
          current_assets=excluded.current_assets,
          current_liabilities=excluded.current_liabilities,
          cash=excluded.cash,
          free_cash_flow=excluded.free_cash_flow,
          implied_shares=excluded.implied_shares,
          raw_json=excluded.raw_json,
          fetched_ts_utc=excluded.fetched_ts_utc
        """,
        rows,
    )
    con.commit()
    return len(rows)


def query_fundamental_points(
    con: sqlite3.Connection,
    *,
    symbols: List[str],
    start_date: Optional[str],
    end_date: Optional[str],
    provider: str,
    period: str,
) -> List[Dict[str, Any]]:
    prov = str(provider).strip().lower()
    per = str(period).strip().lower()
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not syms:
        return []
    ph = ",".join("?" * len(syms))
    where = [f"symbol IN ({ph})", "provider = ?", "period = ?"]
    params: List[Any] = list(syms) + [prov, per]
    if start_date:
        where.append("asof_date >= ?")
        params.append(str(start_date))
    if end_date:
        where.append("asof_date <= ?")
        params.append(str(end_date))
    sql = f"""
      SELECT symbol, asof_date, period, provider,
             revenue, operating_income, net_income, eps, ebitda,
             equity, debt, current_assets, current_liabilities, cash,
             free_cash_flow, implied_shares,
             raw_json, fetched_ts_utc
      FROM vm_fundamental_points
      WHERE {' AND '.join(where)}
      ORDER BY symbol ASC, asof_date ASC
    """
    cur = con.execute(sql, params)
    out: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        d = dict(r)
        try:
            d["raw"] = json.loads(d.pop("raw_json") or "{}")
        except Exception:
            d["raw"] = {}
        out.append(d)
    return out


def upsert_standard_metrics(
    con: sqlite3.Connection,
    *,
    provider: str,
    rows: List[Dict[str, Any]],
) -> int:
    if not rows:
        return 0
    prov = str(provider).strip().lower()
    payload = []
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        payload.append(
            (
                sym,
                prov,
                str(r.get("fetched_ts_utc") or _utcnow_iso()),
                r.get("pe"),
                r.get("pb"),
                r.get("peg"),
                r.get("dividend_yield"),
                r.get("free_cash_flow_yield"),
                r.get("debt_to_equity"),
                r.get("roe"),
                r.get("current_ratio"),
                r.get("operating_margin"),
                r.get("ev_to_ebitda"),
                r.get("total_return_1y"),
                r.get("total_return_3y"),
                r.get("total_return_5y"),
                r.get("total_return_10y"),
                r.get("high_52w"),
                r.get("low_52w"),
                r.get("range_position_52w"),
                r.get("ytd_return"),
                r.get("sharpe_ratio"),
                r.get("beta"),
                r.get("alpha"),
                r.get("volatility"),
                r.get("max_drawdown"),
                r.get("average_volume"),
                r.get("expense_ratio"),
                r.get("trailing_pe"),
                r.get("mean_rsi_7d"),
                r.get("mean_rsi_30d"),
                r.get("mean_rsi_3m"),
                r.get("mean_rsi_1y"),
                json.dumps(r.get("raw") or {}),
            )
        )
    if not payload:
        return 0
    con.executemany(
        """
        INSERT INTO vm_standard_metrics(
          symbol, provider, fetched_ts_utc,
          pe, pb, peg, dividend_yield, free_cash_flow_yield, debt_to_equity, roe,
          current_ratio, operating_margin, ev_to_ebitda,
          total_return_1y, total_return_3y, total_return_5y, total_return_10y,
          high_52w, low_52w, range_position_52w, ytd_return,
          sharpe_ratio, beta, alpha, volatility, max_drawdown, average_volume,
          expense_ratio, trailing_pe, mean_rsi_7d, mean_rsi_30d, mean_rsi_3m, mean_rsi_1y,
          raw_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, provider) DO UPDATE SET
          fetched_ts_utc=excluded.fetched_ts_utc,
          pe=excluded.pe, pb=excluded.pb, peg=excluded.peg, dividend_yield=excluded.dividend_yield,
          free_cash_flow_yield=excluded.free_cash_flow_yield, debt_to_equity=excluded.debt_to_equity,
          roe=excluded.roe, current_ratio=excluded.current_ratio, operating_margin=excluded.operating_margin,
          ev_to_ebitda=excluded.ev_to_ebitda, total_return_1y=excluded.total_return_1y,
          total_return_3y=excluded.total_return_3y, total_return_5y=excluded.total_return_5y,
          total_return_10y=excluded.total_return_10y, high_52w=excluded.high_52w, low_52w=excluded.low_52w,
          range_position_52w=excluded.range_position_52w, ytd_return=excluded.ytd_return,
          sharpe_ratio=excluded.sharpe_ratio, beta=excluded.beta, alpha=excluded.alpha,
          volatility=excluded.volatility, max_drawdown=excluded.max_drawdown, average_volume=excluded.average_volume,
          expense_ratio=excluded.expense_ratio, trailing_pe=excluded.trailing_pe,
          mean_rsi_7d=excluded.mean_rsi_7d, mean_rsi_30d=excluded.mean_rsi_30d,
          mean_rsi_3m=excluded.mean_rsi_3m, mean_rsi_1y=excluded.mean_rsi_1y,
          raw_json=excluded.raw_json
        """,
        payload,
    )
    con.commit()
    return len(payload)


def query_standard_metrics(
    con: sqlite3.Connection,
    *,
    symbols: List[str],
    provider: str,
) -> List[Dict[str, Any]]:
    prov = str(provider).strip().lower()
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not syms:
        return []
    ph = ",".join("?" * len(syms))
    cur = con.execute(
        f"""
        SELECT *
        FROM vm_standard_metrics
        WHERE provider = ? AND symbol IN ({ph})
        ORDER BY symbol
        """,
        [prov] + syms,
    )
    out: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        d = dict(r)
        try:
            d["raw"] = json.loads(d.pop("raw_json") or "{}")
        except Exception:
            d["raw"] = {}
        out.append(d)
    return out


def ensure_user(con: sqlite3.Connection, user_id: str) -> None:
    uid = str(user_id).strip()
    con.execute(
        "INSERT OR IGNORE INTO vm_users(user_id, created_ts_utc) VALUES(?, ?)",
        (uid, _utcnow_iso()),
    )
    con.commit()


def get_watchlist(con: sqlite3.Connection, user_id: str) -> List[str]:
    uid = str(user_id).strip()
    cur = con.execute("SELECT symbol FROM vm_watchlist WHERE user_id = ? ORDER BY symbol", (uid,))
    return [str(r["symbol"]) for r in cur.fetchall()]


def add_to_watchlist(con: sqlite3.Connection, user_id: str, symbols: Sequence[str]) -> int:
    uid = str(user_id).strip()
    ensure_user(con, uid)
    rows = [(uid, str(s).strip().upper(), _utcnow_iso()) for s in symbols if str(s).strip()]
    if not rows:
        return 0
    con.executemany(
        "INSERT OR IGNORE INTO vm_watchlist(user_id, symbol, created_ts_utc) VALUES(?, ?, ?)",
        rows,
    )
    con.commit()
    return int(con.total_changes)


def remove_from_watchlist(con: sqlite3.Connection, user_id: str, symbols: Sequence[str]) -> int:
    uid = str(user_id).strip()
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not syms:
        return 0
    con.executemany(
        "DELETE FROM vm_watchlist WHERE user_id = ? AND symbol = ?",
        [(uid, s) for s in syms],
    )
    con.commit()
    return int(con.total_changes)


def list_all_watchlist_symbols(con: sqlite3.Connection) -> List[str]:
    cur = con.execute("SELECT DISTINCT symbol FROM vm_watchlist ORDER BY symbol")
    return [str(r["symbol"]) for r in cur.fetchall()]


def create_alert_rule(
    con: sqlite3.Connection,
    *,
    user_id: str,
    symbol: str,
    metric: str,
    op: str,
    threshold: float,
    cooldown_minutes: int = 240,
    enabled: bool = True,
) -> int:
    uid = str(user_id).strip()
    ensure_user(con, uid)
    con.execute(
        """
        INSERT INTO vm_alert_rules(
          user_id, symbol, metric, op, threshold, cooldown_minutes, enabled,
          last_state, last_triggered_ts_utc, created_ts_utc
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        """,
        (
            uid,
            str(symbol).strip().upper(),
            str(metric).strip(),
            str(op).strip(),
            float(threshold),
            int(cooldown_minutes),
            1 if enabled else 0,
            _utcnow_iso(),
        ),
    )
    con.commit()
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def list_alert_rules(con: sqlite3.Connection, user_id: str) -> List[Dict[str, Any]]:
    uid = str(user_id).strip()
    cur = con.execute(
        """
        SELECT * FROM vm_alert_rules
        WHERE user_id = ?
        ORDER BY enabled DESC, id DESC
        """,
        (uid,),
    )
    return [dict(r) for r in cur.fetchall()]


def set_alert_rule_enabled(con: sqlite3.Connection, rule_id: int, enabled: bool) -> None:
    con.execute("UPDATE vm_alert_rules SET enabled = ? WHERE id = ?", (1 if enabled else 0, int(rule_id)))
    con.commit()


def delete_alert_rule(con: sqlite3.Connection, rule_id: int) -> None:
    con.execute("DELETE FROM vm_alert_rules WHERE id = ?", (int(rule_id),))
    con.commit()


def list_enabled_rules(con: sqlite3.Connection) -> List[sqlite3.Row]:
    cur = con.execute("SELECT * FROM vm_alert_rules WHERE enabled = 1")
    return list(cur.fetchall())


def update_rule_state(
    con: sqlite3.Connection,
    *,
    rule_id: int,
    last_state: int,
    last_triggered_ts_utc: Optional[str],
) -> None:
    con.execute(
        "UPDATE vm_alert_rules SET last_state = ?, last_triggered_ts_utc = ? WHERE id = ?",
        (int(last_state), last_triggered_ts_utc, int(rule_id)),
    )
    con.commit()


def insert_alert_event(
    con: sqlite3.Connection,
    *,
    user_id: str,
    rule_id: int,
    symbol: str,
    metric: str,
    op: str,
    threshold: float,
    value: Optional[float],
    meta: Optional[Dict[str, Any]] = None,
) -> int:
    uid = str(user_id).strip()
    con.execute(
        """
        INSERT INTO vm_alert_events(
          user_id, rule_id, symbol, metric, op, threshold, value, triggered_ts_utc, meta_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            int(rule_id),
            str(symbol).strip().upper(),
            str(metric).strip(),
            str(op).strip(),
            float(threshold),
            float(value) if value is not None else None,
            _utcnow_iso(),
            json.dumps(meta or {}),
        ),
    )
    con.commit()
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def list_alert_events(con: sqlite3.Connection, user_id: str, *, limit: int = 200) -> List[Dict[str, Any]]:
    uid = str(user_id).strip()
    cur = con.execute(
        """
        SELECT * FROM vm_alert_events
        WHERE user_id = ?
        ORDER BY triggered_ts_utc DESC, id DESC
        LIMIT ?
        """,
        (uid, int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]

