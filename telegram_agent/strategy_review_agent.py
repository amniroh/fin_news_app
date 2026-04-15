"""
LLM-assisted audit of systematic trading logic: lookahead bias, implementation bugs,
interval/universe mismatches, and research hygiene.

Example::

    python -m telegram_agent.strategy_review_agent --strategy rsi_mean_reversion
    python -m telegram_agent.strategy_review_agent --strategy rsi_mean_reversion --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram_agent.config import DATA_DIR, DEFAULT_LLM_MODEL, load_config
from telegram_agent.summarizer import _get_openrouter_client

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_REVIEW_MODEL = "gemini-1.5-flash"

ROOT = Path(__file__).resolve().parent

REVIEW_SYSTEM_PROMPT = """You are a senior quantitative researcher and systematic trading auditor.
Your job is to review trading algorithm implementations (code + described behavior) and find:

1. **Lookahead / future-data leakage**: any use of information not knowable at decision time
   (e.g. labels, centered windows, train/test bleed, using the same bar's close after it should
   be unknown, mis-ordered joins, shuffled time series).
2. **Implementation bugs**: off-by-one errors, wrong interval (daily vs hourly), unit mistakes,
   inconsistent symbol normalization, division by zero, silent drops of NaNs.
3. **Statistical / research biases**: multiple testing, implicit survivorship, unrealistic fills,
   ignoring fees/slippage, non-comparable backtests, regime assumptions.
4. **Philosophy / spec gaps**: strategy name vs what the code actually optimizes (e.g. RSI period
   defined for daily bars but applied to hourly series without restating assumptions).

Be concrete: cite function names, variables, or line-level logic when possible.
If something is *unclear* from the snippet, say what assumption must be verified (e.g. timestamp
is bar open vs close).

Respond with a single JSON object (no markdown fences) using this schema:
{
  "summary": "2-4 sentences",
  "severity_overall": "low" | "medium" | "high",
  "findings": [
    {
      "category": "lookahead" | "bug" | "bias" | "spec" | "hygiene",
      "severity": "low" | "medium" | "high",
      "title": "short label",
      "detail": "what is wrong and where in the code/flow",
      "mitigation": "how to fix or validate"
    }
  ],
  "correctness_notes": ["bullet affirming what appears walk-forward-correct or well-scoped"],
  "open_questions": ["what to verify outside the provided code"]
}
"""


def _read_text_limited(path: Path, *, max_lines: int = 500) -> str:
    if not path.is_file():
        return f"[missing file: {path}]\n"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    head = lines[:max_lines]
    out = "\n".join(head)
    if len(lines) > max_lines:
        out += f"\n\n... [{len(lines) - max_lines} more lines truncated]\n"
    return out


def _bundle_rsi_mean_reversion() -> str:
    """Source context for RSI mean-reversion across competitive bots + hourly sim + live."""
    parts: List[str] = []
    files: List[Tuple[Path, int]] = [
        (ROOT / "competitive_bots.py", 220),
        (ROOT / "rsi_portfolio_simulator.py", 320),
        (ROOT / "rsi_alpaca_live.py", 260),
        (ROOT / "agent_db.py", 80),
    ]
    for fp, lim in files:
        parts.append(f"=== FILE: {fp.name} (first ~{lim} lines) ===\n")
        if fp.name == "agent_db.py":
            # Only the daily series helper used by competitive bots (lookahead-relevant).
            text = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            # Slice around get_adj_closes_series
            start = next((i for i, ln in enumerate(text) if ln.startswith("def get_adj_closes_series")), 0)
            chunk = text[start : start + 45]
            parts.append("\n".join(chunk))
            parts.append("\n")
            continue
        parts.append(_read_text_limited(fp, max_lines=lim))
        parts.append("\n")
    parts.append(
        """=== CONTEXT (not code) ===
- `rsi_mean_reversion` in competitive_bots ranks symbols using **daily** adjusted closes via
  get_adj_closes_series(..., interval=\"1d\").
- `rsi_portfolio_simulator` and `rsi_alpaca_live` use the **same** _mean_reversion_score on
  **hourly** close sequences.
- _mean_reversion_score internally uses RSI(14) and a 5-bar momentum; bar size is whatever closes
  are passed in (day vs hour).
"""
    )
    return "\n".join(parts)


STRATEGY_BUNDLES = {
    "rsi_mean_reversion": _bundle_rsi_mean_reversion,
}


def _static_rsi_mean_preflight() -> Dict[str, Any]:
    """Cheap checks without an LLM (hints only; not a substitute for code review)."""
    notes = [
        "Competitive bot path uses get_adj_closes_series(..., end_ts=now, ts_utc <= end_ts) — daily bars should not include future dates if DB timestamps are correct.",
        "Hourly simulator advances each symbol's deque only up to the reference bar time t; scoring uses closes available at t — typical end-of-bar convention (signal after bar close).",
        "The same _mean_reversion_score on hourly vs daily bars implies different economic meaning (14 hours vs 14 days); confirm this is intentional.",
        "Cross-sectional ranking uses a single reference symbol's hourly calendar; symbols with sparse hours can have 'stale' closes vs the reference clock — worth validating.",
    ]
    return {"static_preflight_notes": notes}


def _run_review_openrouter(client: Any, model: str, user: str, max_tokens: int) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0.15,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    return (resp.choices[0].message.content or "").strip()


def _run_review_openrouter_fallback(client: Any, model: str, user: str, max_tokens: int) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0.15,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def _run_review_gemini(user: str, model_name: str, max_tokens: int) -> str:
    import google.generativeai as genai

    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    genai.configure(api_key=key)
    model = genai.GenerativeModel(model_name)
    full = f"{REVIEW_SYSTEM_PROMPT}\n\n---\n\n{user}"
    resp = model.generate_content(
        full,
        generation_config={
            "temperature": 0.15,
            "max_output_tokens": max_tokens,
        },
    )
    return (resp.text or "").strip()


def run_strategy_review(
    strategy_id: str,
    cfg: dict,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    bundle_fn = STRATEGY_BUNDLES.get(strategy_id)
    if not bundle_fn:
        raise ValueError(f"Unknown strategy: {strategy_id}. Known: {list(STRATEGY_BUNDLES)}")

    code_bundle = bundle_fn()
    static_block = _static_rsi_mean_preflight() if strategy_id == "rsi_mean_reversion" else {}

    user = f"""Review the following implementation for strategy id: {strategy_id!r}.

{json.dumps(static_block, indent=2)}

=== CODE AND CONTEXT ===
{code_bundle}
"""

    or_model = os.getenv("STRATEGY_REVIEW_MODEL", "").strip()
    gem_model = os.getenv("STRATEGY_REVIEW_GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_REVIEW_MODEL

    out: Dict[str, Any] = {
        "strategy_id": strategy_id,
        "model": cfg.get("strategy_review_model") or or_model or cfg.get("llm_model", DEFAULT_LLM_MODEL),
        "static_preflight": static_block,
    }

    if dry_run:
        out["llm_review"] = None
        out["skipped"] = "dry_run"
        return out

    max_tokens = int(os.getenv("STRATEGY_REVIEW_MAX_TOKENS", "8192"))
    raw: Optional[str] = None
    backend = "openrouter"

    client = _get_openrouter_client()
    if client:
        model = out["model"]
        try:
            raw = _run_review_openrouter(client, model, user, max_tokens)
        except Exception as e:
            logger.warning("OpenRouter json_object failed: %s; retrying without response_format", e)
            try:
                raw = _run_review_openrouter_fallback(client, model, user, max_tokens)
            except Exception as e2:
                logger.warning("OpenRouter failed: %s", e2)
                raw = None
    if raw is None and os.getenv("GEMINI_API_KEY", "").strip():
        backend = "gemini"
        out["model"] = gem_model
        try:
            raw = _run_review_gemini(user, gem_model, max_tokens)
        except Exception as e:
            out["llm_review"] = None
            out["error"] = f"Gemini review failed: {e}"
            return out

    if raw is None:
        out["llm_review"] = None
        out["error"] = (
            "No LLM configured: set OPENROUTER_API_KEY (preferred) or GEMINI_API_KEY for strategy review."
        )
        return out

    out["llm_backend"] = backend
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0].strip()
    try:
        out["llm_review"] = json.loads(raw)
    except json.JSONDecodeError:
        out["llm_review_raw"] = raw
        out["llm_review"] = None
        out["error"] = "LLM returned non-JSON"

    return out


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="Strategy review agent (LLM + static preflight)")
    p.add_argument(
        "--strategy",
        type=str,
        default="rsi_mean_reversion",
        choices=sorted(STRATEGY_BUNDLES.keys()),
    )
    p.add_argument("--dry-run", action="store_true", help="Skip LLM; emit bundle + static notes only")
    p.add_argument(
        "--out",
        type=str,
        default=None,
        help=f"Write JSON result (default: {DATA_DIR}/strategy_review_<strategy>.json)",
    )
    args = p.parse_args()

    repo = Path(__file__).resolve().parents[1]
    _load_env_file(repo / ".env")
    _load_env_file(Path(__file__).resolve().parent / ".env")
    try:
        from dotenv import load_dotenv

        load_dotenv(repo / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    cfg = load_config()
    result = run_strategy_review(args.strategy, cfg, dry_run=bool(args.dry_run))

    out_path = Path(args.out) if args.out else DATA_DIR / f"strategy_review_{args.strategy}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
    print("")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
