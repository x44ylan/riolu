from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta
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

    async def remember_message(self, chat_id: int, message_id: int) -> None:
        if message_id <= 0:
            return
        async with self._lock:
            data = self._load()
            values = data["messages"].setdefault(str(chat_id), [])
            if message_id not in values:
                values.append(message_id)
                data["messages"][str(chat_id)] = values[-100:]
                self._save(data)

    async def take_message_history(self, chat_id: int) -> list[int]:
        async with self._lock:
            data = self._load()
            values = data["messages"].pop(str(chat_id), [])
            if values:
                self._save(data)
            return [value for value in values if isinstance(value, int) and value > 0]

    async def cart_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            data = self._load()
            return list(data["cart"].get(str(chat_id), []))

    async def add_cart_item(self, chat_id: int, text: str) -> dict[str, Any]:
        clean = " ".join(text.split()).strip()[:500]
        if not clean:
            raise ValueError("Cart item cannot be empty.")
        async with self._lock:
            data = self._load()
            values = data["cart"].setdefault(str(chat_id), [])
            existing = next(
                (value for value in values if str(value.get("text", "")).casefold() == clean.casefold()),
                None,
            )
            if existing is not None:
                return dict(existing)
            value = {
                "id": _stable_id("cart", f"{chat_id}:{clean.casefold()}"),
                "text": clean,
                "created_at": _now_iso(),
            }
            values.append(value)
            self._save(data)
            return dict(value)

    async def remove_cart_item(self, chat_id: int, item_id: str) -> dict[str, Any] | None:
        async with self._lock:
            data = self._load()
            values = data["cart"].setdefault(str(chat_id), [])
            for index, value in enumerate(values):
                if str(value.get("id", "")) == item_id:
                    removed = values.pop(index)
                    self._save(data)
                    return removed
            return None

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
                "paused": False,
                "keywords": [],
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

    async def set_subscription_paused(
        self,
        chat_id: int,
        token: str,
        paused: bool,
    ) -> dict[str, Any] | None:
        return await self._update_subscription(chat_id, token, paused=paused)

    async def set_subscription_keywords(
        self,
        chat_id: int,
        token: str,
        keywords: list[str],
    ) -> dict[str, Any] | None:
        normalized = sorted({keyword.strip().casefold() for keyword in keywords if keyword.strip()})[:20]
        return await self._update_subscription(chat_id, token, keywords=normalized)

    async def _update_subscription(
        self,
        chat_id: int,
        token: str,
        **changes: object,
    ) -> dict[str, Any] | None:
        async with self._lock:
            data = self._load()
            subscriptions = data["subscriptions"].setdefault(str(chat_id), [])
            normalized = token.strip().casefold()
            for subscription in subscriptions:
                identities = {
                    str(subscription.get("id", "")).casefold(),
                    str(subscription.get("url", "")).casefold(),
                }
                if normalized not in identities:
                    continue
                subscription.update(changes)
                self._save(data)
                return dict(subscription)
            return None

    async def subscription_chat_ids(self) -> set[int]:
        async with self._lock:
            data = self._load()
            chat_ids: set[int] = set()
            for key, subscriptions in data["subscriptions"].items():
                if not any(not item.get("paused", False) for item in subscriptions):
                    continue
                try:
                    chat_ids.add(int(key))
                except ValueError:
                    continue
            return chat_ids

    async def add_reminder(
        self,
        chat_id: int,
        text: str,
        due_at: datetime,
        *,
        repeat: str = "",
    ) -> dict[str, Any]:
        async with self._lock:
            data = self._load()
            reminder = {
                "id": _stable_id("rem", f"{chat_id}:{due_at.isoformat()}:{text}"),
                "chat_id": chat_id,
                "text": text,
                "due_at": due_at.isoformat(),
                "created_at": _now_iso(),
            }
            if repeat == "daily":
                reminder["repeat"] = "daily"
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

    async def reschedule_daily_reminder(self, reminder_id: str, now: datetime) -> None:
        async with self._lock:
            data = self._load()
            for reminder in data["reminders"]:
                if reminder.get("id") != reminder_id or reminder.get("repeat") != "daily":
                    continue
                due_at = _parse_datetime(str(reminder.get("due_at", "")))
                if due_at is None:
                    return
                if due_at.tzinfo is None and now.tzinfo is not None:
                    due_at = due_at.replace(tzinfo=now.tzinfo)
                while due_at <= now:
                    due_at += timedelta(days=1)
                reminder["due_at"] = due_at.isoformat()
                reminder["last_sent_at"] = now.isoformat()
                data["reminders"].sort(key=lambda item: item.get("due_at", ""))
                self._save(data)
                return

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
    return {
        "version": 2,
        "seen": {},
        "subscriptions": {},
        "reminders": [],
        "cart": {},
        "messages": {},
        "chats": [],
    }


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
            str(chat_id): [_normalize_subscription(item) for item in items if isinstance(item, dict)]
            for chat_id, items in subscriptions.items()
            if isinstance(items, list)
        }

    reminders = raw.get("reminders")
    if isinstance(reminders, list):
        state["reminders"] = _normalize_reminders(reminders)

    cart = raw.get("cart")
    if isinstance(cart, dict):
        state["cart"] = {
            str(chat_id): [_normalize_cart_item(item) for item in items if isinstance(item, dict)]
            for chat_id, items in cart.items()
            if isinstance(items, list)
        }

    messages = raw.get("messages")
    if isinstance(messages, dict):
        state["messages"] = {
            str(chat_id): [value for value in values if isinstance(value, int) and value > 0][-100:]
            for chat_id, values in messages.items()
            if isinstance(values, list)
        }

    chats = raw.get("chats")
    if isinstance(chats, list):
        state["chats"] = [item for item in chats if isinstance(item, (int, str))]

    return state


def _normalize_subscription(item: dict[object, object]) -> dict[str, Any]:
    subscription = {str(key): value for key, value in item.items()}
    keywords = subscription.get("keywords", [])
    if not isinstance(keywords, list):
        keywords = []
    subscription["keywords"] = [
        str(keyword).strip().casefold()
        for keyword in keywords
        if isinstance(keyword, str) and keyword.strip()
    ][:20]
    subscription["paused"] = bool(subscription.get("paused", False))
    return subscription


def _normalize_cart_item(item: dict[object, object]) -> dict[str, Any]:
    value = {str(key): raw for key, raw in item.items()}
    value["id"] = str(value.get("id", ""))[:80]
    value["text"] = " ".join(str(value.get("text", "Cart item")).split())[:500]
    return value


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
        if item.get("repeat") == "daily":
            reminder["repeat"] = "daily"
        else:
            reminder.pop("repeat", None)
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
