"""Persistent store for seen item IDs (deduplication)."""
import json
import logging
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)


class SeenStore:
    """File-based set of seen item IDs with optional max size."""

    def __init__(self, path: str, max_size: int = 5000):
        self.path = Path(path)
        self.max_size = max_size
        self._ids: Set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
            self._ids = set(data.get("ids", []))
        except Exception as e:
            logger.warning("Could not load seen store %s: %s", self.path, e)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ids_list = list(self._ids)
        if len(ids_list) > self.max_size:
            ids_list = ids_list[-self.max_size:]
            self._ids = set(ids_list)
        with open(self.path, "w") as f:
            json.dump({"ids": ids_list}, f, indent=0)

    def add(self, item_id: str) -> None:
        self._ids.add(item_id)
        self._save()

    def add_many(self, item_ids: list) -> None:
        self._ids.update(item_ids)
        self._save()

    def seen(self, item_id: str) -> bool:
        return item_id in self._ids

    def filter_unseen(self, item_ids: list) -> list:
        return [i for i in item_ids if i not in self._ids]
