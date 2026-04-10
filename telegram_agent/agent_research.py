"""Deep research: LLM synthesizes news + memory, updates structured memory, stores concrete plans."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from telegram_agent.agent_db import (
    connect,
    init_db,
    count_memories_before,
    fetch_news_rows_between,
    fetch_news_rows_between_universe_linked,
    fetch_news_rows_calendar_day_utc,
    fetch_news_rows_calendar_day_universe_linked,
    list_recommendations_with_tester_for_prompt,
    top_mentioned_symbols,
    top_mentioned_symbols_for_news_window,
    top_mentioned_symbols_linkage_range,
    get_close_at_or_before,
    insert_recommendation,
    latest_memory,
    latest_memory_before,
    upsert_memory,
)
from telegram_agent.config import DEFAULT_LLM_MODEL
from telegram_agent.cost_estimate import estimate_research_cost
from telegram_agent.memory_structured import (
    parse_memory_payload,
    format_memory_for_prompt,
    merge_memory_state,
    memory_meta_wrapper,
)
from telegram_agent.agent_tester import load_strategy_test_aggregate
from telegram_agent.research_publish import format_research_telegram_message, publish_research_to_target
from telegram_agent.summarizer import _get_openrouter_client
from telegram_agent.symbol_universe import load_symbol_universe, symbol_universe_set

logger = logging.getLogger(__name__)

_NEWS_BOILERPLATE_EXACT = {
    "this is a delayed news feed.",
    "details",
    "read analysis",
}


def _clean_news_text(source_name: str, title: str, content: str) -> str:
    """
    Clean noisy/boilerplate lines before putting news into the research prompt.
    Conservative: remove known repeated disclaimers + obvious CTA lines + dedupe repeats.
    """
    src = (source_name or "").strip().lower()
    t = (title or "").strip()
    c = (content or "").strip()

    if not c:
        return t

    lines_in = [ln.strip() for ln in c.splitlines() if ln.strip()]
    out_lines: List[str] = []
    prev_norm: Optional[str] = None
    for ln in lines_in:
        ln_norm = ln.lower()
        if ln_norm in _NEWS_BOILERPLATE_EXACT:
            continue
        if ln_norm.startswith("please check our premium product"):
            continue
        if "contact @giuseppefxl" in ln_norm:
            continue
        if t and ln == t:
            continue
        if prev_norm is not None and ln_norm == prev_norm:
            continue
        prev_norm = ln_norm
        out_lines.append(ln)

    if t.startswith("http") and out_lines and out_lines[0].startswith("http") and out_lines[0] == t:
        out_lines = out_lines[1:]

    cleaned = "\n".join(out_lines).strip()
    if not cleaned:
        return t

    # Whale-alert style posts are often repetitive; keep one-line payload.
    if src in ("whale_alert_io", "whale_alert"):
        cleaned = cleaned.replace("\n", " ").strip()

    return cleaned


def _epistemic_guidance_text(n_snapshots: int) -> str:
    """Instructions tied to how much research memory exists (calibration, not a hard rule)."""
    if n_snapshots <= 0:
        return (
            "**Epistemic stance:** This is an **early** run: **no** prior research memory snapshots exist yet. "
            "Do **not** take an assertive or highly confident tone. Treat the news sample as incomplete; "
            "avoid definitive claims. In `memory_update`, assign **low** trend confidences (**0–3**) unless "
            "a theme is almost trivially factual. In `thinking`, use cautious, hedged language."
        )
    if n_snapshots < 7:
        return (
            f"**Epistemic stance:** Only **{n_snapshots}** prior research snapshot(s) — **limited** longitudinal "
            "validation. Stay measured: most new or updated trends should remain **0–5** until repeated "
            "confirmation across days. Raise confidences only when news, prices, and (if present) tester outcomes align."
        )
    if n_snapshots < 60:
        return (
            f"**Epistemic stance:** **{n_snapshots}** prior snapshots — **growing** evidence. You may **raise** "
            "confidence for themes that recur in news and are consistent with price context and (when available) "
            "tester results. Treat **9** as strong; reserve **10** for rare, repeatedly validated themes."
        )
    return (
        f"**Epistemic stance:** **{n_snapshots}+** prior snapshots — **substantial** history. High confidences "
        "are permissible only when **news + memory + tester backtests** (where present) reinforce the same thesis. "
        "**10** must remain **rare** (multi-window, repeated validation)."
    )


def _tester_rows_to_prompt_lines(rows: List[Any]) -> str:
    if not rows:
        return (
            "(No evaluated backtests in the database. After recommendations exist, run "
            "`python -m telegram_agent.agent test-suggestions` to fill `meta_json.tester`, then re-run research.)"
        )
    lines_out: List[str] = []
    for r in rows:
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        t = meta.get("tester") or {}
        sym = r["symbol"]
        rid = r["id"]
        parts = [f"id={rid} {sym}"]
        fc = r["forecast_pct"]
        if fc is not None:
            parts.append(f"forecast_pct={fc}")
        conf = r["confidence"]
        if conf is not None:
            parts.append(f"suggestion_conf={conf}")
        if t.get("realized_pct") is not None:
            parts.append(f"realized_pct={t['realized_pct']}")
        if t.get("entry_px") is not None:
            parts.append(f"entry_px={t['entry_px']}")
        if t.get("exit_px") is not None:
            parts.append(f"exit_px={t['exit_px']}")
        note = t.get("note")
        if note:
            parts.append(f"note={note}")
        lines_out.append(" | ".join(parts))
    return "\n".join(f"- {ln}" for ln in lines_out)


def _compact_strategy_aggregate(agg: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "n_legs",
        "benchmark_symbol",
        "risk_free_annual",
        "oos_split",
        "sharpe",
        "alpha_vs_benchmark_mean",
        "alpha_n_legs_with_benchmark",
        "max_drawdown",
        "sharpe_in_sample",
        "sharpe_out_of_sample",
        "oos_n_is",
        "oos_n_oos",
        "calmar",
        "mean_return_per_trade_pct",
        "significance_p_value_mean_return",
        "optimization_metric",
        "optimization_value",
        "evaluated_at",
        "note",
    )
    return {k: agg[k] for k in keys if k in agg}


def _strategy_optimization_feedback_for_prompt(cfg: dict, con) -> str:
    """
    Aggregate metrics from last `test-suggestions` run (kv_state), including the configured
    optimization scalar for steering research quality.
    """
    agg = load_strategy_test_aggregate(con)
    if not agg:
        return (
            "(No aggregate strategy-test metrics yet. Run `python -m telegram_agent.agent test-suggestions` "
            "after price history exists and strategy legs are stored in `recommendations`.)"
        )
    opt_k = str(cfg.get("test_optimization_metric") or "sharpe").strip()
    compact = _compact_strategy_aggregate(agg)
    head = (
        f"Configured optimization metric: {opt_k}. Field `optimization_value` is derived so that **higher is better** "
        f"(max drawdown is negated; p-values use (1-p) so higher means more significant). "
        f"Use this block as feedback when improving suggestion quality.\n"
    )
    return head + json.dumps(compact, indent=2, default=str)


@dataclass
class ResearchRunContext:
    """Live run uses wall-clock now + 14d news; daily backfill simulates one UTC calendar day."""

    sim_now: datetime
    daily_mode: bool
    # If daily_mode: news and mentions restricted to this UTC day [day_start, day_start+1d)
    day_start_utc: Optional[datetime] = None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    t = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _end_of_utc_day(day_start: datetime) -> datetime:
    return day_start + timedelta(days=1) - timedelta(seconds=1)


def _price_returns_pct_only(
    con, symbol: str, asof: datetime
) -> Dict[str, float]:
    """
    Return only horizons where we have two valid closes (percent change vs asof).
    Values are **percentage points** (e.g. 1.5 means +1.5%), comparable across tickers.
    Omits any horizon with missing prices — never None.
    """
    out: Dict[str, float] = {}
    for days, label in [(1, "1d"), (5, "5d"), (30, "30d")]:
        t0 = asof - timedelta(days=days)
        c0 = get_close_at_or_before(con, symbol, t0)
        c1 = get_close_at_or_before(con, symbol, asof)
        if c0 and c1 and c0 > 0:
            out[label] = round((c1 - c0) / c0 * 100.0, 3)
    return out


def _format_price_context_line(sym: str, pct_by_horizon: Dict[str, float]) -> str:
    """Single line: explicit % labels, only horizons present."""
    parts = [f"{h} {pct_by_horizon[h]:+.3f}%" for h in ("1d", "5d", "30d") if h in pct_by_horizon]
    body = "  ".join(parts) if parts else ""
    return f"{sym}: {body}"


def _cfg_for_research_universe(cfg: dict) -> dict:
    """
    Research-only universe priority override.
    If agent_research_max_priority is set, use it as max_priority when loading the universe
    and when filtering linkage/news for this run.
    """
    cfg2 = dict(cfg)
    if cfg2.get("agent_research_max_priority") is not None:
        try:
            cfg2["max_priority"] = int(cfg2["agent_research_max_priority"])
        except Exception:
            pass
    return cfg2


def _build_research_prompts(
    cfg: dict,
    con,
    *,
    min_confidence: float,
    ctx: ResearchRunContext,
) -> Tuple[str, str]:
    cfg = _cfg_for_research_universe(cfg)
    sim_now = ctx.sim_now
    if sim_now.tzinfo is None:
        sim_now = sim_now.replace(tzinfo=timezone.utc)
    sim_now = sim_now.astimezone(timezone.utc)

    lines: List[str] = []
    news_count = 0
    # Single knob: controls both fetch limit and prompt inclusion count.
    max_news = int(cfg.get("agent_research_max_num_ofnews", 200))
    max_news = max(0, min(5000, max_news))

    allowed_syms: Optional[Set[str]] = symbol_universe_set(cfg)
    universe_list: Optional[List[str]] = None
    if allowed_syms is not None:
        try:
            universe_list = load_symbol_universe(cfg)
        except Exception:
            universe_list = None
        if not universe_list:
            universe_list = sorted(allowed_syms)

    use_universe_linkage = (
        allowed_syms is not None
        and bool(cfg.get("agent_research_universe_news_linkage", True))
    )

    if ctx.daily_mode and ctx.day_start_utc is not None:
        day_start = ctx.day_start_utc
        if day_start.tzinfo is None:
            day_start = day_start.replace(tzinfo=timezone.utc)
        day_start = day_start.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_end_excl = day_start + timedelta(days=1)
        if use_universe_linkage and allowed_syms:
            news = fetch_news_rows_calendar_day_universe_linked(
                con, day_start, allowed_syms, limit=max_news
            )
            syms = top_mentioned_symbols_linkage_range(
                con,
                day_start,
                day_end_excl,
                allowed_syms,
                inclusive_end=False,
                limit=80,
            )
            news_scope = (
                f"BACKFILL (daily): only news on this UTC calendar day ({day_start.date().isoformat()}) "
                f"that are linked to at least one allowlist ticker (universe preprocessor). "
                f"{len(news)} row(s) after linkage filter."
            )
        else:
            news = fetch_news_rows_calendar_day_utc(con, day_start, limit=max_news)
            syms = top_mentioned_symbols_for_news_window(
                con, day_start, day_end_excl, limit=80
            )
            news_scope = f"BACKFILL (daily): only news items with timestamps on this UTC calendar day ({day_start.date().isoformat()}). {len(news)} row(s) in DB for that day."
        news_count = len(news)
        for r in news[:max_news]:
            t0 = str(r["title"] or "")
            c0 = str(r["content"] or "")
            t = t0[:120]
            c = _clean_news_text(str(r["source_name"] or ""), t0, c0)[:400]
            lines.append(f"- [{r['ts_utc']}] {r['source_name']}: {t}\n  {c}")
        # Latest snapshot strictly before simulated run time (not midnight of the day):
        # includes any prior run on this calendar day and matches tester / price asof semantics.
        mem_row = latest_memory_before(con, sim_now)
    else:
        start = sim_now - timedelta(days=14)
        if use_universe_linkage and allowed_syms:
            news = fetch_news_rows_between_universe_linked(
                con, start, sim_now, allowed_syms, limit=max_news
            )
            syms = top_mentioned_symbols_linkage_range(
                con, start, sim_now, allowed_syms, inclusive_end=True, limit=80
            )
            news_scope = (
                "LIVE: rolling 14d window; news items are **restricted to those linked to allowlist tickers** "
                f"(universe preprocessor). {len(news)} row(s) after linkage filter."
            )
        else:
            news = fetch_news_rows_between(con, start, sim_now, limit=max_news)
            syms = top_mentioned_symbols(con, limit=80)
            news_scope = "LIVE: rolling 14d news window (sample in prompt)."
        news_count = len(news)
        for r in news[:max_news]:
            t0 = str(r["title"] or "")
            c0 = str(r["content"] or "")
            t = t0[:120]
            c = _clean_news_text(str(r["source_name"] or ""), t0, c0)[:400]
            lines.append(f"- [{r['ts_utc']}] {r['source_name']}: {t}\n  {c}")
        mem_row = latest_memory(con)

    prev_struct = parse_memory_payload(
        (mem_row["text"] if mem_row else "") or "",
        (mem_row["meta_json"] if mem_row else None),
    )
    mem_cap = int(cfg.get("agent_memory_prompt_chars", 8000))
    mem_txt = format_memory_for_prompt(prev_struct, max_chars=mem_cap)

    if allowed_syms is not None:
        syms = [(s, c) for (s, c) in syms if s in allowed_syms]

    # Price context: every symbol in the priority-filtered universe (when universe mode is on),
    # else every symbol from the mention leaderboard. Order: news-linked symbols first, then
    # remaining universe names (same set as allowlist / max_priority research config).
    px_pairs: List[Tuple[str, int]] = []
    seen_px: set[str] = set()
    for s, c in syms:
        if s not in seen_px:
            seen_px.add(s)
            px_pairs.append((s, c))
    if allowed_syms is not None and universe_list:
        for u in universe_list:
            if u not in seen_px:
                seen_px.add(u)
                px_pairs.append((u, 0))

    px_lines: List[str] = []
    for sym, cnt in px_pairs:
        pct_map = _price_returns_pct_only(con, sym, sim_now)
        if not pct_map:
            continue
        px_lines.append(_format_price_context_line(sym, pct_map))

    universe_prompt_txt = ""
    if allowed_syms is not None and universe_list:
        joined = ", ".join(universe_list)
        max_u = 14000
        if len(joined) > max_u:
            universe_prompt_txt = (
                joined[:max_u]
                + f"\n... [truncated; {len(universe_list)} tickers total in universe — use only tickers from your configured universe]"
            )
        else:
            universe_prompt_txt = joined

    cap_s = int(cfg.get("agent_memory_cap_strongest", 20))
    cap_r = int(cfg.get("agent_memory_cap_recent", 20))
    sug_d = int(cfg.get("agent_memory_suggestion_days", 30))

    n_snapshots = count_memories_before(con, sim_now)

    tester_limit = int(cfg.get("agent_research_tester_prompt_limit", 50))
    tester_rows = list_recommendations_with_tester_for_prompt(
        con, before_ts_utc=sim_now, limit=tester_limit
    )
    tester_txt = _tester_rows_to_prompt_lines(tester_rows)
    strategy_opt_txt = _strategy_optimization_feedback_for_prompt(cfg, con)
    epistemic_user = _epistemic_guidance_text(n_snapshots)

    mode_extra = ""
    if ctx.daily_mode and ctx.day_start_utc is not None:
        d = ctx.day_start_utc.astimezone(timezone.utc).date().isoformat()
        mode_extra = f"""
- **Daily backfill:** You are simulating the market research desk at the **end of this UTC calendar day ({d})**. Only today's news (below) exists to you; prior memory is the **latest saved snapshot strictly before this simulated moment** (end of {d}) — if research was skipped for prior days, that memory may be days or weeks old. All trend labels, thinking, and suggestion timestamps must be consistent with a trader operating **on {d}** (no knowledge of future days).
- **suggestion_ts_utc** and plan windows must use dates **on or after** {d} as appropriate.
- **Confidence vs simulated calendar:** Early simulated days in a backfill behave like early real runs: keep trend confidences **low** until cumulative evidence (and in later days, tester results) supports higher scores."""

    system = f"""You are a disciplined market research agent.
Use plain language suitable for a smart general reader with no investing background — minimize jargon, define any unavoidable terms briefly, and keep trends/suggestions easy to understand.
You MUST output ONLY valid JSON (no markdown fences) with exactly this shape:
{{
  "thinking": "<string: your reasoning. Include (1) rising or emerging trends visible in the news sample, (2) for each important theme already in prior memory, whether evidence suggests it is STRENGTHENING, FADING, or UNCHANGED, (3) cross-check vs price context, (4) how **per-leg backtests** and **aggregate strategy metrics** (if any in the user prompt) support or undermine prior themes and suggestion quality.>",
  "memory_update": {{
    "strongest_trends": [
      {{ "text": "<concise trend line>", "confidence": <integer 0-10 — calibrated per rules below> }}
    ],
    "recent_trends": [
      {{ "text": "<concise trend line>", "confidence": <integer 0-10> }}
    ],
    "suggestions_log": [
      {{ "text": "<ISO8601 UTC prefix | SYMBOL | one-line summary>", "confidence": <integer 0-10> }}
    ]
  }},
  "suggestions": [
    {{
      "symbol": "TICKER_OR_ETF",
      "suggestion_ts_utc": "<ISO8601 — as-of date for the thesis (use historical dates when backfilling past opportunities)>",
      "entry_window_start_utc": "<ISO8601 — earliest intended execution>",
      "entry_window_end_utc": "<ISO8601 — latest intended execution / assumed fill>",
      "execute_review_utc": "<ISO8601 — when to close, roll, or reassess the position>",
      "what_to_acquire": "<specific: instrument, side, rough size idea, venue if relevant>",
      "forecast_pct": <number or null — optional expected move by execute_review horizon>,
      "confidence": <0..1 — calibrated: keep LOW when research history is thin or tester evidence is weak>,
      "rationale": "<why this works now>",
      "priced_in": "<how much is already in the price>",
      "novel_vs_memory": <true only if meaningfully new vs prior memory>,
      "memory_trend_alignment": "strengthening" | "fading" | "new" | "unchanged"
    }}
  ]
}}

Rules:
- **Trend / observation confidence (0–10 integer)** applies to `memory_update` entries only. **0–3**: early, weakly supported, or single-source. **4–6**: plausible, some corroboration. **7–8**: well supported across news + memory + prices and/or tester outcomes. **9**: strong multi-window validation. **10**: **rare** — reserve for themes validated repeatedly over time with little contradiction; do not hand out 10s on sparse history.
- **Suggestion `confidence` (0..1 float)** must align with the same epistemics: use **lower** values when few memory snapshots exist or tester data contradicts similar past ideas; raise only when evidence stacks.
- **Thinking** must explicitly compare NEWS vs MEMORY vs TESTER BACKTESTS (when listed), not only headlines.
- **memory_update** feeds a capped store: aim for up to ~{cap_s} strongest and ~{cap_r} recent trend objects; suggestions_log entries should be dated and compact (last ~{sug_d} days are retained by the system). When updating an existing theme, adjust **confidence** up or down with explicit justification in `thinking`.
- **No duration field** — use the four timestamps instead (backtests use entry_window and execute_review).
- **Concrete suggestions only**: every suggestion must have all timestamp fields AND what_to_acquire filled so a tester can simulate fills.
- **Novelty**: set novel_vs_memory true only for ideas you would persist; omit weak duplicates.
- **Persistence threshold**: only persist suggestion rows with confidence >= {min_confidence} (still obey calibration — if nothing meets the bar, return an empty suggestions array).
- **Ranking / limit**: sort `suggestions` by `confidence` (descending) and return **at most 5** suggestions total, all meeting the persistence threshold.
- **Symbols**: When **symbol universe mode is active** (see user prompt), every `suggestions[].symbol` MUST be **exactly** one ticker from the **allowlist** printed there — not merely “mentioned in news”. Prefer names that also appear in the news sample when relevant; if news is thin, you may still propose universe names supported by price/tester context.
- If nothing qualifies, return an empty suggestions array and still refresh memory_update + thinking.{mode_extra}"""

    if allowed_syms is not None:
        px_title = (
            "Recent return context (**all** symbols in the priority-filtered universe for this run — same tickers as the "
            "allowlist above; membership follows `max_priority` / `--max-priority` and the universe JSON) as of simulated time:"
        )
        px_order = "- Ordering: symbols tied to news mentions first, then the rest of the allowlist (same priority-filtered set)."
    else:
        px_title = (
            "Recent return context (all symbols from the news mention leaderboard for this run; symbol universe mode is **off**) "
            "as of simulated time:"
        )
        px_order = "- Ordering: by mention frequency (most-mentioned first)."

    news_filter_note = ""
    if use_universe_linkage and allowed_syms:
        news_filter_note = (
            "Only news rows that the **universe preprocessor** linked to at least one allowlist ticker are included "
            "(duplicates and unrelated headlines are dropped relative to the raw ingest stream).\n\n"
        )

    user = f"""Simulated current time (UTC) for this run: {sim_now.isoformat()}
News scope: {news_scope}

Research memory depth (snapshots completed **before** this run, used for calibration): **{n_snapshots}**

{epistemic_user}

=== SUGGESTION BACKTESTS (tester — realized outcomes vs plan; use to validate or downgrade confidence) ===
{tester_txt}

=== AGGREGATE STRATEGY METRICS (portfolio-style; optimization target for research feedback) ===
{strategy_opt_txt}

Prior structured memory (baseline — compare every idea against this; for daily backfill this is the state **before** the simulated day only):
{mem_txt}

News sample for this run:
{news_filter_note}{chr(10).join(lines) if lines else "(no news rows in scope)"}

{"=== SYMBOL UNIVERSE ALLOWLIST (use ONLY these tickers in suggestions[].symbol) ===\n" + universe_prompt_txt + "\n\n" if universe_prompt_txt else ""}{px_title}
- Values are **total return in percent** vs the prior close for each horizon (1d/5d/30d). Only horizons with price history are shown; symbols with no usable closes are omitted.
{px_order}
{chr(10).join(px_lines) if px_lines else "(no price context rows)"}

Symbol universe mode: {"ACTIVE — `suggestions[].symbol` must be chosen **only** from the allowlist block above (same set as price pipeline / extract)." if allowed_syms is not None else "OFF — you may suggest other liquid names if justified."}

Produce JSON only."""

    return system, user


def write_research_dry_run_prompt_file(rep: Dict[str, Any], path: Path) -> None:
    """Write full system + user prompts and a short metadata header (UTF-8)."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    st = rep["stats"]
    header = (
        f"# Research dry-run prompt export (no API call)\n"
        f"# model={rep['model']} temperature={rep['temperature']} max_output_tokens={rep['max_output_tokens']}\n"
        f"# input_tokens_est={rep['input_tokens']} "
        f"total_usd_typical={rep['total_usd_typical']:.6f} total_usd_worst={rep['total_usd_worst']:.6f}\n"
        f"# news_rows={st['news_rows_in_prompt_window']} price_symbols={st['price_context_symbols']}\n"
        f"# system_chars={rep['system_chars']} user_chars={rep['user_chars']} total_chars={rep['total_chars']}\n"
        f"\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("=== SYSTEM PROMPT ===\n\n")
        f.write(rep["system_prompt"])
        f.write("\n\n=== USER PROMPT ===\n\n")
        f.write(rep["user_prompt"])
        if not rep["user_prompt"].endswith("\n"):
            f.write("\n")


def estimate_research_dry_run(cfg: dict) -> Dict[str, Any]:
    """
    Build the same prompts as run_research (no API calls) and return token/cost estimates + debug stats.
    Research uses exactly one chat.completions call per run.
    """
    cfg = {**cfg}
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)

    min_conf = float(cfg.get("agent_research_min_confidence", 0.75))
    now = datetime.now(timezone.utc)
    ctx = ResearchRunContext(sim_now=now, daily_mode=False)
    max_news = int(cfg.get("agent_research_max_num_ofnews", 200))
    max_news = max(0, min(5000, max_news))
    mem = latest_memory(con)
    allowed_syms = symbol_universe_set(cfg)
    use_link = allowed_syms is not None and bool(cfg.get("agent_research_universe_news_linkage", True))
    if use_link and allowed_syms:
        news = fetch_news_rows_between_universe_linked(
            con, now - timedelta(days=14), now, allowed_syms, limit=max_news
        )
    else:
        news = fetch_news_rows_between(con, now - timedelta(days=14), now, limit=max_news)
    try:
        ul = load_symbol_universe(cfg) if allowed_syms else None
    except Exception:
        ul = None
    if allowed_syms is not None and not ul:
        ul = sorted(allowed_syms)
    if ul:
        price_n = len(ul)
    else:
        syms = top_mentioned_symbols(con, limit=80)
        price_n = len(syms)

    system, user = _build_research_prompts(cfg, con, min_confidence=min_conf, ctx=ctx)
    mem_struct = parse_memory_payload(
        (mem["text"] if mem else "") or "",
        (mem["meta_json"] if mem else None),
    )
    mem_fmt = format_memory_for_prompt(
        mem_struct, max_chars=int(cfg.get("agent_memory_prompt_chars", 8000))
    )
    con.close()

    model = cfg.get("agent_research_model") or cfg.get("llm_model", DEFAULT_LLM_MODEL)
    max_out = int(cfg.get("agent_research_max_output_tokens", 12000))
    assumed_out = int(cfg.get("agent_research_assumed_output_tokens", min(4000, max_out)))
    assumed_out = min(assumed_out, max_out)

    est_typical = estimate_research_cost(
        system, user, model, assumed_output_tokens=assumed_out
    )
    est_worst = estimate_research_cost(
        system, user, model, assumed_output_tokens=max_out
    )

    return {
        "llm_calls": 1,
        "endpoint": "chat.completions (single request)",
        "model": model,
        "temperature": 0.2,
        "max_output_tokens": max_out,
        "assumed_output_tokens": assumed_out,
        "input_tokens": est_typical["input_tokens"],
        "output_tokens_est_typical": est_typical["output_tokens_est"],
        "output_tokens_est_worst": est_worst["output_tokens_est"],
        "input_usd": est_typical["input_usd"],
        "output_usd_typical": est_typical["output_usd"],
        "total_usd_typical": est_typical["total_usd"],
        "total_usd_worst": est_worst["total_usd"],
        "stats": {
            "news_rows_in_prompt_window": len(news),
            "news_lines_in_prompt": min(max_news, len(news)),
            "memory_snapshot_present": bool(mem),
            "structured_memory_chars_in_prompt": len(mem_fmt),
            "price_context_symbols": price_n,
            "symbol_universe_configured": allowed_syms is not None,
        },
        "system_prompt": system,
        "user_prompt": user,
        "system_chars": len(system),
        "user_chars": len(user),
        "total_chars": len(system) + len(user),
        "note": "USD uses cost_estimate.py pricing; set ESTIMATE_* env vars to match OpenRouter. "
        "Typical vs worst output tokens: assumed vs max_output_tokens.",
    }


def estimate_research_dry_run_for_calendar_day(cfg: dict, day: date) -> Dict[str, Any]:
    """
    Same return shape as estimate_research_dry_run, but prompts match one daily backfill day
    (first day of a range is typical). No API calls.
    """
    cfg = {**cfg}
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)

    min_conf = float(cfg.get("agent_research_min_confidence", 0.75))
    day_start = _utc_day_start(day)
    sim_now = _end_of_utc_day(day_start)
    ctx = ResearchRunContext(sim_now=sim_now, daily_mode=True, day_start_utc=day_start)

    max_news = int(cfg.get("agent_research_max_num_ofnews", 200))
    max_news = max(0, min(5000, max_news))
    mem_row = latest_memory_before(con, sim_now)
    allowed_syms = symbol_universe_set(cfg)
    use_link = allowed_syms is not None and bool(cfg.get("agent_research_universe_news_linkage", True))
    day_end_excl = day_start + timedelta(days=1)
    if use_link and allowed_syms:
        news = fetch_news_rows_calendar_day_universe_linked(
            con, day_start, allowed_syms, limit=max_news
        )
    else:
        news = fetch_news_rows_calendar_day_utc(con, day_start, limit=max_news)
    try:
        ul = load_symbol_universe(cfg) if allowed_syms else None
    except Exception:
        ul = None
    if allowed_syms is not None and not ul:
        ul = sorted(allowed_syms)
    if ul:
        price_n = len(ul)
    else:
        syms = top_mentioned_symbols_for_news_window(con, day_start, day_end_excl, limit=80)
        price_n = len(syms)

    system, user = _build_research_prompts(cfg, con, min_confidence=min_conf, ctx=ctx)
    mem_struct = parse_memory_payload(
        (mem_row["text"] if mem_row else "") or "",
        (mem_row["meta_json"] if mem_row else None),
    )
    mem_fmt = format_memory_for_prompt(
        mem_struct, max_chars=int(cfg.get("agent_memory_prompt_chars", 8000))
    )
    con.close()

    model = cfg.get("agent_research_model") or cfg.get("llm_model", DEFAULT_LLM_MODEL)
    max_out = int(cfg.get("agent_research_max_output_tokens", 12000))
    assumed_out = int(cfg.get("agent_research_assumed_output_tokens", min(4000, max_out)))
    assumed_out = min(assumed_out, max_out)

    est_typical = estimate_research_cost(
        system, user, model, assumed_output_tokens=assumed_out
    )
    est_worst = estimate_research_cost(
        system, user, model, assumed_output_tokens=max_out
    )

    return {
        "llm_calls": 1,
        "endpoint": "chat.completions (single request, daily backfill)",
        "model": model,
        "temperature": 0.2,
        "max_output_tokens": max_out,
        "assumed_output_tokens": assumed_out,
        "input_tokens": est_typical["input_tokens"],
        "output_tokens_est_typical": est_typical["output_tokens_est"],
        "output_tokens_est_worst": est_worst["output_tokens_est"],
        "input_usd": est_typical["input_usd"],
        "output_usd_typical": est_typical["output_usd"],
        "total_usd_typical": est_typical["total_usd"],
        "total_usd_worst": est_worst["total_usd"],
        "stats": {
            "news_rows_in_prompt_window": len(news),
            "news_lines_in_prompt": min(max_news, len(news)),
            "memory_snapshot_present": bool(mem_row),
            "structured_memory_chars_in_prompt": len(mem_fmt),
            "price_context_symbols": price_n,
            "symbol_universe_configured": allowed_syms is not None,
        },
        "system_prompt": system,
        "user_prompt": user,
        "system_chars": len(system),
        "user_chars": len(user),
        "total_chars": len(system) + len(user),
        "note": f"Daily backfill sample for UTC day {day.isoformat()}. "
        "USD uses cost_estimate.py pricing; set ESTIMATE_* env vars to match OpenRouter.",
    }


def _run_research_once(cfg: dict, con, ctx: ResearchRunContext) -> int:
    """Single LLM call + persist memory + recommendations + optional Telegram publish."""
    min_conf = float(cfg.get("agent_research_min_confidence", 0.75))
    sim_now = ctx.sim_now
    if sim_now.tzinfo is None:
        sim_now = sim_now.replace(tzinfo=timezone.utc)
    sim_now = sim_now.astimezone(timezone.utc)

    if cfg.get("agent_research_run_universe_preprocess_before_research", True):
        try:
            from telegram_agent.news_universe_preprocess import run_news_universe_preprocess

            pr = run_news_universe_preprocess(
                cfg,
                con,
                limit=int(cfg.get("news_universe_preprocess_max_rows_per_run", 100_000)),
                max_ts_utc_inclusive=sim_now,
            )
            if not pr.get("skipped"):
                logger.info(
                    "Universe news preprocess: processed=%s batch_rounds=%s universe=%s",
                    pr.get("processed"),
                    pr.get("batches"),
                    pr.get("allowed_universe_size"),
                )
        except Exception as e:
            logger.warning("Universe news preprocess failed (continuing): %s", e)

    system, user = _build_research_prompts(cfg, con, min_confidence=min_conf, ctx=ctx)
    model = cfg.get("agent_research_model") or cfg.get("llm_model", DEFAULT_LLM_MODEL)
    max_out = int(cfg.get("agent_research_max_output_tokens", 12000))

    client = _get_openrouter_client()
    if not client:
        logger.error("OPENROUTER_API_KEY not set; cannot run research.")
        return 0

    try:
        # Prefer JSON-typed responses when supported to reduce "almost JSON but not quite"
        # outputs (observed with some models, incl. Claude Sonnet variants).
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                max_tokens=max_out,
                response_format={"type": "json_object"},
            )
        except Exception:
            # Fallback for providers/models that don't support response_format.
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                max_tokens=max_out,
            )
        raw = resp.choices[0].message.content or ""
        if not isinstance(raw, str):
            raw = str(raw)
        raw = raw.strip()
    except Exception as e:
        logger.error("Research LLM failed: %s", e)
        return 0

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Heuristic salvage: extract the largest {...} region and retry parsing.
        i0 = raw.find("{")
        i1 = raw.rfind("}")
        if i0 != -1 and i1 != -1 and i1 > i0:
            candidate = raw[i0 : i1 + 1].strip()
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                logger.error(
                    "Research output not JSON; first 500 chars: %s", raw[:500]
                )
                return 0
        else:
            logger.error("Research output not JSON; first 500 chars: %s", raw[:500])
            return 0

    if not isinstance(data, dict):
        logger.error("Research output root must be an object")
        return 0

    if ctx.daily_mode and ctx.day_start_utc is not None:
        mem_row = latest_memory_before(con, sim_now)
    else:
        mem_row = latest_memory(con)

    prev_struct = parse_memory_payload(
        (mem_row["text"] if mem_row else "") or "",
        (mem_row["meta_json"] if mem_row else None),
    )
    merged = merge_memory_state(
        prev_struct,
        data.get("memory_update") or {},
        now=sim_now,
        cap_strongest=int(cfg.get("agent_memory_cap_strongest", 20)),
        cap_recent=int(cfg.get("agent_memory_cap_recent", 20)),
        suggestion_days=int(cfg.get("agent_memory_suggestion_days", 30)),
    )
    mem_text = json.dumps(merged, ensure_ascii=False, indent=2)
    upsert_memory(
        con,
        horizon_months=int(cfg.get("agent_memory_months", 6)),
        text=mem_text[:50000],
        meta=memory_meta_wrapper(merged),
        ts_utc=sim_now,
    )

    allowed_syms = symbol_universe_set(cfg)
    suggestions = data.get("suggestions") or []
    if not isinstance(suggestions, list):
        suggestions = []

    stored_summaries: List[Dict[str, Any]] = []
    n = 0
    for o in suggestions:
        if not isinstance(o, dict):
            continue
        sym = str(o.get("symbol") or "").strip().upper()
        if not sym:
            continue
        if allowed_syms is not None and sym not in allowed_syms:
            logger.info("Skip %s (not in symbol universe)", sym)
            continue
        try:
            conf_f = float(o.get("confidence")) if o.get("confidence") is not None else None
        except (TypeError, ValueError):
            conf_f = None
        if conf_f is None or conf_f < min_conf:
            continue
        if o.get("novel_vs_memory") is not True:
            continue
        what = str(o.get("what_to_acquire") or "").strip()
        if not what:
            continue

        sug_ts = _parse_iso(o.get("suggestion_ts_utc")) or sim_now
        ew0 = _parse_iso(o.get("entry_window_start_utc"))
        ew1 = _parse_iso(o.get("entry_window_end_utc"))
        ex = _parse_iso(o.get("execute_review_utc"))
        if not ew0 or not ew1 or not ex:
            logger.warning("Skip %s: incomplete plan timestamps", sym)
            continue
        if ew1 < ew0:
            logger.warning("Skip %s: entry_window_end before start", sym)
            continue

        fc_pct = o.get("forecast_pct")
        try:
            fc_f = float(fc_pct) if fc_pct is not None else None
        except (TypeError, ValueError):
            fc_f = None

        rationale = str(o.get("rationale") or "")
        meta = {
            "plan": {
                "what_to_acquire": what,
                "suggestion_ts_utc": sug_ts.isoformat(),
                "entry_window_start_utc": ew0.isoformat(),
                "entry_window_end_utc": ew1.isoformat(),
                "execute_review_utc": ex.isoformat(),
                "memory_trend_alignment": o.get("memory_trend_alignment"),
            },
            "priced_in": str(o.get("priced_in") or ""),
            "novel_vs_memory": True,
            "raw": o,
        }
        if ctx.daily_mode:
            meta["backfill"] = {
                "daily_utc": ctx.day_start_utc.astimezone(timezone.utc).date().isoformat()
                if ctx.day_start_utc
                else None,
            }
        try:
            insert_recommendation(
                con,
                symbol=sym,
                duration="plan",
                forecast_usd=None,
                forecast_pct=fc_f,
                confidence=conf_f,
                rationale=rationale,
                meta=meta,
                ts_utc=sim_now,
                suggestion_ts_utc=sug_ts,
                entry_window_start_utc=ew0,
                entry_window_end_utc=ew1,
                execute_review_utc=ex,
            )
            n += 1
            stored_summaries.append(
                {
                    "symbol": sym,
                    "what_to_acquire": what,
                    "suggestion_ts_utc": sug_ts.isoformat(),
                    "entry_window_start_utc": ew0.isoformat(),
                    "entry_window_end_utc": ew1.isoformat(),
                    "execute_review_utc": ex.isoformat(),
                }
            )
        except Exception as e:
            logger.warning("Skip bad suggestion row %s: %s", sym, e)

    thinking = str(data.get("thinking") or "")
    backfill_publish = cfg.get("agent_research_backfill_publish", True)
    do_publish = cfg.get("agent_research_publish", True)
    if ctx.daily_mode and not backfill_publish:
        do_publish = False
    if do_publish:
        try:
            msg = format_research_telegram_message(
                thinking=thinking,
                merged_memory=merged,
                new_suggestions=stored_summaries,
                run_ts=sim_now,
            )
            publish_research_to_target(cfg, msg)
        except Exception as e:
            logger.warning("Research publish failed (memory still saved): %s", e)

    logger.info("Research stored %s recommendations; memory updated (sim_now=%s)", n, sim_now.isoformat())
    return n


def run_research(cfg: dict) -> int:
    cfg = {**cfg}
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    now = datetime.now(timezone.utc)
    ctx = ResearchRunContext(sim_now=now, daily_mode=False)
    try:
        return _run_research_once(cfg, con, ctx)
    finally:
        con.close()


def run_research_backfill(
    cfg: dict,
    *,
    start: date,
    end: date,
    clear_memories: bool = False,
) -> Dict[str, Any]:
    """
    Chronological daily replay: each UTC day uses only that day's news, memory from prior
    snapshots (strictly before that midnight), prices as of end of that UTC day, then writes
    memory + recommendations with simulated timestamps and publishes (unless disabled).
    """
    cfg = {**cfg}
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)

    if clear_memories:
        con.execute("DELETE FROM memories")
        con.commit()
        logger.warning("Cleared all rows in memories (backfill --clear-memories).")

    total_recs = 0
    days_done = 0
    skipped = 0
    d = start
    sleep_s = float(cfg.get("agent_research_backfill_sleep_seconds", 1.0))

    while d <= end:
        day_start = _utc_day_start(d)
        sim_now = _end_of_utc_day(day_start)
        ctx = ResearchRunContext(
            sim_now=sim_now,
            daily_mode=True,
            day_start_utc=day_start,
        )
        logger.info("Research backfill day %s (sim_now=%s)", d.isoformat(), sim_now.isoformat())
        n = _run_research_once(cfg, con, ctx)
        if n == 0:
            # still counts as a day processed (memory may have updated); LLM failure returns 0 too
            skipped += 1
        total_recs += n
        days_done += 1
        d = d + timedelta(days=1)
        if d <= end and sleep_s > 0:
            time.sleep(sleep_s)

    con.close()
    return {
        "days": days_done,
        "recommendations": total_recs,
        "days_with_zero_new_recs": skipped,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def estimate_research_backfill_dry_run(
    cfg: dict, *, start: date, end: date, sample_day: Optional[date] = None
) -> Dict[str, Any]:
    """Cost estimate for one simulated day in backfill (or first day in range if sample_day omitted)."""
    cfg = {**cfg}
    day = sample_day or start
    db = Path(cfg.get("agent_db_path", "telegram_agent/data/agent.sqlite"))
    con = connect(db)
    init_db(con)
    day_start = _utc_day_start(day)
    sim_now = _end_of_utc_day(day_start)
    ctx = ResearchRunContext(sim_now=sim_now, daily_mode=True, day_start_utc=day_start)
    min_conf = float(cfg.get("agent_research_min_confidence", 0.75))
    system, user = _build_research_prompts(cfg, con, min_confidence=min_conf, ctx=ctx)
    max_news = int(cfg.get("agent_research_max_num_ofnews", 200))
    max_news = max(0, min(5000, max_news))
    allowed_syms = symbol_universe_set(cfg)
    use_link = allowed_syms is not None and bool(cfg.get("agent_research_universe_news_linkage", True))
    if use_link and allowed_syms:
        news = fetch_news_rows_calendar_day_universe_linked(
            con, day_start, allowed_syms, limit=max_news
        )
    else:
        news = fetch_news_rows_calendar_day_utc(con, day_start, limit=max_news)
    con.close()

    model = cfg.get("agent_research_model") or cfg.get("llm_model", DEFAULT_LLM_MODEL)
    max_out = int(cfg.get("agent_research_max_output_tokens", 12000))
    assumed_out = int(cfg.get("agent_research_assumed_output_tokens", min(4000, max_out)))
    assumed_out = min(assumed_out, max_out)
    est_typical = estimate_research_cost(system, user, model, assumed_output_tokens=assumed_out)
    est_worst = estimate_research_cost(system, user, model, assumed_output_tokens=max_out)

    n_days = (end - start).days + 1
    return {
        "sample_day": day.isoformat(),
        "days_in_range": n_days,
        "llm_calls_total_est": n_days,
        "model": model,
        "max_output_tokens": max_out,
        "per_day_input_tokens_est": est_typical["input_tokens"],
        "per_day_total_usd_typical": est_typical["total_usd"],
        "per_day_total_usd_worst": est_worst["total_usd"],
        "range_total_usd_typical": est_typical["total_usd"] * n_days,
        "range_total_usd_worst": est_worst["total_usd"] * n_days,
        "news_rows_sample_day": len(news),
        "system_chars": len(system),
        "user_chars": len(user),
    }
