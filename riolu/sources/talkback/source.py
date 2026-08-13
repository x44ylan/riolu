from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from riolu.models import IntelItem
from riolu.sources.base import dedupe_items
from riolu.text import clean_text


TALKBACK_URL = "https://talkback.sh/"


@dataclass(frozen=True)
class TalkbackSource:
    id: str = "talkback"
    name: str = "Talkback"
    category: str = "cybersecurity"
    description: str = "Talkback vulnerability and security research links."
    aliases: tuple[str, ...] = ("vulns", "vulnerability")

    async def fetch(self, limit: int, client: httpx.AsyncClient) -> list[IntelItem]:
        response = await client.get(TALKBACK_URL)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        return dedupe_items(_items_from_page(soup))[:limit]


def _items_from_page(soup: BeautifulSoup) -> list[IntelItem]:
    items: list[IntelItem] = []
    for anchor in soup.select('a[href^="http"], a[href^="/vulnerability/"]'):
        if not isinstance(anchor, Tag):
            continue

        href = urljoin(TALKBACK_URL, str(anchor.get("href", "")))
        if _is_chrome_link(href):
            continue

        parts = [clean_text(part) for part in anchor.stripped_strings]
        parts = [part for part in parts if part]
        if not parts:
            continue

        title = next((part for part in parts if _useful_title(part)), "")
        if not title:
            continue

        summary = next((part for part in parts[1:] if len(part) > 50), "")
        items.append(
            IntelItem(
                source_id="talkback",
                source_name="Talkback",
                title=title,
                url=href,
                summary=summary[:280],
                category="cybersecurity",
                tags=("cyber", "vulnerability"),
            )
        )
    return items


def _is_chrome_link(url: str) -> bool:
    host = urlparse(url).netloc.removeprefix("www.")
    if host in {"elttam.com", "s3.talkback.sh"}:
        return True
    return url in {TALKBACK_URL, urljoin(TALKBACK_URL, "/resources/")}


def _useful_title(title: str) -> bool:
    if len(title) < 4 or len(title) > 160:
        return False
    blocked = {"home", "login", "sign in", "sign up", "rss", "github", "twitter", "x"}
    return title.casefold() not in blocked


SOURCE = TalkbackSource()
