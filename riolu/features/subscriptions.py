from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

import httpx

from riolu.config import Settings
from riolu.rendering import RenderSection
from riolu.screens import (
    Screen,
    confirm_feed_delete,
    digest,
    error,
    feed_detail,
    notice,
    subscription_added,
    subscriptions,
)
from riolu.sources.base import FeedSpec, RssBundleSource, Source
from riolu.state import StateStore


class SubscriptionsFeature:
    COMMANDS = frozenset(
        {
            "subscribe", "sub", "feeds", "subscriptions", "subs", "unsubscribe", "unsub", "deletefeed",
            "get", "pause", "resume", "filter", "feed_open", "feed_get", "feed_pause",
            "feed_resume", "feed_confirm_delete", "feed_delete",
        }
    )

    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        http_client: Callable[[], httpx.AsyncClient],
    ) -> None:
        self.settings = settings
        self.store = store
        self._http_client = http_client

    async def execute(self, chat_id: int, command: str, args: list[str]) -> Screen:
        if command in {"feeds", "subscriptions", "subs"}:
            return subscriptions(await self.store.subscriptions_for_chat(chat_id))
        if command == "feed_open":
            return await self._detail(chat_id, args)
        if command == "feed_confirm_delete":
            return await self._confirm_delete(chat_id, args)
        if command == "feed_delete":
            return await self._unsubscribe(chat_id, args)
        if command in {"subscribe", "sub"}:
            return await self._subscribe(chat_id, args)
        if command in {"unsubscribe", "unsub", "deletefeed"}:
            return await self._unsubscribe(chat_id, args)
        if command in {"get", "feed_get"}:
            return await self._get(chat_id, args)
        if command in {"pause", "resume", "feed_pause", "feed_resume"}:
            paused = command in {"pause", "feed_pause"}
            return await self._set_paused(chat_id, args, paused=paused, show_detail=command.startswith("feed_"))
        if command == "filter":
            return await self._set_filter(chat_id, args)
        return error("Unknown feed command", command)

    async def _detail(self, chat_id: int, args: list[str]) -> Screen:
        value = await self._find(chat_id, args)
        return feed_detail(value) if value is not None else error("Feed settings", "No matching feed found.")

    async def _confirm_delete(self, chat_id: int, args: list[str]) -> Screen:
        value = await self._find(chat_id, args)
        return confirm_feed_delete(value) if value is not None else error("Remove feed", "No matching feed found.")

    async def _subscribe(self, chat_id: int, args: list[str]) -> Screen:
        if not args:
            return error("RSS subscribe", "Use /subscribe <feed_url> [name].")
        url = args[0].strip()
        if not _looks_like_url(url):
            return error("RSS subscribe", "That does not look like an http(s) feed URL.")

        title = " ".join(args[1:]).strip() or _title_from_url(url)
        preview = subscription_source({"id": "preview", "url": url, "title": title})
        section = await self._fetch(preview, self.settings.max_limit)
        if section.error:
            return error("Could not add feed", section.error)

        value = await self.store.add_subscription(chat_id, url, title)
        await self.store.mark_seen(chat_id, str(value.get("id", "")), [item.identity for item in section.items])
        return subscription_added(value)

    async def _unsubscribe(self, chat_id: int, args: list[str]) -> Screen:
        if not args:
            return error("RSS unsubscribe", "Use /unsubscribe <id|url>.")
        removed = await self.store.remove_subscription(chat_id, args[0])
        if removed is None:
            return error("RSS unsubscribe", "No matching feed found.")
        return notice("Feed removed", str(removed.get("title", "RSS feed")))

    async def _get(self, chat_id: int, args: list[str]) -> Screen:
        if not args:
            return error("Fetch feed", "Use /get <id> [limit].")
        value = _find(await self.store.subscriptions_for_chat(chat_id), args[0])
        if value is None:
            return error("Fetch feed", "No matching feed found.")
        source = subscription_source(value)
        section = await self._fetch(source, _limit(args[1:], self.settings))
        return digest(source.name, (section,), source_id=source.id)

    async def _set_paused(
        self,
        chat_id: int,
        args: list[str],
        *,
        paused: bool,
        show_detail: bool = False,
    ) -> Screen:
        if not args:
            command = "pause" if paused else "resume"
            return error("Feed delivery", f"Use /{command} <id>.")
        value = await self.store.set_subscription_paused(chat_id, args[0], paused)
        if value is None:
            return error("Feed delivery", "No matching feed found.")
        if show_detail:
            return feed_detail(value)
        action = "paused" if paused else "resumed"
        return notice(f"Feed {action}", str(value.get("title", "RSS feed")))

    async def _set_filter(self, chat_id: int, args: list[str]) -> Screen:
        if len(args) < 2:
            return error("Feed filter", "Use /filter <id> keyword1, keyword2—or /filter <id> off.")
        raw = " ".join(args[1:]).strip()
        keywords = [] if raw.casefold() in {"off", "none", "clear"} else raw.split(",")
        value = await self.store.set_subscription_keywords(chat_id, args[0], keywords)
        if value is None:
            return error("Feed filter", "No matching feed found.")
        active = value.get("keywords", [])
        detail = ", ".join(str(keyword) for keyword in active) if active else "All items"
        return notice("Feed filter updated", detail)

    async def _find(self, chat_id: int, args: list[str]) -> dict[str, object] | None:
        if not args:
            return None
        return _find(await self.store.subscriptions_for_chat(chat_id), args[0])

    async def _fetch(self, source: Source, limit: int) -> RenderSection:
        try:
            items = await source.fetch(limit, self._http_client())
        except Exception as exc:
            return RenderSection(title=source.name, error=str(exc))
        return RenderSection(title=source.name, items=tuple(items))


def subscription_source(value: dict[str, object]) -> Source:
    title = str(value.get("title") or "RSS feed")
    url = str(value.get("url") or "")
    source_id = str(value.get("id") or _title_from_url(url))
    keywords = tuple(str(keyword) for keyword in value.get("keywords", []) if str(keyword).strip())
    return RssBundleSource(
        id=source_id,
        name=title,
        category="rss",
        description=url,
        feeds=(FeedSpec(id=source_id, name=title, url=url, tags=("rss", "custom")),),
        aliases=(),
        keywords=keywords,
    )


def _find(values: list[dict[str, object]], token: str) -> dict[str, object] | None:
    normalized = token.strip().casefold()
    for value in values:
        if normalized in {str(value.get("id", "")).casefold(), str(value.get("url", "")).casefold()}:
            return value
    return None


def _limit(args: list[str], settings: Settings) -> int:
    if not args:
        return settings.default_limit
    try:
        return max(1, min(int(args[0]), settings.max_limit))
    except ValueError:
        return settings.default_limit


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _title_from_url(value: str) -> str:
    return urlparse(value).netloc or "RSS feed"
