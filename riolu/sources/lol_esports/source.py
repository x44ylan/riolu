from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from riolu.feeds import parse_feed
from riolu.models import IntelItem
from riolu.sources.base import dedupe_items
from riolu.text import clean_text


LOGGER = logging.getLogger(__name__)

OFFICIAL_LOL_NEWS_URL = "https://www.leagueoflegends.com/en-us/news/"
LOL_ESPORTS_NEWS_URL = "https://lolesports.com/news"
LOLESPORTS_REDDIT_FEED = "https://www.reddit.com/r/lolesports/.rss"

KEYWORDS = (
    "lck",
    "lcs",
    "msi",
    "worlds",
    "international",
    "lol esports",
    "league of legends esports",
    "first stand",
    "t1",
    "gen.g",
    "hanwha",
    "dplus",
    "kt rolster",
    "flyquest",
    "cloud9",
    "team liquid",
    "100 thieves",
    "nrg",
)

BLOCKED_URL_PARTS = (
    "merch.riotgames.com",
)

BLOCKED_TITLE_PARTS = (
    "jacket",
    "jersey",
    "figure",
    "statue",
)


@dataclass(frozen=True)
class LolEsportsSource:
    id: str = "lol_esports"
    name: str = "LoL Esports"
    category: str = "esports"
    description: str = "LCK, LCS, and international League of Legends esports updates."
    aliases: tuple[str, ...] = ("lol", "league", "lck", "lcs", "worlds", "msi")

    async def fetch(self, limit: int, client: httpx.AsyncClient) -> list[IntelItem]:
        results = await asyncio.gather(
            _official_news(client, OFFICIAL_LOL_NEWS_URL),
            _official_news(client, LOL_ESPORTS_NEWS_URL),
            _reddit_news(client),
            return_exceptions=True,
        )

        items: list[IntelItem] = []
        for result in results:
            if isinstance(result, BaseException):
                LOGGER.warning("LoL esports source branch failed: %s", result)
                continue
            items.extend(result)
        return dedupe_items(items)[:limit]


async def _official_news(client: httpx.AsyncClient, url: str) -> list[IntelItem]:
    response = await client.get(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    items: list[IntelItem] = []
    for anchor in soup.select('a[data-testid="articlefeaturedcard-component"][href]'):
        if not isinstance(anchor, Tag):
            continue

        title_node = anchor.select_one('[data-testid="card-title"]')
        if title_node is None:
            continue

        title = clean_text(title_node.get_text(" ", strip=True))
        summary_node = anchor.select_one('[data-testid="card-description"]')
        summary = clean_text(summary_node.get_text(" ", strip=True)) if summary_node else ""
        if not title or not _matches_esports(title, summary):
            continue

        time_node = anchor.select_one("time[datetime]")
        published_at = _parse_iso_datetime(str(time_node.get("datetime", ""))) if isinstance(time_node, Tag) else None
        href = str(anchor.get("href", ""))
        if _is_blocked_item(title, href):
            continue
        items.append(
            IntelItem(
                source_id="lol_esports",
                source_name="LoL Esports",
                title=title,
                url=urljoin(url, href),
                summary=summary,
                category="esports",
                tags=_tags_for(title, summary),
                published_at=published_at,
            )
        )
    return items


async def _reddit_news(client: httpx.AsyncClient) -> list[IntelItem]:
    response = await client.get(LOLESPORTS_REDDIT_FEED)
    response.raise_for_status()
    items = parse_feed(
        response.text,
        source_id="lol_esports",
        source_name="r/lolesports",
        category="esports",
        tags=("league-of-legends", "esports", "community"),
        feed_url=LOLESPORTS_REDDIT_FEED,
    )
    return [
        IntelItem(
            source_id=item.source_id,
            source_name=item.source_name,
            title=item.title,
            url=item.url,
            summary=item.summary,
            category=item.category,
            tags=_tags_for(item.title, item.summary) or item.tags,
            published_at=item.published_at,
        )
        for item in items
        if _matches_esports(item.title, item.summary)
    ]


def _matches_esports(title: str, summary: str) -> bool:
    haystack = f"{title} {summary}".casefold()
    return any(keyword in haystack for keyword in KEYWORDS)


def _is_blocked_item(title: str, url: str) -> bool:
    title_lower = title.casefold()
    url_lower = url.casefold()
    return any(part in url_lower for part in BLOCKED_URL_PARTS) or any(part in title_lower for part in BLOCKED_TITLE_PARTS)


def _tags_for(title: str, summary: str) -> tuple[str, ...]:
    haystack = f"{title} {summary}".casefold()
    tags = ["league-of-legends", "esports"]
    if "lck" in haystack or "t1" in haystack or "gen.g" in haystack:
        tags.append("lck")
    if "lcs" in haystack or "flyquest" in haystack or "cloud9" in haystack or "team liquid" in haystack:
        tags.append("lcs")
    if "msi" in haystack or "worlds" in haystack or "international" in haystack:
        tags.append("international")
    return tuple(dict.fromkeys(tags))


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


SOURCE = LolEsportsSource()
