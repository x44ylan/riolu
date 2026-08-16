from __future__ import annotations

from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

from riolu.models import IntelItem
from riolu.text import clean_text


ONETRACKER_URL = "https://onetracker.org/"
FALLBACK_TITLE = "OneTracker cybersecurity hub"
FALLBACK_SUMMARY = (
    "Track security tools, open-source projects, governance, risk, compliance, "
    "research reports, ransomware, artificial intelligence, and news timelines."
)


@dataclass(frozen=True)
class OneTrackerSource:
    id: str = "onetracker"
    name: str = "OneTracker"
    category: str = "cybersecurity"
    description: str = "Cybersecurity tools, research, GRC, ransomware, AI, and news timelines."
    aliases: tuple[str, ...] = ("one_tracker", "security_tracker")

    async def fetch(self, limit: int, client: httpx.AsyncClient) -> list[IntelItem]:
        if limit < 1:
            return []
        response = await client.get(ONETRACKER_URL)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        title = _meta_content(soup, "meta[property='og:title']") or FALLBACK_TITLE
        summary = _meta_content(soup, "meta[property='og:description']") or FALLBACK_SUMMARY
        return [
            IntelItem(
                source_id=self.id,
                source_name=self.name,
                title=title,
                url=ONETRACKER_URL,
                summary=summary,
                category=self.category,
                tags=("cyber", "research", "grc"),
            )
        ]


def _meta_content(soup: BeautifulSoup, selector: str) -> str:
    element = soup.select_one(selector)
    if element is None:
        return ""
    return clean_text(str(element.get("content", "")))


SOURCE = OneTrackerSource()
