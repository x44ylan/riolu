from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from riolu.config import Settings
from riolu.reminders import parse_reminder_request
from riolu.rendering import (
    RenderSection,
    ReplyMarkup,
    back_to_menu_markup,
    digest_markup,
    format_datetime,
    main_menu_markup,
    render_digest,
    render_help,
    render_loading,
    render_notice,
    render_plain_error,
    render_reminder_added,
    render_reminder_due,
    render_reminders,
    render_sources,
    render_subscription_added,
    render_subscriptions,
    render_welcome,
    sources_markup,
)
from riolu.sources.base import FeedSpec, RssBundleSource, Source, normalize_source_token
from riolu.sources.registry import SourceRegistry
from riolu.state import StateStore
from riolu.telegram import TelegramAPI


LOGGER = logging.getLogger(__name__)

BOT_COMMANDS: tuple[dict[str, str], ...] = (
    {"command": "start", "description": "Open the Riolu menu"},
    {"command": "latest", "description": "Fetch your latest digest"},
    {"command": "sources", "description": "Browse information sources"},
    {"command": "cyber", "description": "Security headlines"},
    {"command": "tech", "description": "Daily technical learning"},
    {"command": "lol", "description": "League of Legends esports"},
    {"command": "opencode", "description": "Opencode releases"},
    {"command": "subscriptions", "description": "Manage custom feeds"},
    {"command": "remind", "description": "Create a reminder"},
    {"command": "reminders", "description": "View queued reminders"},
    {"command": "help", "description": "Show examples and help"},
)


class RioluBot:
    def __init__(
        self,
        settings: Settings,
        *,
        registry: SourceRegistry | None = None,
        store: StateStore | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry or SourceRegistry()
        self.store = store or StateStore(settings.state_path)
        self.http: httpx.AsyncClient | None = None
        self.telegram: TelegramAPI | None = None

    async def run(self) -> None:
        timeout = httpx.Timeout(self.settings.http_timeout_seconds, read=50.0)
        headers = {"User-Agent": "riolu/0.2 (+https://github.com/Dylan-Liew/riolu)"}
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout) as client:
            self.http = client
            self.telegram = TelegramAPI(self.settings.telegram_token, client)
            await self._configure_telegram()

            tasks = {
                asyncio.create_task(self._feed_loop()),
                asyncio.create_task(self._reminder_loop()),
            }
            for task in tasks:
                task.add_done_callback(_log_background_failure)

            try:
                await self._poll_forever()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.telegram = None
                self.http = None

    async def _configure_telegram(self) -> None:
        try:
            await self._telegram.set_my_commands(BOT_COMMANDS)
        except Exception:
            LOGGER.warning("Could not update Telegram command menu", exc_info=True)

    async def _respond(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: ReplyMarkup | None = None,
        message_id: int | None = None,
    ) -> int | None:
        if message_id is not None:
            await self._telegram.edit_message_text(
                chat_id,
                message_id,
                text,
                reply_markup=reply_markup,
            )
            return message_id

        sent = await self._telegram.send_message(chat_id, text, reply_markup=reply_markup)
        try:
            return int(sent.get("message_id"))
        except (AttributeError, TypeError, ValueError):
            return None

    async def _show_loading(self, chat_id: int, label: str, *, message_id: int | None) -> int | None:
        return await self._respond(
            chat_id,
            render_loading(label),
            reply_markup={"inline_keyboard": []},
            message_id=message_id,
        )

    async def _poll_forever(self) -> None:
        offset = 0
        LOGGER.info("Riolu is polling Telegram")
        while True:
            try:
                updates = await self._telegram.get_updates(offset=offset, timeout=45)
            except Exception:
                LOGGER.exception("Telegram polling failed")
                await asyncio.sleep(5)
                continue

            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                try:
                    await self._handle_update(update)
                except Exception:
                    LOGGER.exception("Failed to handle Telegram update")

    async def _handle_update(self, update: dict[str, Any]) -> None:
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self._handle_callback_query(callback_query)
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None or not text.startswith("/"):
            return

        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return

        if not self._is_allowed(chat_id):
            LOGGER.warning("Rejected chat_id=%s", chat_id)
            await self._telegram.send_message(
                chat_id,
                render_plain_error("Private bot", "This chat is not on the allowlist."),
            )
            return

        await self.store.remember_chat(chat_id)

        command, args = _parse_command(text)
        try:
            await self._dispatch(chat_id, command, args)
        except Exception as exc:
            LOGGER.exception("Command failed: /%s", command)
            await self._telegram.send_message(
                chat_id,
                render_plain_error("Something went wrong", str(exc)),
                reply_markup=back_to_menu_markup(),
            )

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        callback_id = str(callback_query.get("id") or "")
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        try:
            chat_id = int(chat.get("id"))
            message_id = int(message.get("message_id"))
        except (TypeError, ValueError):
            return

        if not self._is_allowed(chat_id):
            LOGGER.warning("Rejected callback chat_id=%s", chat_id)
            if callback_id:
                await self._telegram.answer_callback_query(
                    callback_id,
                    text="This chat is not on the allowlist.",
                    show_alert=True,
                )
            return

        parsed = _parse_callback_data(str(callback_query.get("data") or ""))
        if parsed is None:
            if callback_id:
                await self._telegram.answer_callback_query(callback_id, text="That action has expired.")
            return

        if callback_id:
            await self._telegram.answer_callback_query(callback_id)
        await self.store.remember_chat(chat_id)
        command, args = parsed
        try:
            await self._dispatch(chat_id, command, args, message_id=message_id)
        except Exception as exc:
            LOGGER.exception("Button action failed: %s", callback_query.get("data"))
            await self._respond(
                chat_id,
                render_plain_error("Something went wrong", str(exc)),
                reply_markup=back_to_menu_markup(),
                message_id=message_id,
            )

    async def _dispatch(
        self,
        chat_id: int,
        command: str,
        args: list[str],
        *,
        message_id: int | None = None,
    ) -> None:
        normalized = normalize_source_token(command)
        source = self.registry.command_aliases().get(normalized)
        if source is not None:
            await self._send_source_digest(
                chat_id,
                source,
                _limit(args, self.settings),
                message_id=message_id,
            )
            return

        if normalized == "start":
            await self._respond(
                chat_id,
                render_welcome(),
                reply_markup=main_menu_markup(),
                message_id=message_id,
            )
        elif normalized == "help":
            await self._respond(
                chat_id,
                render_help(),
                reply_markup=back_to_menu_markup(),
                message_id=message_id,
            )
        elif normalized == "sources":
            sources = self.registry.all()
            await self._respond(
                chat_id,
                render_sources(sources),
                reply_markup=sources_markup(sources),
                message_id=message_id,
            )
        elif normalized == "latest":
            await self._handle_latest(chat_id, args, message_id=message_id)
        elif normalized in {"subscribe", "sub"}:
            await self._handle_subscribe(chat_id, args)
        elif normalized in {"subscriptions", "subs"}:
            subscriptions = await self.store.subscriptions_for_chat(chat_id)
            await self._respond(
                chat_id,
                render_subscriptions(subscriptions),
                reply_markup=back_to_menu_markup(),
                message_id=message_id,
            )
        elif normalized in {"unsubscribe", "unsub", "deletefeed"}:
            await self._handle_unsubscribe(chat_id, args)
        elif normalized == "remind":
            await self._handle_remind(chat_id, args)
        elif normalized == "reminders":
            await self._respond(
                chat_id,
                render_reminders(await self.store.reminders_for_chat(chat_id)),
                reply_markup=back_to_menu_markup(),
                message_id=message_id,
            )
        elif normalized in {"forget", "deletereminder"}:
            await self._handle_forget(chat_id, args)
        else:
            await self._respond(
                chat_id,
                render_plain_error(f"Unknown command: /{command}", "Open /help or use the menu below."),
                reply_markup=main_menu_markup(),
                message_id=message_id,
            )

    async def _handle_latest(self, chat_id: int, args: list[str], *, message_id: int | None = None) -> None:
        source_token = ""
        limit_args = args
        if args and not args[0].isdigit():
            if args[0].casefold() in {"all", "*"}:
                limit_args = args[1:]
            else:
                source_token = args[0]
                limit_args = args[1:]

        if source_token:
            source = self.registry.get(source_token)
            if source is None:
                await self._respond(
                    chat_id,
                    render_plain_error("Unknown source", source_token),
                    reply_markup=sources_markup(self.registry.all()),
                    message_id=message_id,
                )
                return
            await self._send_source_digest(
                chat_id,
                source,
                _limit(limit_args, self.settings),
                message_id=message_id,
            )
            return

        sources = self.registry.default_sources(self.settings.default_source_ids)
        message_id = await self._show_loading(chat_id, "Your default sources", message_id=message_id)
        sections = await self._fetch_sections(sources, _limit(limit_args, self.settings))
        await self._respond(
            chat_id,
            render_digest("Latest for you", sections),
            reply_markup=digest_markup(),
            message_id=message_id,
        )

    async def _send_source_digest(
        self,
        chat_id: int,
        source: Source,
        limit: int,
        *,
        message_id: int | None = None,
    ) -> None:
        message_id = await self._show_loading(chat_id, source.name, message_id=message_id)
        sections = await self._fetch_sections((source,), limit)
        await self._respond(
            chat_id,
            render_digest(source.name, sections),
            reply_markup=digest_markup(source.id),
            message_id=message_id,
        )

    async def _handle_subscribe(self, chat_id: int, args: list[str]) -> None:
        if not args:
            await self._telegram.send_message(
                chat_id,
                render_plain_error("RSS subscribe", "Use /subscribe <feed_url> [name]."),
                reply_markup=back_to_menu_markup(),
            )
            return

        url = args[0].strip()
        if not _looks_like_url(url):
            await self._telegram.send_message(
                chat_id,
                render_plain_error("RSS subscribe", "That does not look like an http(s) feed URL."),
                reply_markup=back_to_menu_markup(),
            )
            return

        title = " ".join(args[1:]).strip() or _title_from_url(url)
        source = _subscription_source({"id": "preview", "url": url, "title": title})
        message_id = await self._show_loading(chat_id, f"Validating {title}", message_id=None)
        sections = await self._fetch_sections((source,), 1)
        first = sections[0] if sections else RenderSection(title=title, error="Could not read feed.")
        if first.error:
            await self._respond(
                chat_id,
                render_plain_error("Could not add feed", first.error),
                reply_markup=back_to_menu_markup(),
                message_id=message_id,
            )
            return

        subscription = await self.store.add_subscription(chat_id, url, title)
        await self._respond(
            chat_id,
            render_subscription_added(subscription),
            reply_markup=back_to_menu_markup(),
            message_id=message_id,
        )

    async def _handle_unsubscribe(self, chat_id: int, args: list[str]) -> None:
        if not args:
            await self._telegram.send_message(
                chat_id,
                render_plain_error("RSS unsubscribe", "Use /unsubscribe <id|url>."),
                reply_markup=back_to_menu_markup(),
            )
            return

        removed = await self.store.remove_subscription(chat_id, args[0])
        if removed is None:
            await self._telegram.send_message(
                chat_id,
                render_plain_error("RSS unsubscribe", "No matching feed found."),
                reply_markup=back_to_menu_markup(),
            )
            return
        await self._telegram.send_message(
            chat_id,
            render_notice("Feed removed", str(removed.get("title", "RSS feed"))),
            reply_markup=back_to_menu_markup(),
        )

    async def _handle_remind(self, chat_id: int, args: list[str]) -> None:
        try:
            request = parse_reminder_request(
                " ".join(args),
                now=datetime.now(self.settings.timezone),
                timezone=self.settings.timezone,
            )
        except ValueError as exc:
            await self._telegram.send_message(
                chat_id,
                render_plain_error("Could not set reminder", str(exc)),
                reply_markup=back_to_menu_markup(),
            )
            return

        reminder = await self.store.add_reminder(chat_id, request.text, request.due_at)
        await self._telegram.send_message(
            chat_id,
            render_reminder_added(reminder, request.due_at),
            reply_markup=back_to_menu_markup(),
        )

    async def _handle_forget(self, chat_id: int, args: list[str]) -> None:
        if not args:
            await self._telegram.send_message(
                chat_id,
                render_plain_error("Forget reminder", "Use /forget <id>."),
                reply_markup=back_to_menu_markup(),
            )
            return

        removed = await self.store.remove_reminder(chat_id, args[0])
        if removed is None:
            await self._telegram.send_message(
                chat_id,
                render_plain_error("Forget reminder", "No matching reminder found."),
                reply_markup=back_to_menu_markup(),
            )
            return
        await self._telegram.send_message(
            chat_id,
            render_notice("Reminder removed", str(removed.get("text", "Reminder"))),
            reply_markup=back_to_menu_markup(),
        )

    async def _fetch_sections(self, sources: tuple[Source, ...], limit: int) -> list[RenderSection]:
        results = await asyncio.gather(*(self._fetch_section(source, limit) for source in sources))
        return list(results)

    async def _fetch_section(self, source: Source, limit: int) -> RenderSection:
        try:
            items = await source.fetch(limit, self._http)
        except Exception as exc:
            LOGGER.exception("Failed to fetch source %s", source.id)
            return RenderSection(title=source.name, error=str(exc))
        return RenderSection(title=source.name, items=tuple(items))

    async def _feed_loop(self) -> None:
        while True:
            try:
                await self._post_periodic_updates()
            except Exception:
                LOGGER.exception("Periodic feed failed")
            await asyncio.sleep(self.settings.post_interval_minutes * 60)

    async def _post_periodic_updates(self) -> None:
        chat_ids = await self.store.subscription_chat_ids()
        if self.settings.target_chat_id is not None:
            chat_ids.add(self.settings.target_chat_id)
            default_digest_chat_ids = {self.settings.target_chat_id}
        else:
            default_digest_chat_ids = await self.store.known_chat_ids()
            chat_ids.update(default_digest_chat_ids)

        for chat_id in chat_ids:
            if not self._is_allowed(chat_id):
                continue

            sources: list[Source] = []
            if chat_id in default_digest_chat_ids:
                sources.extend(self.registry.default_sources(self.settings.default_source_ids))
            subscriptions = await self.store.subscriptions_for_chat(chat_id)
            sources.extend(_subscription_source(subscription) for subscription in subscriptions)
            await self._post_new_items(chat_id, tuple(sources))

    async def _post_new_items(self, chat_id: int, sources: tuple[Source, ...]) -> None:
        sections: list[RenderSection] = []
        seen_updates: list[tuple[str, list[str]]] = []
        for source in sources:
            try:
                items = await source.fetch(self.settings.default_limit, self._http)
            except Exception:
                LOGGER.exception("Automated source failed: %s", source.id)
                continue

            seen = await self.store.seen_urls(chat_id, source.id)
            fresh = [item for item in items if item.identity not in seen]
            if not fresh:
                continue

            sections.append(RenderSection(title=source.name, items=tuple(fresh)))
            seen_updates.append((source.id, [item.identity for item in fresh]))

        if not sections:
            LOGGER.info("No automated updates for chat_id=%s", chat_id)
            return

        await self._telegram.send_message(
            chat_id,
            render_digest("New updates", sections),
            reply_markup=digest_markup(),
        )
        for source_id, urls in seen_updates:
            await self.store.mark_seen(chat_id, source_id, urls)

    async def _reminder_loop(self) -> None:
        while True:
            try:
                await self._send_due_reminders()
            except Exception:
                LOGGER.exception("Reminder loop failed")
            await asyncio.sleep(30)

    async def _send_due_reminders(self) -> None:
        now = datetime.now(self.settings.timezone)
        for reminder in await self.store.due_reminders(now):
            chat_id = int(reminder["chat_id"])
            if not self._is_allowed(chat_id):
                await self.store.complete_reminder(str(reminder.get("id", "")))
                continue

            await self._telegram.send_message(
                chat_id,
                render_reminder_due(str(reminder.get("text", "Reminder"))),
                reply_markup=back_to_menu_markup(),
            )
            await self.store.complete_reminder(str(reminder.get("id", "")))
            LOGGER.info("Sent reminder %s for %s at %s", reminder.get("id"), chat_id, format_datetime(now))

    def _is_allowed(self, chat_id: int) -> bool:
        return not self.settings.allowed_chat_ids or chat_id in self.settings.allowed_chat_ids

    @property
    def _http(self) -> httpx.AsyncClient:
        if self.http is None:
            raise RuntimeError("HTTP client is not ready")
        return self.http

    @property
    def _telegram(self) -> TelegramAPI:
        if self.telegram is None:
            raise RuntimeError("Telegram client is not ready")
        return self.telegram


def _parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.split()
    command = parts[0].split("@", 1)[0].removeprefix("/").casefold()
    return command, parts[1:]


def _parse_callback_data(value: str) -> tuple[str, list[str]] | None:
    action, separator, command = value.partition(":")
    if separator != ":" or action not in {"nav", "run"} or not command:
        return None
    if any(character.isspace() for character in command):
        return None
    return _parse_command(f"/{command}")


def _limit(args: list[str], settings: Settings) -> int:
    if not args:
        return settings.default_limit
    try:
        return max(1, min(int(args[0]), settings.max_limit))
    except ValueError:
        return settings.default_limit


def _subscription_source(subscription: dict[str, object]) -> Source:
    title = str(subscription.get("title") or "RSS feed")
    url = str(subscription.get("url") or "")
    source_id = str(subscription.get("id") or _title_from_url(url))
    return RssBundleSource(
        id=source_id,
        name=title,
        category="rss",
        description=url,
        feeds=(FeedSpec(id=source_id, name=title, url=url, tags=("rss", "custom")),),
        aliases=(),
    )


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _title_from_url(value: str) -> str:
    parsed = urlparse(value)
    return parsed.netloc or "RSS feed"


def _log_background_failure(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        LOGGER.exception("Background task crashed")
