from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class IntelItem:
    source_id: str
    source_name: str
    title: str
    url: str
    summary: str = ""
    category: str = "general"
    tags: tuple[str, ...] = ()
    published_at: datetime | None = None

    @property
    def identity(self) -> str:
        return self.url or f"{self.source_id}:{self.title.casefold()}"
