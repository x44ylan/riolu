from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx

from riolu.config import Settings
from riolu.features import CartFeature, DojoFeature, NewsFeature, RemindersFeature, SubscriptionsFeature
from riolu.features.subscriptions import subscription_source
from riolu.host import HostInspector
from riolu.rendering import RenderSection, format_datetime
from riolu.screens import Screen, daily_task_due, digest, error, help_screen, reminder_due, welcome
from riolu.sources.base import Source, normalize_source_token
from riolu.sources.registry import SourceRegistry
from riolu.state import StateStore
from riolu.telegram import TelegramAPI


LOGGER = logging.getLogger(__name__)

BOT_COMMANDS: tuple[dict[str, str], ...] = (
    {"command": "start", "description": "Open the Riolu menu"},
    {"command": "latest", "description": "Choose a news source"},
    {"command": "feeds", "description": "Manage custom feeds"},
    {"command": "daily", "description": "Repeat a task until done"},
    {"command": "dailies", "description": "View active daily tasks"},
    {"command": "cart", "description": "Shopping list"},
    {"command": "buy", "description": "Add something to the cart"},
    {"command": "dojo", "description": "Dojo health and Tailscale"},
    {"command": "clear", "description": "Clear recent Riolu chat history"},
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
        self.news = NewsFeature(settings, self.registry, lambda: self._http)
        self.cart = CartFeature(self.store)
        self.dojo = DojoFeature(HostInspector())
        self.subscriptions = SubscriptionsFeature(settings, self.store, lambda: self._http)
        self.reminders = RemindersFeature(settings, self.store)

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

    async def _show(self, chat_id: int, screen: Screen, *, message_id: int | None = None) -> int | None:
        if message_id is not None:
            await self._telegram.edit_message_text(
                chat_id,
                message_id,
                screen.text,
                reply_markup=screen.markup,
            )
            return message_id

        sent = await self._telegram.send_message(chat_id, screen.text, reply_markup=screen.markup)
        try:
            sent_id = int(sent.get("message_id"))
        except (AttributeError, TypeError, ValueError):
            return None
        await self.store.remember_message(chat_id, sent_id)
        return sent_id

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
        text = (message.get("text") or "").strip()
        try:
            chat_id = int(chat.get("id"))
        except (TypeError, ValueError):
            return
        if not self._is_allowed(chat_id):
            LOGGER.warning("Rejected chat_id=%s", chat_id)
            await self._show(chat_id, error("Private bot", "This chat is not on the allowlist."))
            return

        try:
            message_id = int(message.get("message_id"))
        except (TypeError, ValueError):
            message_id = 0
        await self.store.remember_message(chat_id, message_id)
        if not text.startswith("/"):
            return

        await self.store.remember_chat(chat_id)
        command, args = _parse_command(text)
        try:
            await self._dispatch(chat_id, command, args)
        except Exception as exc:
            LOGGER.exception("Command failed: /%s", command)
            await self._show(chat_id, error("Something went wrong", str(exc)))

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
            await self._show(chat_id, error("Something went wrong", str(exc)), message_id=message_id)

    async def _dispatch(
        self,
        chat_id: int,
        command: str,
        args: list[str],
        *,
        message_id: int | None = None,
    ) -> None:
        normalized = normalize_source_token(command)
        if normalized == "start":
            screen = welcome()
        elif normalized == "clear":
            await self._clear_chat(chat_id)
            return
        elif normalized == "help":
            screen = help_screen()
        elif normalized in DojoFeature.COMMANDS:
            screen = await self.dojo.execute(normalized, args)
        elif normalized in {"latest", "sources"} or normalized in self.registry.command_aliases():
            await self._telegram.send_chat_action(chat_id)
            screen = await self.news.execute(normalized, args)
        elif normalized in SubscriptionsFeature.COMMANDS:
            await self._telegram.send_chat_action(chat_id)
            screen = await self.subscriptions.execute(chat_id, normalized, args)
        elif normalized in RemindersFeature.COMMANDS:
            screen = await self.reminders.execute(chat_id, normalized, args)
        elif normalized in CartFeature.COMMANDS:
            screen = await self.cart.execute(chat_id, normalized, args)
        else:
            screen = error(f"Unknown command: /{command}", "Open /help or use the menu below.", markup=welcome().markup)
        await self._show(chat_id, screen, message_id=message_id)

    async def _clear_chat(self, chat_id: int) -> None:
        message_ids = await self.store.take_message_history(chat_id)
        if not message_ids:
            return
        try:
            await self._telegram.delete_messages(chat_id, message_ids)
        except Exception:
            LOGGER.warning("Could not delete every tracked message for chat_id=%s", chat_id, exc_info=True)

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
            values = await self.store.subscriptions_for_chat(chat_id)
            sources.extend(subscription_source(value) for value in values if not value.get("paused", False))
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
            if fresh:
                sections.append(RenderSection(title=source.name, items=tuple(fresh)))
                seen_updates.append((source.id, [item.identity for item in fresh]))

        if not sections:
            LOGGER.info("No automated updates for chat_id=%s", chat_id)
            return

        await self._show(chat_id, digest("New updates", sections))
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
        for value in await self.store.due_reminders(now):
            chat_id = int(value["chat_id"])
            reminder_id = str(value.get("id", ""))
            if not self._is_allowed(chat_id):
                await self.store.complete_reminder(reminder_id)
                continue
            if value.get("repeat") == "daily":
                await self._show(chat_id, daily_task_due(value))
                await self.store.reschedule_daily_reminder(reminder_id, now)
            else:
                await self._show(chat_id, reminder_due(str(value.get("text", "Reminder"))))
                await self.store.complete_reminder(reminder_id)
            LOGGER.info("Sent reminder %s for %s at %s", reminder_id, chat_id, format_datetime(now))

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
    if separator != ":" or action not in {"nav", "run", "feed", "reminder", "cart", "dojo"} or not command:
        return None
    if action in {"feed", "reminder", "cart"}:
        operation, operation_separator, item_id = command.partition(":")
        if operation_separator != ":" or not operation or not item_id:
            return None
        if any(character.isspace() for character in operation + item_id):
            return None
        return f"{action}_{operation}", [item_id]
    if action == "dojo":
        if any(character.isspace() for character in command) or ":" in command:
            return None
        return f"dojo_{command}", []
    if any(character.isspace() for character in command):
        return None
    if action == "run":
        parts = command.split(":")
        if not all(parts):
            return None
        return parts[0], parts[1:]
    return _parse_command(f"/{command}")


def _log_background_failure(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        LOGGER.exception("Background task crashed")
