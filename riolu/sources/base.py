from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

from riolu.feeds import parse_feed
from riolu.models import IntelItem


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedSpec:
    id: str
    name: str
    url: str
    tags: tuple[str, ...] = ()


class Source(Protocol):
    id: str
    name: str
    category: str
    description: str
    aliases: tuple[str, ...]

    async def fetch(self, limit: int, client: httpx.AsyncClient) -> list[IntelItem]:
        ...


@dataclass(frozen=True)
class RssBundleSource:
    id: str
    name: str
    category: str
    description: str
    feeds: tuple[FeedSpec, ...]
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()

    async def fetch(self, limit: int, client: httpx.AsyncClient) -> list[IntelItem]:
        items: list[IntelItem] = []
        errors: list[str] = []
        for feed in self.feeds:
            try:
                response = await client.get(feed.url)
                response.raise_for_status()
                items.extend(
                    parse_feed(
                        response.text,
                        source_id=self.id,
                        source_name=feed.name,
                        category=self.category,
                        tags=feed.tags,
                        feed_url=feed.url,
                    )
                )
            except Exception as exc:
                LOGGER.warning("Feed failed for %s (%s): %s", self.id, feed.url, exc)
                errors.append(f"{feed.name}: {exc}")

        filtered = _filter_keywords(items, self.keywords)
        if not items and errors:
            raise RuntimeError("; ".join(errors))
        return _sort_recent(dedupe_items(filtered))[:limit]


def dedupe_items(items: list[IntelItem]) -> list[IntelItem]:
    seen: set[str] = set()
    result: list[IntelItem] = []
    for item in items:
        key = item.identity.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def matches_source_token(source: Source, token: str) -> bool:
    normalized = normalize_source_token(token)
    names = {
        normalize_source_token(source.id),
        normalize_source_token(source.name),
        *(normalize_source_token(alias) for alias in source.aliases),
    }
    return normalized in names


def normalize_source_token(value: str) -> str:
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


def _filter_keywords(items: list[IntelItem], keywords: tuple[str, ...]) -> list[IntelItem]:
    if not keywords:
        return items

    needles = tuple(keyword.casefold() for keyword in keywords)
    result: list[IntelItem] = []
    for item in items:
        haystack = f"{item.title} {item.summary} {' '.join(item.tags)}".casefold()
        if any(keyword in haystack for keyword in needles):
            result.append(item)
    return result


def _sort_recent(items: list[IntelItem]) -> list[IntelItem]:
    oldest = datetime.min.replace(tzinfo=UTC)
    return sorted(items, key=lambda item: item.published_at or oldest, reverse=True)
