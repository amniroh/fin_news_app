"""List stored suggestions (recommendations) from the agent DB."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


def _safe_json(s: str):
    try:
        return json.loads(s or "{}")
    except json.JSONDecodeError:
        return {}


def main() -> None:
    # Match agent.py env loading
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")

    from telegram_agent.config import load_config
    from telegram_agent.agent_db import connect, init_db

    cfg = load_config()
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)

    cur = con.execute(
        """
        SELECT id, ts_utc, symbol, confidence, forecast_pct,
               suggestion_ts_utc, entry_window_start_utc, entry_window_end_utc, execute_review_utc,
               rationale, meta_json
        FROM recommendations
        ORDER BY ts_utc DESC, id DESC
        """
    )
    rows = cur.fetchall()
    print(f"recommendations={len(rows)}")
    for r in rows:
        meta = _safe_json(r["meta_json"] or "{}")
        tester = meta.get("tester")
        plan = meta.get("plan") or {}
        what = plan.get("what_to_acquire") or ""
        what = (what[:140] + "…") if len(what) > 140 else what
        rat = (r["rationale"] or "")[:140]
        print(
            json.dumps(
                {
                    "id": r["id"],
                    "ts_utc": r["ts_utc"],
                    "symbol": r["symbol"],
                    "confidence": r["confidence"],
                    "forecast_pct": r["forecast_pct"],
                    "suggestion_ts_utc": r["suggestion_ts_utc"],
                    "entry_window_start_utc": r["entry_window_start_utc"],
                    "entry_window_end_utc": r["entry_window_end_utc"],
                    "execute_review_utc": r["execute_review_utc"],
                    "what_to_acquire": what,
                    "rationale_head": rat,
                    "tester_present": bool(tester),
                },
                default=str,
                ensure_ascii=False,
            )
        )

    con.close()


if __name__ == "__main__":
    main()

