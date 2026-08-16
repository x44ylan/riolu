from __future__ import annotations

from datetime import datetime

from riolu.config import Settings
from riolu.reminders import parse_daily_request, parse_reminder_request
from riolu.screens import (
    Screen,
    confirm_reminder_delete,
    dailies,
    error,
    notice,
    reminder_added,
    reminder_detail,
    reminders,
)
from riolu.state import StateStore


class RemindersFeature:
    COMMANDS = frozenset(
        {
            "remind", "daily", "dailies", "reminders", "forget", "deletereminder",
            "reminder_open", "reminder_confirm_delete", "reminder_delete", "reminder_done",
        }
    )

    def __init__(self, settings: Settings, store: StateStore) -> None:
        self.settings = settings
        self.store = store

    async def execute(self, chat_id: int, command: str, args: list[str]) -> Screen:
        if command == "dailies":
            values = await self.store.reminders_for_chat(chat_id)
            return dailies([value for value in values if value.get("repeat") == "daily"])
        if command == "reminders":
            return reminders(await self.store.reminders_for_chat(chat_id))
        if command == "reminder_open":
            value = await self._find(chat_id, args)
            return reminder_detail(value) if value is not None else error("Reminder", "No matching reminder found.")
        if command == "reminder_confirm_delete":
            value = await self._find(chat_id, args)
            return confirm_reminder_delete(value) if value is not None else error("Reminder", "No matching reminder found.")
        if command == "remind":
            return await self._add(chat_id, args)
        if command == "daily":
            return await self._add_daily(chat_id, args)
        if command == "reminder_done":
            return await self._done(chat_id, args)
        if command in {"forget", "deletereminder", "reminder_delete"}:
            return await self._remove(chat_id, args)
        return error("Unknown reminder command", command)

    async def _add(self, chat_id: int, args: list[str]) -> Screen:
        try:
            request = parse_reminder_request(
                " ".join(args),
                now=datetime.now(self.settings.timezone),
                timezone=self.settings.timezone,
            )
        except ValueError as exc:
            return error("Could not set reminder", str(exc))
        value = await self.store.add_reminder(chat_id, request.text, request.due_at)
        return reminder_added(value, request.due_at)

    async def _add_daily(self, chat_id: int, args: list[str]) -> Screen:
        try:
            request = parse_daily_request(
                " ".join(args),
                now=datetime.now(self.settings.timezone),
                timezone=self.settings.timezone,
            )
        except ValueError as exc:
            return error("Could not set daily task", str(exc))
        value = await self.store.add_reminder(
            chat_id,
            request.text,
            request.due_at,
            repeat="daily",
        )
        return reminder_added(value, request.due_at)

    async def _done(self, chat_id: int, args: list[str]) -> Screen:
        if not args:
            return error("Daily task", "No matching task found.")
        removed = await self.store.remove_reminder(chat_id, args[0])
        if removed is None:
            return error("Daily task", "This task is already complete.")
        return notice("Task complete", str(removed.get("text", "Daily task")))

    async def _remove(self, chat_id: int, args: list[str]) -> Screen:
        if not args:
            return error("Forget reminder", "Use /forget <id>.")
        removed = await self.store.remove_reminder(chat_id, args[0])
        if removed is None:
            return error("Forget reminder", "No matching reminder found.")
        return notice("Reminder removed", str(removed.get("text", "Reminder")))

    async def _find(self, chat_id: int, args: list[str]) -> dict[str, object] | None:
        if not args:
            return None
        reminder_id = args[0]
        return next(
            (value for value in await self.store.reminders_for_chat(chat_id) if value.get("id") == reminder_id),
            None,
        )
