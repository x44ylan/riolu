from __future__ import annotations

import httpx

from riolu.models import IntelItem
from riolu.sources.registry import SourceRegistry


async def scrape_hellogithub(limit: int, timeout: float) -> list[IntelItem]:
    return await _fetch_source("hello_github", limit, timeout)


async def scrape_talkback(limit: int, timeout: float) -> list[IntelItem]:
    return await _fetch_source("talkback", limit, timeout)


async def _fetch_source(source_id: str, limit: int, timeout: float) -> list[IntelItem]:
    source = SourceRegistry().get(source_id)
    if source is None:
        raise RuntimeError(f"Unknown source: {source_id}")

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        return await source.fetch(limit, client)
