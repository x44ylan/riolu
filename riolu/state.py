from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def harness_threads(self) -> dict[str, dict[str, Any]]:
        async with self._lock:
            return self._load()["harness_threads"]

    async def save_harness_thread(self, key: str, value: dict[str, Any]) -> None:
        async with self._lock:
            data = self._load()
            if value.get("deleted"):
                # Topic deletion is terminal; do not retain tombstone records.
                data["harness_threads"].pop(key, None)
            else:
                data["harness_threads"][key] = dict(value)
            self._save(data)

    async def forget_harness_thread(self, key: str) -> None:
        async with self._lock:
            data = self._load()
            if key not in data["harness_threads"]:
                return
            data["harness_threads"].pop(key)
            self._save(data)

    async def replace_harness_thread(self, old: str, new: str, value: dict[str, Any]) -> None:
        async with self._lock:
            data = self._load()
            data["harness_threads"].pop(old, None)
            data["harness_threads"][new] = dict(value)
            self._save(data)

    async def digest_last_date(self) -> str:
        async with self._lock:
            return str(self._load()["digest"].get("last_date", ""))

    async def save_digest_date(self, date: str) -> None:
        async with self._lock:
            data = self._load()
            data["digest"]["last_date"] = date
            self._save(data)

    async def chat_migrations(self) -> dict[str, int]:
        async with self._lock:
            return dict(self._load()["chat_migrations"])

    async def resolve_chat(self, chat_id: int) -> int:
        async with self._lock:
            return _resolve_chat(self._load(), chat_id)

    async def save_chat_migration(self, old: int, new: int) -> None:
        async with self._lock:
            data = self._load()
            if old == new:
                return
            data["chat_migrations"][str(old)] = int(new)
            _resolve_chat(data, old)  # Reject cycles before persisting anything.
            data["chats"] = list(
                dict.fromkeys(_resolve_chat(data, int(value)) for value in data.get("chats", []))
            )
            self._save(data)

    async def seen_urls(self, chat_id: int, source_id: str) -> set[str]:
        async with self._lock:
            data = self._load()
            source_seen = {url for key in _chat_keys(data, chat_id, "seen")
                           for url in data["seen"].get(key, {}).get(source_id, [])}
            global_seen = set(data["seen"].get("*", {}).get("*", []))
            return {item for item in source_seen | global_seen if isinstance(item, str)}

    async def mark_seen(self, chat_id: int, source_id: str, urls: list[str]) -> None:
        if not urls:
            return
        async with self._lock:
            data = self._load()
            chat_key = str(_resolve_chat(data, chat_id))
            seen = data["seen"].setdefault(chat_key, {}).setdefault(source_id, [])
            merged = list(dict.fromkeys([*seen, *urls]))
            data["seen"][chat_key][source_id] = merged[-1000:]
            self._save(data)

    async def remember_chat(self, chat_id: int) -> None:
        async with self._lock:
            data = self._load()
            chat_id = _resolve_chat(data, chat_id)
            chats = data["chats"]
            if chat_id in chats:
                return
            chats.append(chat_id)
            chats.sort()
            self._save(data)

    async def hook_seen(self, chat_id: int) -> set[str]:
        async with self._lock:
            data = self._load()
            return {item for key in _chat_keys(data, chat_id, "hooks")
                    for item in data["hooks"].get(key, []) if isinstance(item, str)}

    async def mark_hook_sent(self, chat_id: int, event_id: str) -> None:
        if not event_id:
            return
        async with self._lock:
            data = self._load()
            chat_id = _resolve_chat(data, chat_id)
            values = data["hooks"].setdefault(str(chat_id), [])
            if event_id not in values:
                values.append(event_id)
                data["hooks"][str(chat_id)] = values[-500:]
                self._save(data)

    async def known_chat_ids(self) -> set[int]:
        async with self._lock:
            data = self._load()
            chat_ids: set[int] = set()
            for chat_id in data["chats"]:
                try:
                    chat_ids.add(_resolve_chat(data, int(chat_id)))
                except (TypeError, ValueError):
                    continue
            return chat_ids

    async def create_agent_request(
        self,
        request_id: str,
        question: str,
        choices: list[str],
        source: str,
    ) -> bool:
        async with self._lock:
            data = self._load()
            requests = data["agent_requests"]
            if request_id in requests:
                return False
            requests[request_id] = {
                "question": question,
                "choices": choices,
                "source": source,
                "status": "pending",
                "answer": "",
                "prompts": [],
                "created_at": _now_iso(),
            }
            if len(requests) > 100:
                oldest = min(requests, key=lambda key: str(requests[key].get("created_at", "")))
                requests.pop(oldest, None)
            self._save(data)
            return True

    async def agent_request(self, request_id: str) -> dict[str, Any] | None:
        async with self._lock:
            value = self._load()["agent_requests"].get(request_id)
            return dict(value) if isinstance(value, dict) else None

    async def remember_agent_prompt(self, request_id: str, chat_id: int, message_id: int) -> None:
        if message_id <= 0:
            return
        async with self._lock:
            data = self._load()
            value = data["agent_requests"].get(request_id)
            if not isinstance(value, dict):
                return
            prompts = value.setdefault("prompts", [])
            prompt = {"chat_id": chat_id, "message_id": message_id}
            if prompt not in prompts:
                prompts.append(prompt)
                value["prompts"] = prompts[-10:]
                self._save(data)

    async def pending_agent_request_for_message(
        self,
        chat_id: int,
        message_id: int,
    ) -> tuple[str, dict[str, Any]] | None:
        async with self._lock:
            requests = self._load()["agent_requests"]
            for request_id, value in requests.items():
                if not isinstance(value, dict) or value.get("status") != "pending":
                    continue
                prompts = value.get("prompts", [])
                if {"chat_id": chat_id, "message_id": message_id} in prompts:
                    return str(request_id), dict(value)
            return None

    async def answer_agent_request(self, request_id: str, answer: str) -> dict[str, Any] | None:
        clean_answer = answer.strip()[:1_000]
        if not clean_answer:
            return None
        async with self._lock:
            data = self._load()
            value = data["agent_requests"].get(request_id)
            if not isinstance(value, dict):
                return None
            if value.get("status") == "pending":
                value["status"] = "answered"
                value["answer"] = clean_answer
                value["answered_at"] = _now_iso()
                self._save(data)
            return dict(value)

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

    async def add_note(self, chat_id: int, name: str, body: str) -> dict[str, Any]:
        clean_name = " ".join(name.split()).strip()[:100]
        clean_body = body.strip()[:10_000]
        if not clean_name or not clean_body:
            raise ValueError("A note needs both a name and content.")
        async with self._lock:
            data = self._load()
            chat_id = _resolve_chat(data, chat_id)
            values = data["notes"].setdefault(str(chat_id), [])
            if any(str(value.get("name", "")).casefold() == clean_name.casefold()
                   for value in _chat_notes(data, chat_id)):
                raise ValueError("That note name is already in use.")
            value = {
                "id": _stable_id("note", f"{chat_id}:{clean_name.casefold()}"),
                "name": clean_name,
                "body": clean_body,
                "created_at": _now_iso(),
            }
            values.append(value)
            values.sort(key=lambda item: str(item.get("name", "")).casefold())
            self._save(data)
            return dict(value)

    async def notes_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            data = self._load()
            return [dict(value) for value in _chat_notes(data, chat_id)]

    async def note_for_chat(self, chat_id: int, note_id: str) -> dict[str, Any] | None:
        async with self._lock:
            data = self._load()
            for value in _chat_notes(data, chat_id):
                if str(value.get("id", "")) == note_id:
                    return dict(value)
            return None

    async def remove_note(self, chat_id: int, note_id: str) -> dict[str, Any] | None:
        async with self._lock:
            data = self._load()
            removed = None
            for key in _chat_keys(data, chat_id, "notes"):
                values = data["notes"].get(key, [])
                for value in list(values):
                    if str(value.get("id", "")) == note_id:
                        removed = value
                        values.remove(value)
            if removed is not None:
                self._save(data)
                return dict(removed)
            return None

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_state()

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read state from {self.path}; existing state has been preserved.") from exc

        if not isinstance(raw, dict):
            raise RuntimeError(f"State at {self.path} must be a JSON object; existing state has been preserved.")
        return _normalize_state(raw)

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.path)


def _resolve_chat(data: dict[str, Any], chat_id: int) -> int:
    visited = set()
    while str(chat_id) in data["chat_migrations"]:
        if chat_id in visited:
            raise ValueError("Chat migration contains a cycle.")
        visited.add(chat_id)
        chat_id = int(data["chat_migrations"][str(chat_id)])
    return chat_id


def _chat_keys(data: dict[str, Any], chat_id: int, section: str) -> list[str]:
    """Read durable content through aliases; message IDs stay in their chat."""
    destination = _resolve_chat(data, chat_id)
    keys = []
    for key in data[section]:
        try:
            candidate = int(key)
        except (TypeError, ValueError):
            continue
        if _resolve_chat(data, candidate) == destination:
            keys.append(key)
    return keys


def _chat_notes(data: dict[str, Any], chat_id: int) -> list[dict[str, Any]]:
    values = {value["id"]: value for key in _chat_keys(data, chat_id, "notes")
              for value in data["notes"].get(key, [])}
    return sorted(values.values(), key=lambda value: value["name"].casefold())


def _empty_state() -> dict[str, Any]:
    return {
        "version": 9,
        "seen": {},
        # Preserve retired cart records when older state files are rewritten.
        "cart": {},
        "notes": {},
        "messages": {},
        "chats": [],
        "hooks": {},
        "agent_requests": {},
        "harness_threads": {},
        "digest": {},
        "chat_migrations": {},
    }


def _normalize_state(raw: dict[str, Any]) -> dict[str, Any]:
    state = _empty_state()
    threads = raw.get("harness_threads")
    if isinstance(threads, dict):
        state["harness_threads"] = {
            str(key): value for key, value in threads.items()
            if isinstance(value, dict) and value.get("harness") == "opencode"
            and isinstance(value.get("chat_id"), int) and isinstance(value.get("topic_id"), int)
            and not value.get("deleted")
        }
    digest = raw.get("digest")
    if isinstance(digest, dict):
        state["digest"] = digest
    migrations = raw.get("chat_migrations")
    if isinstance(migrations, dict):
        state["chat_migrations"] = {
            str(old): new
            for old, new in migrations.items()
            if isinstance(new, int)
        }
    seen = raw.get("seen")
    if isinstance(seen, dict):
        state["seen"] = _normalize_seen(seen)

    legacy_seen = raw.get("seen_urls")
    if isinstance(legacy_seen, list):
        state["seen"].setdefault("*", {})["*"] = [item for item in legacy_seen if isinstance(item, str)]

    cart = raw.get("cart")
    if isinstance(cart, dict):
        state["cart"] = {
            str(chat_id): [_normalize_cart_item(item) for item in items if isinstance(item, dict)]
            for chat_id, items in cart.items()
            if isinstance(items, list)
        }

    notes = raw.get("notes")
    if isinstance(notes, dict):
        state["notes"] = {
            str(chat_id): [_normalize_note(item) for item in items if isinstance(item, dict)]
            for chat_id, items in notes.items()
            if isinstance(items, list)
        }
        state["notes"] = {
            chat_id: [item for item in items if item]
            for chat_id, items in state["notes"].items()
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

    hooks = raw.get("hooks")
    if isinstance(hooks, dict):
        state["hooks"] = {
            str(chat_id): [item for item in values if isinstance(item, str)][-500:]
            for chat_id, values in hooks.items()
            if isinstance(values, list)
        }

    agent_requests = raw.get("agent_requests")
    if isinstance(agent_requests, dict):
        state["agent_requests"] = {
            str(request_id): normalized
            for request_id, value in agent_requests.items()
            if isinstance(value, dict)
            and (normalized := _normalize_agent_request(value))
        }

    return state


def _normalize_cart_item(item: dict[object, object]) -> dict[str, Any]:
    value = {str(key): raw for key, raw in item.items()}
    value["id"] = str(value.get("id", ""))[:80]
    value["text"] = " ".join(str(value.get("text", "Cart item")).split())[:500]
    return value


def _normalize_note(item: dict[object, object]) -> dict[str, Any]:
    value = {str(key): raw for key, raw in item.items()}
    note_id = str(value.get("id", "")).strip()[:80]
    name = " ".join(str(value.get("name", "")).split()).strip()[:100]
    body = str(value.get("body", "")).strip()[:10_000]
    if not note_id or not name or not body:
        return {}
    normalized = {"id": note_id, "name": name, "body": body}
    if value.get("created_at"):
        normalized["created_at"] = str(value["created_at"])
    return normalized


def _normalize_agent_request(item: dict[object, object]) -> dict[str, Any]:
    value = {str(key): raw for key, raw in item.items()}
    question = str(value.get("question", "")).strip()[:1_000]
    if not question:
        return {}
    choices = value.get("choices", [])
    prompts = value.get("prompts", [])
    return {
        "question": question,
        "choices": [str(choice).strip()[:40] for choice in choices if str(choice).strip()][:6]
        if isinstance(choices, list)
        else [],
        "source": str(value.get("source", "")).strip()[:100],
        "status": "answered" if value.get("status") == "answered" else "pending",
        "answer": str(value.get("answer", "")).strip()[:1_000],
        "prompts": [
            {"chat_id": prompt.get("chat_id"), "message_id": prompt.get("message_id")}
            for prompt in prompts
            if isinstance(prompt, dict)
            and isinstance(prompt.get("chat_id"), int)
            and isinstance(prompt.get("message_id"), int)
        ][-10:]
        if isinstance(prompts, list)
        else [],
        "created_at": str(value.get("created_at", "")),
        "answered_at": str(value.get("answered_at", "")),
    }


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


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()
