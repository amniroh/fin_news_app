"""Token counting and USD cost estimates for micro + digest LLM calls."""
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Approximate USD per 1M tokens (override via env ESTIMATE_*). Sources: public list prices, rounded.
DEFAULT_MODEL_PRICING = {
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-2.0-flash": (0.10, 0.40),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "anthropic/claude-3-haiku": (0.25, 1.25),
    "anthropic/claude-3.5-sonnet": (3.0, 15.0),
    "anthropic/claude-3.5-haiku": (1.0, 5.0),
}


def count_tokens(text: str) -> int:
    """Best-effort token count; prefers tiktoken, else chars/4 heuristic."""
    if not text:
        return 0
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def _pricing_for_model(model: str) -> Tuple[float, float]:
    """Returns (input_per_1m_usd, output_per_1m_usd)."""
    # Env override: ESTIMATE_ANTHROPIC_CLAUDE_3_5_SONNET_INPUT_PER_1M=3.0
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", model).strip("_").upper()
    inp = os.getenv(f"ESTIMATE_{slug}_INPUT_PER_1M")
    out = os.getenv(f"ESTIMATE_{slug}_OUTPUT_PER_1M")
    if inp and out:
        try:
            return float(inp), float(out)
        except ValueError:
            pass
    for key, (i, o) in DEFAULT_MODEL_PRICING.items():
        if key in model or model.endswith(key.split("/")[-1]):
            return i, o
    # Fallback
    return float(os.getenv("ESTIMATE_DEFAULT_INPUT_PER_1M", "1.0")), float(
        os.getenv("ESTIMATE_DEFAULT_OUTPUT_PER_1M", "5.0")
    )


def usd_for_tokens(tokens: int, per_million: float) -> float:
    return (tokens / 1_000_000.0) * per_million


def estimate_digest_cost(
    system_prompt: str,
    user_prompt: str,
    digest_model: str,
    assumed_output_tokens: int,
) -> Dict[str, Any]:
    inp_p, out_p = _pricing_for_model(digest_model)
    in_tok = count_tokens(system_prompt) + count_tokens(user_prompt)
    out_tok = assumed_output_tokens
    return {
        "model": digest_model,
        "input_tokens": in_tok,
        "output_tokens_est": out_tok,
        "input_usd": usd_for_tokens(in_tok, inp_p),
        "output_usd": usd_for_tokens(out_tok, out_p),
        "total_usd": usd_for_tokens(in_tok, inp_p) + usd_for_tokens(out_tok, out_p),
    }


def estimate_research_cost(
    system_prompt: str,
    user_prompt: str,
    model: str,
    *,
    assumed_output_tokens: int,
) -> Dict[str, Any]:
    """Token/USD estimate for a single chat completion (research agent dry-run)."""
    return estimate_digest_cost(system_prompt, user_prompt, model, assumed_output_tokens)


def estimate_micro_batch_cost(
    batch_user_json: str,
    system_prompt: str,
    micro_model: str,
    batch_output_tokens_est: int,
) -> Dict[str, Any]:
    inp_p, out_p = _pricing_for_model(micro_model)
    in_tok = count_tokens(system_prompt) + count_tokens(batch_user_json)
    out_tok = batch_output_tokens_est
    return {
        "model": micro_model,
        "input_tokens": in_tok,
        "output_tokens_est": out_tok,
        "input_usd": usd_for_tokens(in_tok, inp_p),
        "output_usd": usd_for_tokens(out_tok, out_p),
        "total_usd": usd_for_tokens(in_tok, inp_p) + usd_for_tokens(out_tok, out_p),
    }


def log_run_cost_summary(
    micro_batches: Optional[List[Dict[str, Any]]],
    digest_estimates: Optional[List[Tuple[str, Dict[str, Any]]]],
    *,
    dry_run: bool,
    micro_estimated_only: bool,
    micro_disabled: bool,
) -> None:
    """Log human-readable cost estimate. digest_estimates: [(label, estimate_dict), ...]."""
    lines = [
        "--- Cost estimate (USD, approximate; set ESTIMATE_<MODEL>_INPUT_PER_1M or ESTIMATE_DEFAULT_*; install tiktoken for better token counts) ---"
    ]
    total = 0.0
    if micro_disabled:
        lines.append("  Micro-summarizer: disabled (MICRO_SUMMARIZE=false)")
    elif micro_estimated_only:
        lines.append("  Micro-summarizer: not called (dry-run); figures below are estimated")
    elif micro_batches:
        lines.append("  Micro-summarizer: executed (batches below)")
    elif not micro_disabled:
        lines.append("  Micro-summarizer: 0 batches (no items met MICRO_MIN_* thresholds)")
    if micro_batches:
        m_total = 0.0
        for i, b in enumerate(micro_batches, 1):
            m_total += b.get("total_usd", 0)
            lines.append(
                f"  Micro batch {i}: model={b.get('model')} "
                f"~in={b.get('input_tokens')} tok ~out~={b.get('output_tokens_est')} tok "
                f"~${b.get('total_usd', 0):.5f}"
            )
        lines.append(f"  Micro subtotal: ~${m_total:.5f}")
        total += m_total
    if digest_estimates:
        for label, d in digest_estimates:
            du = d.get("total_usd", 0)
            total += du
            lines.append(
                f"  Digest [{label}]: model={d.get('model')} "
                f"~in={d.get('input_tokens')} tok ~out~={d.get('output_tokens_est')} tok "
                f"~${du:.5f}"
            )
    lines.append(f"  Run total (est.): ~${total:.5f}")
    if dry_run:
        lines.append("  Note: dry-run did not call the digest LLMs; digest lines are what a full run would add.")
    for line in lines:
        logger.info(line)
    # Dry-run also prints prompts to stdout; mirror cost lines there so they are not missed (logging is usually stderr).
    if dry_run:
        print("\n".join(lines), flush=True)


