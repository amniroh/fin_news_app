"""Data models for news items and digest."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class NewsItem:
    """A single item from Telegram or RSS."""
    id: str
    source_type: str  # 'telegram' | 'rss' | 'twitter'
    source_name: str
    title: str
    content: str
    url: Optional[str]
    timestamp: datetime
    condensed: Optional[str] = None  # 1–2 sentence micro-summary when MICRO_SUMMARIZE is used

    def to_input_snippet(self, max_content_len: int = 500) -> str:
        """Short text for LLM input."""
        content = (self.content or "")[:max_content_len]
        return f"[{self.source_name}] {self.title}\n{content}"
