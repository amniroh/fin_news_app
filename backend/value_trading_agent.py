"""
Intrinsic value / 6-pillar value-trading agent for interesting stocks.

Reuses OpenRouter JSON LLM patterns from ``telegram_agent.agent_research`` and
context gathering from ``interesting_stocks_service`` / agent DB news.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (_REPO_ROOT, _BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from value_metrics_store import (
    connect,
    init_db,
    insert_value_trading_assessment,
    latest_analyst_rating,
    latest_value_trading_assessment,
    list_interesting_stocks,
    query_analyst_ratings,
    query_fundamental_points,
    query_latest_daily_metric_points,
    query_value_trading_assessments,
)

logger = logging.getLogger(__name__)

PILLAR_KEYS = (
    "competitive_edge",
    "management_competence",
    "financial_fortress",
    "pricing_power",
    "understandability",
    "valuation",
)

DEFAULT_VALUE_TRADING_MODEL = "google/gemini-2.5-flash"
DEFAULT_VALUE_TRADING_ONLINE = "true"
DEFAULT_VALUE_TRADING_REASONING_EFFORT = "medium"
DEFAULT_VALUE_TRADING_REASSESS_DAYS = 60
# ~12k chars input/ticker (median P0); batch 10 ≈ 22k input tokens + 30k output budget.
# Larger batches share one :online search session — main quality limiter above ~10–12.
DEFAULT_VALUE_TRADING_BATCH_SIZE = 10

_SINGLE_ASSESSMENT_JSON_SCHEMA = """{
  "symbol": "<TICKER>",
  "investment_name": "<Name of Asset>",
  "overall_summary": "<2-3 sentence executive summary>",
  "pillars": {
    "competitive_edge": { "rationale": "<analysis>", "score": <0-5 integer> },
    "management_competence": { "rationale": "<analysis>", "score": <0-5 integer> },
    "financial_fortress": { "rationale": "<analysis>", "score": <0-5 integer> },
    "pricing_power": { "rationale": "<analysis>", "score": <0-5 integer> },
    "understandability": { "rationale": "<analysis>", "score": <0-5 integer> },
    "valuation": { "rationale": "<analysis>", "score": <0-5 integer> }
  },
  "total_score": <integer sum of pillar scores, out of 30>
}"""

VALUE_TRADING_SYSTEM_PROMPT = """# SYSTEM PROMPT: Intrinsic Value Investment Analyst

## ROLE
You are an expert investment analyst AI, specializing in fundamental, intrinsic value investing. Your investment philosophy mirrors Warren Buffett and Charlie Munger. Your primary goal is to assess the quality of an asset, its competitive moat, management integrity, and its margin of safety.

## TASK
You will be given **one** investment option (ticker) and structured data from our database (fundamentals, prices, news, analyst ratings). This input is a **starting point only** — you must **not** limit your analysis to it.

Before scoring, conduct **deep research** on the asset: business model, competitive position, management track record, capital structure, industry dynamics, peer comparisons, recent filings, and current valuation context. Actively seek and incorporate **additional information available online** (public filings, earnings, credible financial journalism, exchange data, and other verifiable sources) wherever the provided data is thin, stale, or incomplete.

Use the provided database context to orient your research and cross-check facts, but your final assessment must reflect a thorough, up-to-date view of the asset — not a summary of the input alone.

## THE 6-PILLAR FRAMEWORK
For each pillar, provide a brief analytical rationale, followed by a strict rating from 0 to 5.

1. **Competitive Edge (Moat):** bottlenecks, switching costs, network effects, brand loyalty, irreplaceable utility.
   Rating: 0 (No moat/commoditized) to 5 (Unassailable monopoly).

2. **Management Competence:** rational capital allocation, transparency, incentive alignment.
   Rating: 0 (Self-serving/opaque) to 5 (Generational capital allocators).

3. **Financial Fortress & Capital Efficiency:** cash flow, ROIC, debt/leverage; for crypto, tokenomics and inflation.
   Rating: 0 (Drowning in debt/dilutive) to 5 (Cash-printing machine/zero net debt).

4. **Pricing Power & Inflation Resistance:** ability to raise prices without losing customers.
   Rating: 0 (Price taker) to 5 (Absolute pricing power).

5. **Understandability & Circle of Competence:** simplicity of business model or tokenomics.
   Rating: 0 (Impenetrable black box) to 5 (Simple and highly predictable).

6. **Valuation & Margin of Safety:** current price vs intrinsic value, history, peers; downside risk.
   Rating: 0 (Massively overvalued) to 5 (Deeply undervalued/high margin of safety).

## STRICT CONSTRAINTS
- DO NOT predict exact percentage growth for future timeframes. Focus on current intrinsic value and margin of safety.
- You **must** go beyond the provided input: perform deep search and research to gather whatever additional verified information is needed to complete all six pillars.
- Do **not** hallucinate financial metrics, quotes, or facts. Use only verifiable data from the input, your research, or well-established public knowledge you are confident about. When citing specific numbers, prefer recent, authoritative sources.
- If, after thorough research, information for a specific pillar is still unavailable or unverifiable, state "Insufficient Data" in the rationale and assign a score of 0.

## OUTPUT FORMAT
Output ONLY valid JSON (no markdown fences) with exactly this structure:
{
  "investment_name": "<Name of Asset>",
  "overall_summary": "<2-3 sentence executive summary>",
  "pillars": {
    "competitive_edge": { "rationale": "<analysis>", "score": <0-5 integer> },
    "management_competence": { "rationale": "<analysis>", "score": <0-5 integer> },
    "financial_fortress": { "rationale": "<analysis>", "score": <0-5 integer> },
    "pricing_power": { "rationale": "<analysis>", "score": <0-5 integer> },
    "understandability": { "rationale": "<analysis>", "score": <0-5 integer> },
    "valuation": { "rationale": "<analysis>", "score": <0-5 integer> }
  },
  "total_score": <integer sum of pillar scores, out of 30>
}
"""

VALUE_TRADING_BATCH_SYSTEM_PROMPT = f"""# SYSTEM PROMPT: Intrinsic Value Investment Analyst (BATCH)

## ROLE
You are an expert investment analyst AI, specializing in fundamental, intrinsic value investing. Your investment philosophy mirrors Warren Buffett and Charlie Munger.

## TASK
You will be given **multiple** investment options (tickers) in one request, each with structured database context (fundamentals, prices, news, analyst ratings). This input is a **starting point only** — you must **not** limit your analysis to it.

For **each** ticker, conduct deep online research before scoring: business model, moat, management, capital structure, industry dynamics, peers, filings, and valuation. Use the provided data to orient research and cross-check facts.

## THE 6-PILLAR FRAMEWORK
For each pillar per ticker: brief rationale + strict score 0–5 (competitive_edge, management_competence, financial_fortress, pricing_power, understandability, valuation). See single-ticker rubric in training.

## STRICT CONSTRAINTS
- DO NOT predict exact future percentage growth. Focus on intrinsic value and margin of safety.
- Perform deep research beyond the input; do not hallucinate metrics.
- If a pillar lacks verifiable data after research: rationale "Insufficient Data", score 0.
- You **must** return one complete assessment for **every** ticker in the user message.

## OUTPUT FORMAT
Output ONLY valid JSON (no markdown fences):
{{
  "assessments": [
    {_SINGLE_ASSESSMENT_JSON_SCHEMA.strip()}
  ]
}}
Include exactly one object per input ticker; set ``symbol`` to the ticker string.
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_value_trading_config(cfg: Optional[dict] = None) -> dict:
    """Merge telegram ``load_config()`` with value-trading env overrides."""
    if cfg is None:
        from telegram_agent.config import load_config

        cfg = load_config()
    out = dict(cfg)
    vt_env = os.getenv("VALUE_TRADING_MODEL", "").strip()
    if vt_env:
        out["value_trading_model"] = vt_env
    elif str(out.get("value_trading_model") or "").strip():
        out["value_trading_model"] = str(out["value_trading_model"]).strip()
    else:
        out["value_trading_model"] = DEFAULT_VALUE_TRADING_MODEL
    out["value_trading_max_output_tokens"] = int(
        os.getenv("VALUE_TRADING_MAX_OUTPUT_TOKENS", out.get("agent_research_max_output_tokens", 8000))
    )
    out["value_trading_sleep_seconds"] = float(os.getenv("VALUE_TRADING_SLEEP_SECONDS", "1.0"))
    out["value_trading_max_news"] = int(os.getenv("VALUE_TRADING_MAX_NEWS", "40"))
    out["value_trading_online"] = (
        os.getenv("VALUE_TRADING_ONLINE", DEFAULT_VALUE_TRADING_ONLINE) or DEFAULT_VALUE_TRADING_ONLINE
    ).strip().lower()
    effort_env = (os.getenv("VALUE_TRADING_REASONING_EFFORT", "") or "").strip().lower()
    out["value_trading_reasoning_effort"] = effort_env or DEFAULT_VALUE_TRADING_REASONING_EFFORT
    out["value_trading_reassess_days"] = int(
        os.getenv("VALUE_TRADING_REASSESS_DAYS", str(DEFAULT_VALUE_TRADING_REASSESS_DAYS))
    )
    out["value_trading_batch_size"] = max(
        1, int(os.getenv("VALUE_TRADING_BATCH_SIZE", str(DEFAULT_VALUE_TRADING_BATCH_SIZE)))
    )
    rmt = (os.getenv("VALUE_TRADING_REASONING_MAX_TOKENS", "") or "").strip()
    out["value_trading_reasoning_max_tokens"] = int(rmt) if rmt.isdigit() else None
    out["value_trading_web_max_results"] = int(os.getenv("VALUE_TRADING_WEB_MAX_RESULTS", "8"))
    wscs = (os.getenv("VALUE_TRADING_WEB_SEARCH_CONTEXT_SIZE", "high") or "high").strip().lower()
    out["value_trading_web_search_context_size"] = wscs
    return out


_BUILTIN_SEARCH_PREFIXES = ("perplexity/sonar",)


def _model_has_builtin_search(model: str) -> bool:
    mid = str(model or "").strip().lower()
    base = mid.split(":")[0]
    return any(base.startswith(p) for p in _BUILTIN_SEARCH_PREFIXES)


def _model_is_gemini(model: str) -> bool:
    base = str(model or "").strip().lower().split(":")[0]
    return base.startswith("google/gemini")


def resolve_value_trading_model(cfg: dict, model: Optional[str] = None) -> str:
    """Resolve OpenRouter model slug, optionally appending ``:online`` for web search."""
    m = str(model or cfg.get("value_trading_model") or "").strip()
    if not m:
        return m
    if ":online" in m or _model_has_builtin_search(m):
        return m
    mode = str(cfg.get("value_trading_online", "auto") or "auto").strip().lower()
    if mode in ("0", "false", "no", "off"):
        return m
    if mode in ("1", "true", "yes", "on"):
        return f"{m}:online"
    # auto: enable web search for Gemini slugs (not Perplexity Sonar, which is built-in).
    if _model_is_gemini(m):
        return f"{m}:online"
    return m


def build_value_trading_openrouter_extra(cfg: dict, *, model: str) -> Dict[str, Any]:
    """OpenRouter-specific request fields (reasoning, web plugin options)."""
    extra: Dict[str, Any] = {}
    effort = str(cfg.get("value_trading_reasoning_effort") or "").strip().lower()
    max_rt = cfg.get("value_trading_reasoning_max_tokens")
    reasoning: Dict[str, Any] = {"exclude": True}
    if effort and effort not in ("none", "off", "false", "0"):
        reasoning["effort"] = effort
        reasoning["enabled"] = True
    elif max_rt is not None and int(max_rt) > 0:
        reasoning["max_tokens"] = int(max_rt)
        reasoning["enabled"] = True
    elif _model_is_gemini(model) and (
        ":online" in model or str(cfg.get("value_trading_online", "")).lower() in ("1", "true", "yes", "on")
    ):
        # Sensible default when Gemini + online is requested without explicit effort.
        reasoning["effort"] = "medium"
        reasoning["enabled"] = True
    if reasoning.get("enabled"):
        extra["reasoning"] = reasoning

    resolved = resolve_value_trading_model(cfg, model)
    if not _model_has_builtin_search(resolved):
        wscs = str(cfg.get("value_trading_web_search_context_size") or "").strip().lower()
        web_opts: Dict[str, Any] = {}
        if wscs in ("low", "medium", "high"):
            web_opts["search_context_size"] = wscs
        if ":online" not in resolved and str(cfg.get("value_trading_online", "")).lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            extra["plugins"] = [
                {
                    "id": "web",
                    "max_results": int(cfg.get("value_trading_web_max_results", 8)),
                }
            ]
        if web_opts:
            extra["web_search_options"] = web_opts
    return extra


def _parse_json_llm_output(raw: str) -> Optional[Dict[str, Any]]:
    """Same salvage strategy as ``agent_research._run_research_once``."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        i0, i1 = text.find("{"), text.rfind("}")
        if i0 != -1 and i1 > i0:
            try:
                data = json.loads(text[i0 : i1 + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def call_json_llm(
    cfg: dict,
    *,
    system: str,
    user: str,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    from telegram_agent.summarizer import _get_openrouter_client

    client = _get_openrouter_client()
    if not client:
        logger.error("OPENROUTER_API_KEY not set")
        return None
    base_model = str(model or cfg.get("value_trading_model") or "").strip()
    m = resolve_value_trading_model(cfg, base_model)
    extra_body = build_value_trading_openrouter_extra(cfg, model=m)
    max_out = int(max_tokens or cfg.get("value_trading_max_output_tokens", 8000))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    logger.debug(
        "value-trading LLM model=%s extra_keys=%s",
        m,
        sorted(extra_body.keys()),
    )
    try:
        attempts: List[Dict[str, Any]] = [
            {
                "model": m,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": max_out,
                "response_format": {"type": "json_object"},
                **({"extra_body": extra_body} if extra_body else {}),
            },
            {
                "model": m,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": max_out,
                **({"extra_body": extra_body} if extra_body else {}),
            },
            {
                "model": m,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": max_out,
            },
        ]
        resp = None
        last_err: Optional[Exception] = None
        for kwargs in attempts:
            try:
                resp = client.chat.completions.create(**kwargs)
                break
            except Exception as e:
                last_err = e
                continue
        if resp is None:
            raise last_err or RuntimeError("all LLM attempts failed")
        raw = resp.choices[0].message.content or ""
        if not isinstance(raw, str):
            raw = str(raw)
        parsed = _parse_json_llm_output(raw.strip())
        if parsed is None and raw.strip():
            logger.warning(
                "Value-trading JSON parse failed (model=%s); first 800 chars: %s",
                m,
                raw.strip()[:800],
            )
        return parsed
    except Exception as e:
        logger.error("Value-trading LLM failed (model=%s): %s", m, e)
        return None


def _clamp_score(v: Any) -> int:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, n))


def normalize_value_trading_output(data: Dict[str, Any], *, symbol: str) -> Dict[str, Any]:
    """Validate and normalize LLM JSON to storage shape."""
    pillars_in = data.get("pillars") if isinstance(data.get("pillars"), dict) else {}
    pillars_out: Dict[str, Dict[str, Any]] = {}
    pillar_scores: Dict[str, int] = {}
    for key in PILLAR_KEYS:
        block = pillars_in.get(key) if isinstance(pillars_in.get(key), dict) else {}
        score = _clamp_score(block.get("score"))
        rationale = str(block.get("rationale") or "").strip() or "Insufficient Data"
        if score == 0 and rationale.lower() == "insufficient data":
            pass
        pillars_out[key] = {"rationale": rationale, "score": score}
        pillar_scores[key] = score
    total = sum(pillar_scores.values())
    try:
        llm_total = int(data.get("total_score"))
        if llm_total != total:
            logger.debug("%s: total_score %s != computed %s; using computed", symbol, llm_total, total)
    except (TypeError, ValueError):
        pass
    return {
        "investment_name": str(data.get("investment_name") or symbol).strip(),
        "overall_summary": str(data.get("overall_summary") or "").strip(),
        "pillars": pillars_out,
        "pillar_scores": pillar_scores,
        "total_score": total,
        "raw": data,
    }


def gather_symbol_context(
    vm_db: Path,
    symbol: str,
    *,
    cfg: Optional[dict] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Build user prompt body + metadata for one symbol."""
    from interesting_stocks_service import _agent_news_for_symbol, _agent_price_history, _coverage_window
    from telegram_agent.symbol_universe import load_symbol_type_map

    sym = str(symbol).strip().upper()
    cfg = load_value_trading_config(cfg)
    start_s, end_s, _, _ = _coverage_window()
    type_map = load_symbol_type_map()
    asset_type = type_map.get(sym, "unknown")

    con = connect(vm_db)
    init_db(con)
    try:
        stock_rows = [r for r in list_interesting_stocks(con) if str(r["symbol"]) == sym]
        stock = stock_rows[0] if stock_rows else {}
        metrics = query_latest_daily_metric_points(con, symbols=[sym], provider="yfinance")
        fundamentals = query_fundamental_points(
            con, symbols=[sym], start_date=start_s, end_date=end_s, provider="yfinance", period="quarter"
        )
        analyst_latest = latest_analyst_rating(con, symbol=sym)
        analyst_hist = query_analyst_ratings(con, symbol=sym, start_date=start_s, end_date=end_s, limit=12)
    finally:
        con.close()

    prices = _agent_price_history(sym, start_s, end_s)
    news = _agent_news_for_symbol(sym, limit=int(cfg.get("value_trading_max_news", 40)))

    lines: List[str] = [
        f"# Investment option: {sym}",
        f"- Asset type (universe): {asset_type}",
        f"- Universe priority: {stock.get('universe_priority', 'unknown')}",
        f"- Name: {stock.get('name') or sym}",
        "",
        "## Latest daily metrics (SQLite, yfinance pipeline)",
    ]
    if metrics:
        m = metrics[0]
        lines.append(
            f"asof={m.get('asof_date')} pe={m.get('pe')} pb={m.get('pb')} roe={m.get('roe')} "
            f"debt_to_equity={m.get('debt_to_equity')} operating_margin={m.get('operating_margin')} "
            f"fcf_yield={m.get('free_cash_flow_yield')} div_yield={m.get('dividend_yield')}"
        )
    else:
        lines.append("(No precomputed daily metrics in DB.)")

    lines.extend(["", "## Recent quarterly fundamentals (last 8 quarters)"])
    for f in (fundamentals or [])[-8:]:
        lines.append(
            f"- {f.get('asof_date')}: eps={f.get('eps')} revenue={f.get('revenue')} "
            f"net_income={f.get('net_income')} fcf={f.get('free_cash_flow')}"
        )
    if not fundamentals:
        lines.append("(No quarterly fundamentals in DB.)")

    lines.extend(["", "## Price history (daily, deduped, last 10 bars)"])
    for p in (prices or [])[-10:]:
        lines.append(f"- {str(p.get('ts', ''))[:10]}: close={p.get('close')}")
    if not prices:
        lines.append("(No price history in agent DB.)")

    lines.extend(["", "## Analyst ratings (yfinance snapshots)"])
    if analyst_latest:
        lines.append(
            f"Latest ({analyst_latest.get('asof_date')}): key={analyst_latest.get('recommendation_key')} "
            f"mean={analyst_latest.get('recommendation_mean')} target_mean={analyst_latest.get('target_mean')} "
            f"buy={analyst_latest.get('buy')} hold={analyst_latest.get('hold')} sell={analyst_latest.get('sell')}"
        )
    for a in sorted(analyst_hist or [], key=lambda x: str(x.get("asof_date") or ""))[-6:]:
        lines.append(
            f"- {a.get('asof_date')}: mean={a.get('recommendation_mean')} "
            f"counts sb/b/h/s/ss={a.get('strong_buy')}/{a.get('buy')}/{a.get('hold')}/{a.get('sell')}/{a.get('strong_sell')}"
        )
    if not analyst_latest and not analyst_hist:
        lines.append("(No analyst ratings stored.)")

    lines.extend(["", "## Recent news (agent DB, universe-linked)"])
    for n in (news or [])[: int(cfg.get("value_trading_max_news", 40))]:
        title = str(n.get("title") or "")[:140]
        snippet = str(n.get("snippet") or "")[:280]
        lines.append(f"- [{n.get('ts_utc')}] {title}\n  {snippet}")
    if not news:
        lines.append("(No linked news in agent DB.)")

    lines.extend(
        [
            "",
            "The database excerpts above are partial context only. Conduct deep online research to fill gaps "
            "and complement this input with any additional verified information required for a complete "
            "6-Pillar assessment. Analyze this single investment using the 6-Pillar Framework. "
            "Return JSON only per the system prompt schema.",
        ]
    )

    meta = {
        "symbol": sym,
        "asset_type": asset_type,
        "n_news": len(news or []),
        "n_fundamentals": len(fundamentals or []),
        "n_prices": len(prices or []),
        "has_analyst": analyst_latest is not None,
    }
    return "\n".join(lines), meta


def _parse_produced_ts_utc(produced_ts_utc: str) -> Optional[datetime]:
    raw = str(produced_ts_utc or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def assessment_age_days(produced_ts_utc: str, *, now: Optional[datetime] = None) -> Optional[int]:
    """Whole days since ``produced_ts_utc`` (UTC)."""
    dt = _parse_produced_ts_utc(produced_ts_utc)
    if dt is None:
        return None
    ref = now or datetime.now(timezone.utc)
    return max(0, int((ref - dt).total_seconds() // 86400))


def symbol_needs_value_trading_assessment(
    vm_db: Path,
    symbol: str,
    *,
    min_reassess_days: int = DEFAULT_VALUE_TRADING_REASSESS_DAYS,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Return (needs_assessment, latest_row).

    Skips when a stored assessment is younger than ``min_reassess_days``.
    """
    sym = str(symbol).strip().upper()
    if not sym:
        return False, None
    con = connect(vm_db)
    init_db(con)
    try:
        latest = latest_value_trading_assessment(con, symbol=sym)
    finally:
        con.close()
    if not latest:
        return True, None
    produced = str(latest.get("produced_ts_utc") or "")
    age = assessment_age_days(produced)
    if age is None:
        return True, latest
    if age < int(min_reassess_days):
        return False, latest
    return True, latest


def filter_symbols_needing_assessment(
    vm_db: Path,
    symbols: Sequence[str],
    *,
    min_reassess_days: int = DEFAULT_VALUE_TRADING_REASSESS_DAYS,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Split symbols into those eligible for a new LLM run vs recently assessed."""
    eligible: List[str] = []
    skipped: List[Dict[str, Any]] = []
    for sym in symbols:
        s = str(sym).strip().upper()
        if not s:
            continue
        needs, latest = symbol_needs_value_trading_assessment(
            vm_db, s, min_reassess_days=min_reassess_days
        )
        if needs:
            eligible.append(s)
        else:
            skipped.append(
                {
                    "symbol": s,
                    "reason": "recent_assessment",
                    "produced_ts_utc": latest.get("produced_ts_utc") if latest else None,
                    "age_days": assessment_age_days(str(latest.get("produced_ts_utc") or "")) if latest else None,
                    "min_reassess_days": int(min_reassess_days),
                }
            )
    return eligible, skipped


def build_value_trading_prompts(
    vm_db: Path,
    symbol: str,
    *,
    cfg: Optional[dict] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    user, meta = gather_symbol_context(vm_db, symbol, cfg=cfg)
    return VALUE_TRADING_SYSTEM_PROMPT, user, meta


def build_value_trading_batch_prompts(
    vm_db: Path,
    symbols: Sequence[str],
    *,
    cfg: Optional[dict] = None,
) -> Tuple[str, str, Dict[str, Dict[str, Any]]]:
    """Build one system + user prompt covering multiple tickers."""
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not syms:
        raise ValueError("symbols required for batch prompts")
    cfg_eff = dict(cfg or load_value_trading_config(cfg))
    if len(syms) > 1:
        cfg_eff["value_trading_max_news"] = min(int(cfg_eff.get("value_trading_max_news", 40)), 20)

    sections: List[str] = []
    metas: Dict[str, Dict[str, Any]] = {}
    for sym in syms:
        user_part, meta = gather_symbol_context(vm_db, sym, cfg=cfg_eff)
        sections.append(user_part)
        metas[sym] = meta

    if len(syms) == 1:
        system = VALUE_TRADING_SYSTEM_PROMPT
    else:
        system = VALUE_TRADING_BATCH_SYSTEM_PROMPT

    user = (
        "\n\n==========\n\n".join(sections)
        + "\n\n"
        + (
            "Analyze this single investment using the 6-Pillar Framework. Return JSON only per the system prompt schema."
            if len(syms) == 1
            else f"Analyze all {len(syms)} investments above (tickers: {', '.join(syms)}). "
            "Conduct deep online research for each. Return JSON only with one assessment per ticker."
        )
    )
    return system, user, metas


def _chunk_symbols(symbols: Sequence[str], batch_size: int) -> List[List[str]]:
    n = max(1, int(batch_size))
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    return [syms[i : i + n] for i in range(0, len(syms), n)]


def _batch_max_output_tokens(cfg: dict, batch_size: int) -> int:
    base = int(cfg.get("value_trading_max_output_tokens", 8000))
    per_symbol = 3000
    return min(32000, max(base, int(batch_size) * per_symbol))


def parse_batch_value_trading_response(
    data: Dict[str, Any],
    expected_symbols: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    """Extract per-symbol assessment dicts from single- or multi-ticker LLM JSON."""
    expected = [str(s).strip().upper() for s in expected_symbols if str(s).strip()]
    out: Dict[str, Dict[str, Any]] = {}

    assessments = data.get("assessments")
    if isinstance(assessments, list):
        for item in assessments:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").strip().upper()
            if sym:
                out[sym] = item
        return out

    if len(expected) == 1 and isinstance(data, dict) and data.get("pillars"):
        out[expected[0]] = {**data, "symbol": expected[0]}
        return out

    # Some models return { "AAPL": { ... }, "MSFT": { ... } }
    for sym in expected:
        block = data.get(sym)
        if isinstance(block, dict) and block.get("pillars"):
            out[sym] = {**block, "symbol": sym}
    return out


def _persist_assessment(
    vm_db: Path,
    *,
    symbol: str,
    cfg: dict,
    norm: Dict[str, Any],
    meta: Dict[str, Any],
    produced: str,
    model: str,
) -> int:
    con = connect(vm_db)
    init_db(con)
    try:
        return insert_value_trading_assessment(
            con,
            symbol=symbol,
            produced_ts_utc=produced,
            model=model,
            investment_name=norm["investment_name"],
            asset_type=str(meta.get("asset_type") or ""),
            overall_summary=norm["overall_summary"],
            total_score=norm["total_score"],
            pillar_scores=norm["pillar_scores"],
            pillars_json=norm["pillars"],
            raw_json=norm["raw"],
            meta_json=meta,
        )
    finally:
        con.close()


def run_value_trading_for_batch(
    vm_db: Path,
    symbols: Sequence[str],
    *,
    cfg: Optional[dict] = None,
    dry_run: bool = False,
    persist: bool = True,
) -> Dict[str, Any]:
    """Run one LLM call for a batch of symbols; persist each assessment."""
    syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
    if not syms:
        return {"symbols": [], "results": {}, "failed": []}

    cfg = load_value_trading_config(cfg)
    system, user, metas = build_value_trading_batch_prompts(vm_db, syms, cfg=cfg)

    if dry_run:
        return {
            "symbols": syms,
            "dry_run": True,
            "system_chars": len(system),
            "user_chars": len(user),
            "batch_size": len(syms),
            "metas": metas,
        }

    max_out = _batch_max_output_tokens(cfg, len(syms))
    data = call_json_llm(cfg, system=system, user=user, max_tokens=max_out)
    if not data:
        return {"symbols": syms, "results": {}, "failed": syms, "error": "no_parseable_json"}

    parsed = parse_batch_value_trading_response(data, syms)
    produced = _utcnow_iso()
    model = resolve_value_trading_model(cfg)
    results: Dict[str, Any] = {}
    failed: List[str] = []

    for sym in syms:
        block = parsed.get(sym)
        if not block:
            failed.append(sym)
            logger.warning("Batch response missing assessment for %s", sym)
            continue
        norm = normalize_value_trading_output(block, symbol=sym)
        meta = metas.get(sym) or {}
        row_id: Optional[int] = None
        if persist:
            row_id = _persist_assessment(
                vm_db,
                symbol=sym,
                cfg=cfg,
                norm=norm,
                meta=meta,
                produced=produced,
                model=model,
            )
            logger.info("%s: value-trading stored id=%s total_score=%s/30 (batch)", sym, row_id, norm["total_score"])
        results[sym] = {
            "id": row_id,
            "symbol": sym,
            "produced_ts_utc": produced,
            "model": model,
            **norm,
        }

    return {"symbols": syms, "results": results, "failed": failed, "batch_size": len(syms)}


def run_value_trading_for_symbol(
    vm_db: Path,
    symbol: str,
    *,
    cfg: Optional[dict] = None,
    dry_run: bool = False,
    skip_if_today: bool = False,
    min_reassess_days: Optional[int] = None,
    persist: bool = True,
) -> Optional[Dict[str, Any]]:
    """Run one symbol; persist unless dry_run or persist=False."""
    sym = str(symbol).strip().upper()
    if not sym:
        return None
    cfg = load_value_trading_config(cfg)

    reassess_days = int(
        min_reassess_days
        if min_reassess_days is not None
        else (1 if skip_if_today else cfg.get("value_trading_reassess_days", DEFAULT_VALUE_TRADING_REASSESS_DAYS))
    )
    needs, latest = symbol_needs_value_trading_assessment(vm_db, sym, min_reassess_days=reassess_days)
    if not needs and not dry_run:
        age = assessment_age_days(str(latest.get("produced_ts_utc") or "")) if latest else None
        logger.info(
            "%s: skip (assessed %s days ago; reassess after %s days)",
            sym,
            age,
            reassess_days,
        )
        return None

    batch_out = run_value_trading_for_batch(
        vm_db, [sym], cfg=cfg, dry_run=dry_run, persist=persist and not dry_run
    )
    if dry_run:
        inner = {k: v for k, v in batch_out.items() if k != "metas"}
        inner["symbol"] = sym
        inner["meta"] = (batch_out.get("metas") or {}).get(sym)
        return inner
    results = batch_out.get("results") or {}
    if sym in results:
        return results[sym]
    if sym in (batch_out.get("failed") or []):
        return None
    return None


def run_value_trading_for_interesting_stocks(
    vm_db: Path,
    *,
    cfg: Optional[dict] = None,
    symbols: Optional[Sequence[str]] = None,
    max_priority: Optional[int] = None,
    max_symbols: int = 0,
    dry_run: bool = False,
    skip_if_today: bool = False,
    min_reassess_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Run assessments for all (or filtered) interesting stocks."""
    from interesting_stocks_service import seed_interesting_stocks_from_universe

    seed_interesting_stocks_from_universe(vm_db)
    cfg = load_value_trading_config(cfg)
    reassess_days = int(
        min_reassess_days
        if min_reassess_days is not None
        else (1 if skip_if_today else cfg.get("value_trading_reassess_days", DEFAULT_VALUE_TRADING_REASSESS_DAYS))
    )

    if symbols:
        sym_list = [str(s).strip().upper() for s in symbols if str(s).strip()]
    else:
        con = connect(vm_db)
        init_db(con)
        try:
            stocks = list_interesting_stocks(con)
        finally:
            con.close()
        sym_list = [str(r["symbol"]) for r in stocks]
        if max_priority is not None:
            sym_list = [
                str(r["symbol"])
                for r in stocks
                if int(r.get("universe_priority", 3)) <= int(max_priority)
            ]

    symbols_requested = len(sym_list)
    if max_symbols and max_symbols > 0:
        sym_list = sym_list[: int(max_symbols)]

    eligible, skipped_recent = filter_symbols_needing_assessment(
        vm_db, sym_list, min_reassess_days=reassess_days
    )
    if skipped_recent:
        logger.info(
            "Skipping %d symbol(s) with assessment younger than %d days",
            len(skipped_recent),
            reassess_days,
        )

    ok: List[str] = []
    skipped: List[str] = [str(s["symbol"]) for s in skipped_recent]
    failed: List[Dict[str, str]] = []
    results: List[Dict[str, Any]] = []
    batch_size = max(1, int(cfg.get("value_trading_batch_size", DEFAULT_VALUE_TRADING_BATCH_SIZE)))
    batches = _chunk_symbols(eligible, batch_size)

    for batch in batches:
        try:
            batch_out = run_value_trading_for_batch(
                vm_db,
                batch,
                cfg=cfg,
                dry_run=dry_run,
                persist=not dry_run,
            )
            if dry_run:
                results.append(batch_out)
                ok.extend(batch)
                continue
            for sym in batch:
                if sym in (batch_out.get("results") or {}):
                    row = batch_out["results"][sym]
                    ok.append(sym)
                    results.append(
                        {"symbol": sym, "total_score": row.get("total_score"), "id": row.get("id")}
                    )
                elif sym in (batch_out.get("failed") or []):
                    failed.append({"symbol": sym, "error": "missing_from_batch_response"})
                else:
                    failed.append({"symbol": sym, "error": str(batch_out.get("error") or "unknown")})
        except Exception as e:
            logger.exception("value-trading batch failed: %s", batch)
            for sym in batch:
                failed.append({"symbol": sym, "error": str(e)})
        if not dry_run:
            time.sleep(float(cfg.get("value_trading_sleep_seconds", 1.0)))

    return {
        "symbols_requested": symbols_requested,
        "symbols_considered": len(sym_list),
        "symbols_eligible": len(eligible),
        "batch_size": batch_size,
        "n_batches": len(batches),
        "min_reassess_days": reassess_days,
        "model": resolve_value_trading_model(cfg),
        "n_ok": len(ok),
        "n_skipped": len(skipped),
        "n_failed": len(failed),
        "ok": ok,
        "skipped": skipped,
        "skipped_recent": skipped_recent,
        "failed": failed,
        "results": results,
        "dry_run": dry_run,
    }


def get_value_trading_history(vm_db: Path, symbol: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    con = connect(vm_db)
    init_db(con)
    try:
        return query_value_trading_assessments(con, symbol=symbol, limit=limit)
    finally:
        con.close()
