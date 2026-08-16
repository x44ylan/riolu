from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx

from riolu.config import Settings
from riolu.rendering import RenderSection
from riolu.screens import Screen, digest, error, sources
from riolu.sources.base import Source
from riolu.sources.registry import SourceRegistry


PAGE_SIZE = 10


class NewsFeature:
    def __init__(
        self,
        settings: Settings,
        registry: SourceRegistry,
        http_client: Callable[[], httpx.AsyncClient],
    ) -> None:
        self.settings = settings
        self.registry = registry
        self._http_client = http_client

    async def execute(self, command: str, args: list[str]) -> Screen:
        source = self.registry.command_aliases().get(command)
        if source is not None:
            return await self._source_digest(
                source,
                _limit(args, self.settings),
                _page(args),
            )
        if command == "sources":
            return sources(self.registry.all())
        if command != "latest":
            return error("Unknown news command", command)
        return await self._latest(args)

    async def _latest(self, args: list[str]) -> Screen:
        if not args or args[0].isdigit():
            return sources(self.registry.all())

        token = args[0]
        if token.casefold() not in {"all", "*"}:
            source = self.registry.get(token)
            if source is None:
                return error("Unknown source", token, markup=sources(self.registry.all()).markup)
            source_args = args[1:]
            return await self._source_digest(
                source,
                _limit(source_args, self.settings),
                _page(source_args),
            )

        selected = self.registry.default_sources(self.settings.default_source_ids)
        sections = await self._fetch_sections(selected, _limit(args[1:], self.settings))
        return digest("Latest for you", sections)

    async def _source_digest(self, source: Source, limit: int, page: int = 0) -> Screen:
        result = await self._fetch_section(source, limit)
        if result.error:
            return digest(source.name, (result,), source_id=source.id)

        total = len(result.items)
        last_page = max(0, (total - 1) // PAGE_SIZE)
        page = min(page, last_page)
        start = page * PAGE_SIZE
        visible = result.items[start : start + PAGE_SIZE]
        title = source.name
        if total > PAGE_SIZE:
            title = f"{source.name} · {start + 1}–{start + len(visible)} of {total}"
        section = RenderSection(title=source.name, items=visible)
        return digest(
            title,
            (section,),
            source_id=source.id,
            limit=limit,
            max_limit=self.settings.max_limit,
            page=page,
            total=total,
        )

    async def _fetch_sections(self, sources: tuple[Source, ...], limit: int) -> list[RenderSection]:
        return list(await asyncio.gather(*(self._fetch_section(source, limit) for source in sources)))

    async def _fetch_section(self, source: Source, limit: int) -> RenderSection:
        try:
            items = await source.fetch(limit, self._http_client())
        except Exception as exc:
            return RenderSection(title=source.name, error=str(exc))
        return RenderSection(title=source.name, items=tuple(items))


def _limit(args: list[str], settings: Settings) -> int:
    if not args:
        return settings.default_limit
    try:
        return max(1, min(int(args[0]), settings.max_limit))
    except ValueError:
        return settings.default_limit


def _page(args: list[str]) -> int:
    if len(args) < 2:
        return 0
    try:
        return max(0, int(args[1]))
    except ValueError:
        return 0
