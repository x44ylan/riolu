from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def seen_urls(self, chat_id: int, source_id: str) -> set[str]:
        async with self._lock:
            data = self._load()
            chat_seen = data["seen"].get(str(chat_id), {})
            source_seen = set(chat_seen.get(source_id, []))
            global_seen = set(data["seen"].get("*", {}).get("*", []))
            return {item for item in source_seen | global_seen if isinstance(item, str)}

    async def mark_seen(self, chat_id: int, source_id: str, urls: list[str]) -> None:
        if not urls:
            return
        async with self._lock:
            data = self._load()
            chat_key = str(chat_id)
            seen = data["seen"].setdefault(chat_key, {}).setdefault(source_id, [])
            merged = sorted({*seen, *urls})
            data["seen"][chat_key][source_id] = merged[-1000:]
            self._save(data)

    async def remember_chat(self, chat_id: int) -> None:
        async with self._lock:
            data = self._load()
            chats = data["chats"]
            if chat_id in chats:
                return
            chats.append(chat_id)
            chats.sort()
            self._save(data)

    async def known_chat_ids(self) -> set[int]:
        async with self._lock:
            data = self._load()
            chat_ids: set[int] = set()
            for chat_id in data["chats"]:
                try:
                    chat_ids.add(int(chat_id))
                except (TypeError, ValueError):
                    continue
            return chat_ids

    async def add_subscription(self, chat_id: int, url: str, title: str) -> dict[str, Any]:
        async with self._lock:
            data = self._load()
            chat_key = str(chat_id)
            subscriptions = data["subscriptions"].setdefault(chat_key, [])
            existing = next((item for item in subscriptions if item.get("url") == url), None)
            if existing is not None:
                return existing

            subscription = {
                "id": _stable_id("rss", f"{chat_id}:{url}"),
                "title": title,
                "url": url,
                "created_at": _now_iso(),
            }
            subscriptions.append(subscription)
            self._save(data)
            return subscription

    async def remove_subscription(self, chat_id: int, token: str) -> dict[str, Any] | None:
        async with self._lock:
            data = self._load()
            chat_key = str(chat_id)
            subscriptions = data["subscriptions"].setdefault(chat_key, [])
            normalized = token.strip().casefold()
            for index, subscription in enumerate(subscriptions):
                ids = {
                    str(subscription.get("id", "")).casefold(),
                    str(subscription.get("url", "")).casefold(),
                }
                if normalized in ids:
                    removed = subscriptions.pop(index)
                    self._save(data)
                    return removed
            return None

    async def subscriptions_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            data = self._load()
            return list(data["subscriptions"].get(str(chat_id), []))

    async def subscription_chat_ids(self) -> set[int]:
        async with self._lock:
            data = self._load()
            chat_ids: set[int] = set()
            for key, subscriptions in data["subscriptions"].items():
                if not subscriptions:
                    continue
                try:
                    chat_ids.add(int(key))
                except ValueError:
                    continue
            return chat_ids

    async def add_reminder(self, chat_id: int, text: str, due_at: datetime) -> dict[str, Any]:
        async with self._lock:
            data = self._load()
            reminder = {
                "id": _stable_id("rem", f"{chat_id}:{due_at.isoformat()}:{text}"),
                "chat_id": chat_id,
                "text": text,
                "due_at": due_at.isoformat(),
                "created_at": _now_iso(),
            }
            data["reminders"].append(reminder)
            data["reminders"].sort(key=lambda item: item.get("due_at", ""))
            self._save(data)
            return reminder

    async def reminders_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            data = self._load()
            return [item for item in data["reminders"] if item.get("chat_id") == chat_id]

    async def remove_reminder(self, chat_id: int, reminder_id: str) -> dict[str, Any] | None:
        async with self._lock:
            data = self._load()
            for index, reminder in enumerate(data["reminders"]):
                if reminder.get("chat_id") == chat_id and str(reminder.get("id")) == reminder_id:
                    removed = data["reminders"].pop(index)
                    self._save(data)
                    return removed
            return None

    async def due_reminders(self, now: datetime) -> list[dict[str, Any]]:
        async with self._lock:
            data = self._load()
            due: list[dict[str, Any]] = []
            for reminder in data["reminders"]:
                due_at = _parse_datetime(str(reminder.get("due_at", "")))
                if due_at is None:
                    continue
                if due_at.tzinfo is None and now.tzinfo is not None:
                    due_at = due_at.replace(tzinfo=now.tzinfo)
                if due_at <= now:
                    due.append(dict(reminder))
            return due

    async def complete_reminder(self, reminder_id: str) -> None:
        async with self._lock:
            data = self._load()
            data["reminders"] = [item for item in data["reminders"] if item.get("id") != reminder_id]
            self._save(data)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_state()

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Could not read state from %s; starting with an empty state", self.path)
            return _empty_state()

        if not isinstance(raw, dict):
            return _empty_state()
        return _normalize_state(raw)

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.path)


def _empty_state() -> dict[str, Any]:
    return {"version": 2, "seen": {}, "subscriptions": {}, "reminders": [], "chats": []}


def _normalize_state(raw: dict[str, Any]) -> dict[str, Any]:
    state = _empty_state()
    seen = raw.get("seen")
    if isinstance(seen, dict):
        state["seen"] = _normalize_seen(seen)

    legacy_seen = raw.get("seen_urls")
    if isinstance(legacy_seen, list):
        state["seen"].setdefault("*", {})["*"] = [item for item in legacy_seen if isinstance(item, str)]

    subscriptions = raw.get("subscriptions")
    if isinstance(subscriptions, dict):
        state["subscriptions"] = {
            str(chat_id): [dict(item) for item in items if isinstance(item, dict)]
            for chat_id, items in subscriptions.items()
            if isinstance(items, list)
        }

    reminders = raw.get("reminders")
    if isinstance(reminders, list):
        state["reminders"] = _normalize_reminders(reminders)

    chats = raw.get("chats")
    if isinstance(chats, list):
        state["chats"] = [item for item in chats if isinstance(item, (int, str))]

    return state


def _normalize_seen(raw: dict[object, object]) -> dict[str, dict[str, list[str]]]:
    seen: dict[str, dict[str, list[str]]] = {}
    for chat_id, sources in raw.items():
        if not isinstance(sources, dict):
            continue
        normalized_sources: dict[str, list[str]] = {}
        for source_id, identities in sources.items():
            if not isinstance(identities, list):
                continue
            normalized_sources[str(source_id)] = [item for item in identities if isinstance(item, str)]
        seen[str(chat_id)] = normalized_sources
    return seen


def _normalize_reminders(raw: list[object]) -> list[dict[str, Any]]:
    reminders: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        reminder_id = str(item.get("id", "")).strip()
        due_at = str(item.get("due_at", "")).strip()
        try:
            chat_id = int(item.get("chat_id"))
        except (TypeError, ValueError):
            continue
        if not reminder_id or _parse_datetime(due_at) is None:
            continue
        reminder = dict(item)
        reminder.update(
            id=reminder_id,
            chat_id=chat_id,
            text=str(item.get("text") or "Reminder"),
            due_at=due_at,
        )
        reminders.append(reminder)
    return reminders


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()
