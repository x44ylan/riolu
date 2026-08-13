from __future__ import annotations

from dataclasses import dataclass

from riolu.sources.base import Source, matches_source_token, normalize_source_token
from riolu.sources.cyber_news import SOURCE as CYBER_NEWS
from riolu.sources.hello_github import SOURCE as HELLO_GITHUB
from riolu.sources.lol_esports import SOURCE as LOL_ESPORTS
from riolu.sources.opencode_releases import SOURCE as OPENCODE_RELEASES
from riolu.sources.talkback import SOURCE as TALKBACK
from riolu.sources.tech_learning import SOURCE as TECH_LEARNING


BUILTIN_SOURCES: tuple[Source, ...] = (
    HELLO_GITHUB,
    TALKBACK,
    LOL_ESPORTS,
    OPENCODE_RELEASES,
    CYBER_NEWS,
    TECH_LEARNING,
)

DEFAULT_SOURCE_IDS: tuple[str, ...] = (HELLO_GITHUB.id, TALKBACK.id)


@dataclass(frozen=True)
class SourceRegistry:
    sources: tuple[Source, ...] = BUILTIN_SOURCES

    def all(self) -> tuple[Source, ...]:
        return self.sources

    def default_sources(self, configured_ids: tuple[str, ...]) -> tuple[Source, ...]:
        if not configured_ids:
            return self.sources
        return tuple(source for source in (self.get(source_id) for source_id in configured_ids) if source is not None)

    def get(self, token: str) -> Source | None:
        for source in self.sources:
            if matches_source_token(source, token):
                return source
        return None

    def command_aliases(self) -> dict[str, Source]:
        aliases: dict[str, Source] = {}
        for source in self.sources:
            aliases[normalize_source_token(source.id)] = source
            for alias in source.aliases:
                aliases[normalize_source_token(alias)] = source
        return aliases
