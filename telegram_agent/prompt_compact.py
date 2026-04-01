"""Build token-efficient prompt snippets from news items."""
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import NewsItem

# Trailing Telegram footer lines (e.g. "📡 @Channel")
_FOOTER_LINE_RE = re.compile(
    r"^\s*(📡\s*)?@[\w]+\s*$",
    re.UNICODE,
)


def strip_telegram_boilerplate(text: str) -> str:
    """Drop trailing @channel / emoji footer lines."""
    if not text:
        return ""
    lines = text.strip().split("\n")
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        if _FOOTER_LINE_RE.match(last):
            lines.pop()
            continue
        break
    return "\n".join(lines).strip()


def first_block(text: str, max_chars: int) -> str:
    """First paragraph or first line cluster, capped (word-safe when possible)."""
    if not text or max_chars <= 0:
        return ""
    t = text.strip()
    if "\n\n" in t:
        t = t.split("\n\n", 1)[0].strip()
    if len(t) > max_chars:
        cut = t[:max_chars]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        else:
            cut = cut[: max_chars - 1]
        t = cut + "…"
    return t


def _merge_title_excerpt(title: str, excerpt: str) -> str:
    """Avoid repeating the same first line as title and body."""
    title = (title or "").strip()
    excerpt = (excerpt or "").strip()
    if not excerpt:
        return title
    first = excerpt.split("\n", 1)[0].strip()
    if title and first == title:
        rest = excerpt[len(first) :].lstrip("\n")
        return title + ("\n" + rest if rest else "")
    return f"{title}\n{excerpt}".strip() if title else excerpt


def compact_body_for_item(
    source_type: str,
    title: str,
    content: str,
    style: str,
    max_telegram: int,
    max_rss: int,
) -> str:
    """
    style minimal — short excerpt; balanced — default caps; full — larger caps.
    """
    title = (title or "").strip()
    raw = (content or "").strip()

    if source_type == "telegram":
        raw = strip_telegram_boilerplate(raw)
        if style == "minimal":
            cap = min(max_telegram, 140)
        elif style == "full":
            cap = max(max_telegram, 450)
        else:
            cap = max_telegram
        excerpt = first_block(raw, cap)
        return _merge_title_excerpt(title, excerpt)

    # rss + twitter (same excerpt caps)
    if style == "minimal":
        cap = min(max_rss, 220)
    elif style == "full":
        cap = max(max_rss, 550)
    else:
        cap = max_rss
    excerpt = first_block(raw, cap)
    return _merge_title_excerpt(title, excerpt)


def item_to_prompt_snippet(item: "NewsItem", cfg: dict) -> str:
    """One compact block per item for the LLM user prompt."""
    condensed = getattr(item, "condensed", None)
    if condensed and str(condensed).strip():
        title = (item.title or "").strip()
        return f"[{item.source_name}] {title}\n{condensed.strip()}"

    style = (cfg.get("prompt_style") or "balanced").strip().lower()
    if style not in ("minimal", "balanced", "full"):
        style = "balanced"

    max_tg = int(cfg.get("max_snippet_telegram", 220))
    max_rss = int(cfg.get("max_snippet_rss", 380))

    st = item.source_type if item.source_type != "twitter" else "rss"
    text = compact_body_for_item(
        st,
        item.title,
        item.content,
        style,
        max_tg,
        max_rss,
    )
    return f"[{item.source_name}] {text}"
