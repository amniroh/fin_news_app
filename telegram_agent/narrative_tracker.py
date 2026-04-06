"""Narrative tracker: turn news items into horizon-based narrative reports."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from telegram_agent.agent_db import (
    connect,
    init_db,
    fetch_news_rows_between,
    top_mentioned_symbols,
    get_liquidity_cache,
    upsert_liquidity_cache,
    store_horizon_report,
)
from telegram_agent.config import DEFAULT_LLM_MODEL
from telegram_agent.summarizer import _get_openrouter_client

logger = logging.getLogger(__name__)

Horizon = Literal["hourly", "daily", "weekly", "monthly", "annual"]


SOURCE_SPECIALTY = {
    # on-chain confirmation
    "whale_alert": "onchain",
    "lookonchain": "onchain",
    # asia early warning
    "wu_blockchain": "asia",
    # mainstream
    "bloomberg": "mainstream",
    # macro ground truth
    "sec": "macro",
    "edgar": "macro",
    "federalreserve": "macro",
    "federal reserve": "macro",
    "fed": "macro",
}


def _classify_source(name: str) -> str:
    n = (name or "").strip().lower()
    for k, v in SOURCE_SPECIALTY.items():
        if k in n:
            return v
    return "other"


def _window_for_horizon(now: datetime, horizon: Horizon) -> Tuple[datetime, datetime, datetime, datetime]:
    """
    Returns (start, end, prev_start, prev_end) for comparing frequency.
    """
    end = now
    if horizon == "hourly":
        start = end - timedelta(hours=1)
        prev_end = start
        prev_start = prev_end - timedelta(hours=1)
    elif horizon == "daily":
        start = end - timedelta(days=1)
        prev_end = start
        prev_start = prev_end - timedelta(days=1)
    elif horizon == "weekly":
        start = end - timedelta(days=7)
        prev_end = start
        prev_start = prev_end - timedelta(days=7)
    elif horizon == "monthly":
        start = end - timedelta(days=30)
        prev_end = start
        prev_start = prev_end - timedelta(days=30)
    else:
        start = end - timedelta(days=365)
        prev_end = start
        prev_start = prev_end - timedelta(days=365)
    return start, end, prev_start, prev_end


def _liquidity_class_for_symbol(con, symbol: str) -> Dict[str, Any]:
    """
    Best-effort liquidity classification:
    - Known majors => liquid
    - Equities: use yfinance info marketCap and averageVolume*price proxy when available
    Cache results in liquidity_cache.
    """
    sym = symbol.strip().upper()
    cached = get_liquidity_cache(con, sym)
    if cached and cached["liquidity_class"]:
        return {
            "symbol": sym,
            "liquidity_class": cached["liquidity_class"],
            "market_cap_usd": cached["market_cap_usd"],
            "avg_volume_usd": cached["avg_volume_usd"],
            "source": cached["source"],
        }

    majors = {"BTC-USD", "ETH-USD", "SPY", "QQQ", "DXY", "GLD", "SLV", "^GSPC", "^NDX", "^TNX", "USO"}
    if sym in majors:
        upsert_liquidity_cache(
            con,
            symbol=sym,
            liquidity_class="liquid",
            market_cap_usd=None,
            avg_volume_usd=None,
            source="static",
        )
        return {"symbol": sym, "liquidity_class": "liquid", "market_cap_usd": None, "avg_volume_usd": None, "source": "static"}

    # yfinance lookup (can be slow; keep it minimal)
    try:
        import yfinance as yf

        info = yf.Ticker(sym).fast_info or {}
        mcap = info.get("market_cap") or info.get("marketCap")
        last = info.get("last_price") or info.get("lastPrice")
        av = info.get("ten_day_average_volume") or info.get("tenDayAverageVolume") or info.get("averageVolume")
        avg_usd = None
        if last and av:
            avg_usd = float(last) * float(av)
        liq = "unknown"
        # crude thresholds
        if (mcap and float(mcap) >= 10_000_000_000) or (avg_usd and avg_usd >= 200_000_000):
            liq = "liquid"
        elif (mcap and float(mcap) <= 2_000_000_000) or (avg_usd and avg_usd <= 20_000_000):
            liq = "illiquid"
        upsert_liquidity_cache(
            con,
            symbol=sym,
            liquidity_class=liq,
            market_cap_usd=float(mcap) if mcap is not None else None,
            avg_volume_usd=float(avg_usd) if avg_usd is not None else None,
            source="yfinance_fast_info",
        )
        return {"symbol": sym, "liquidity_class": liq, "market_cap_usd": mcap, "avg_volume_usd": avg_usd, "source": "yfinance_fast_info"}
    except Exception:
        upsert_liquidity_cache(
            con,
            symbol=sym,
            liquidity_class="unknown",
            market_cap_usd=None,
            avg_volume_usd=None,
            source="unknown",
        )
        return {"symbol": sym, "liquidity_class": "unknown", "market_cap_usd": None, "avg_volume_usd": None, "source": "unknown"}


def _build_narrative_prompt(
    *,
    horizon: Horizon,
    start: datetime,
    end: datetime,
    prev_start: datetime,
    prev_end: datetime,
    recent_rows: List[dict],
    prev_rows: List[dict],
    symbol_stats: List[Dict[str, Any]],
) -> Tuple[str, str]:
    system = f"""You are a narrative tracker for markets. News items are narrative data points, not trade triggers.

You must output ONLY JSON (no markdown) with this schema:
{{
  "horizon": "{horizon}",
  "header": "<1-2 lines that explicitly say this is awareness/context, not action>",
  "themes": [
    {{
      "theme": "<short name>",
      "summary": "<1-3 sentences, neutral>",
      "frequency": {{"current": <int>, "previous": <int>, "delta": <int>}},
      "sources": {{"onchain": <int>, "asia": <int>, "mainstream": <int>, "macro": <int>, "other": <int>}},
      "assets": [
        {{
          "symbol": "<ticker/pair/index>",
          "liquidity_class": "liquid" | "illiquid" | "unknown",
          "status": "emerging" | "building" | "maturing",
          "confidence": "single_source" | "multi_source_cluster" | "cross_asset_confirmed",
          "note": "<priced-in/context-only label for liquid assets, or why illiquid may be slower to price in>"
        }}
      ],
      "downstream_assets": [
        {{
          "symbol": "<2nd order affected asset>",
          "why": "<short causal link>"
        }}
      ]
    }}
  ]
}}

Rules:
- Treat SEC EDGAR + Fed RSS as macro ground truth (weight highest for equities/rates narratives).
- Treat Whale Alert + Lookonchain as on-chain confirmation; themes are stronger when both appear.
- Treat Wu Blockchain as early Asia signal; if Bloomberg echoes later, status becomes maturing.
- Liquidity filter: for liquid assets, include but label as \"likely priced in — context only\".
- No speculation beyond what the sources imply.
- Produce 3–8 themes depending on density; fewer is fine if weak."""

    def fmt(rows: List[dict]) -> str:
        out = []
        for r in rows:
            out.append(
                f"- [{r['ts_utc']}] ({r['bucket']}) {r['source_name']}: {r['title']}\n  {r['content']}"
            )
        return "\n".join(out)

    user = f"""Window:
current: {start.isoformat()} → {end.isoformat()}
previous: {prev_start.isoformat()} → {prev_end.isoformat()}

Current items (sample):
{fmt(recent_rows)}

Previous-window items (sample):
{fmt(prev_rows)}

Mentioned assets with liquidity classification hints:
{json.dumps(symbol_stats, ensure_ascii=False)}

Return JSON only."""
    return system, user


def generate_horizon_report(cfg: dict, horizon: Horizon) -> str:
    cfg = {**cfg}
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)

    now = datetime.now(timezone.utc)
    start, end, prev_start, prev_end = _window_for_horizon(now, horizon)

    # sample windows
    rows_now = fetch_news_rows_between(con, start, end, limit=260)
    rows_prev = fetch_news_rows_between(con, prev_start, prev_end, limit=260)

    def to_dict(rows) -> List[dict]:
        out: List[dict] = []
        for r in rows[:160]:
            bucket = _classify_source(r["source_name"])
            out.append(
                {
                    "id": r["id"],
                    "ts_utc": r["ts_utc"],
                    "source_name": r["source_name"],
                    "bucket": bucket,
                    "title": (r["title"] or "")[:180],
                    "content": (r["condensed"] or r["content"] or "")[:550],
                }
            )
        return out

    # Use top mentioned symbols as anchors for liquidity + asset lists.
    top_syms = top_mentioned_symbols(con, limit=30)
    sym_stats: List[Dict[str, Any]] = []
    for sym, cnt in top_syms:
        liq = _liquidity_class_for_symbol(con, sym)
        sym_stats.append({"symbol": sym, "mentions": cnt, **liq})

    system, user = _build_narrative_prompt(
        horizon=horizon,
        start=start,
        end=end,
        prev_start=prev_start,
        prev_end=prev_end,
        recent_rows=to_dict(rows_now),
        prev_rows=to_dict(rows_prev),
        symbol_stats=sym_stats,
    )

    client = _get_openrouter_client()
    if not client:
        con.close()
        return "[No OpenRouter configured: set OPENROUTER_API_KEY]"

    model = cfg.get("agent_research_model") or cfg.get("llm_model", DEFAULT_LLM_MODEL)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=3500,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        con.close()
        return f"[Narrative LLM failed: {e}]"

    # Parse JSON for storage; also store pretty text view.
    report_json: Optional[Dict[str, Any]] = None
    raw2 = raw
    if raw2.startswith("```"):
        raw2 = raw2.split("\n", 1)[-1]
        raw2 = raw2.rsplit("```", 1)[0].strip()
    try:
        report_json = json.loads(raw2)
    except Exception:
        report_json = None

    # Create a readable plain text report.
    if isinstance(report_json, dict):
        header = str(report_json.get("header") or "").strip()
        themes = report_json.get("themes") or []
        lines: List[str] = []
        lines.append(f"🧭 Narrative Tracker — {horizon.upper()} (awareness, not action)")
        if header:
            lines.append(header)
        lines.append("")
        for t in themes:
            if not isinstance(t, dict):
                continue
            freq = t.get("frequency") or {}
            lines.append(f"== {t.get('theme','(theme)')} ==")
            lines.append(str(t.get("summary") or "").strip())
            try:
                lines.append(
                    f"Frequency: current={freq.get('current')} prev={freq.get('previous')} delta={freq.get('delta')}"
                )
            except Exception:
                pass
            assets = t.get("assets") or []
            if assets:
                lines.append("Assets:")
                for a in assets[:8]:
                    if not isinstance(a, dict):
                        continue
                    lines.append(
                        f"- {a.get('symbol')} | liquidity={a.get('liquidity_class')} | status={a.get('status')} | confidence={a.get('confidence')} | {a.get('note','')}"
                    )
            downstream = t.get("downstream_assets") or []
            if downstream:
                lines.append("2nd-order:")
                for d in downstream[:6]:
                    if not isinstance(d, dict):
                        continue
                    lines.append(f"- {d.get('symbol')}: {d.get('why')}")
            lines.append("")
        report_text = "\n".join(lines).strip()
    else:
        report_text = raw.strip()

    store_horizon_report(
        con,
        horizon=horizon,
        start_utc=start,
        end_utc=end,
        report_text=report_text,
        report_json=report_json if isinstance(report_json, dict) else None,
        meta={"source_weights": True},
    )
    con.close()
    return report_text


def generate_all_horizons(cfg: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for h in ("hourly", "daily", "weekly", "monthly", "annual"):
        out[h] = generate_horizon_report(cfg, h)  # type: ignore[arg-type]
    return out

