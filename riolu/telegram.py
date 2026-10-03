from __future__ import annotations

import asyncio
import logging
import mimetypes
from typing import Any

import httpx


LOGGER = logging.getLogger(__name__)
MAX_REQUEST_ATTEMPTS = 3


class ChatMigratedError(RuntimeError):
    """Telegram upgraded a group; sends must target new_chat_id instead."""

    def __init__(self, new_chat_id: int, message: str = "chat was upgraded") -> None:
        super().__init__(message)
        self.new_chat_id = new_chat_id


class TelegramAPI:
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self.token = token
        self.client = client

    async def get_updates(self, *, offset: int, timeout: int) -> list[dict[str, Any]]:
        result = await self.request(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
        )
        return result if isinstance(result, list) else []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, object] | None = None,
        message_thread_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        result = await self.request("sendMessage", payload)
        return result if isinstance(result, dict) else {}

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self.request("editMessageText", payload)
        return result if isinstance(result, dict) else {}

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        *,
        caption: str = "",
        message_thread_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if caption:
            payload.update({"caption": caption, "parse_mode": "HTML"})
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        result = await self.request(
            "sendDocument",
            payload,
            files={"document": (filename, content, content_type)},
        )
        return result if isinstance(result, dict) else {}

    async def send_photo(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        *,
        caption: str = "",
        message_thread_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if caption:
            payload.update({"caption": caption, "parse_mode": "HTML"})
        content_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
        result = await self.request(
            "sendPhoto",
            payload,
            files={"photo": (filename, content, content_type)},
        )
        return result if isinstance(result, dict) else {}

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text
        await self.request("answerCallbackQuery", payload)

    async def set_my_commands(
        self,
        commands: tuple[dict[str, str], ...],
        *,
        scope: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {"commands": list(commands)}
        if scope is not None:
            payload["scope"] = scope
        await self.request("setMyCommands", payload)

    async def send_chat_action(
        self, chat_id: int, action: str = "typing", *, message_thread_id: int | None = None
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "action": action}
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        await self.request("sendChatAction", payload)

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str = "⚡") -> None:
        await self.request(
            "setMessageReaction",
            {"chat_id": chat_id, "message_id": message_id,
             "reaction": [{"type": "emoji", "emoji": emoji}]},
        )

    async def delete_messages(self, chat_id: int, message_ids: list[int]) -> None:
        for start in range(0, len(message_ids), 100):
            await self.request(
                "deleteMessages",
                {"chat_id": chat_id, "message_ids": message_ids[start : start + 100]},
            )

    async def request(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> Any:
        creates_message = method in {"sendMessage", "sendDocument", "sendPhoto", "createForumTopic"}
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            try:
                if files is None:
                    response = await self.client.post(self._url(method), json=payload)
                else:
                    response = await self.client.post(self._url(method), data=payload, files=files)
            except httpx.TransportError as exc:
                if creates_message and not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
                    raise RuntimeError(f"Telegram {method} delivery is unconfirmed; check the chat before resending.") from None
                if attempt + 1 >= MAX_REQUEST_ATTEMPTS:
                    raise RuntimeError(f"Telegram {method} could not connect") from None
                await asyncio.sleep(2**attempt)
                continue

            status = int(getattr(response, "status_code", 200))
            try:
                parsed = response.json()
            except ValueError:
                parsed = {}
            data = parsed if isinstance(parsed, dict) else {}
            retry_after = _retry_after(data)
            retryable = status == 429 or retry_after is not None or (status >= 500 and not creates_message)
            if retryable and attempt + 1 < MAX_REQUEST_ATTEMPTS:
                delay = min(60, retry_after if retry_after is not None else 2**attempt)
                LOGGER.warning("Telegram %s retrying in %ss after status %s", method, delay, status)
                await asyncio.sleep(delay)
                continue

            if not data.get("ok"):
                if (method in {"editMessageText", "editMessageReplyMarkup"}
                        and "message is not modified" in str(data.get("description", "")).casefold()):
                    return {"message_id": payload.get("message_id")}
                migrated = (data.get("parameters") or {}).get("migrate_to_chat_id")
                if migrated:
                    raise ChatMigratedError(
                        int(migrated),
                        str(data.get("description", "chat was upgraded")),
                    )
                raise RuntimeError(data.get("description", f"Telegram {method} failed (HTTP {status})"))
            return data.get("result")
        raise RuntimeError("Telegram request retry budget exhausted")

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"


def _retry_after(data: object) -> int | None:
    if not isinstance(data, dict):
        return None
    parameters = data.get("parameters")
    if not isinstance(parameters, dict):
        return None
    try:
        value = int(parameters.get("retry_after"))
    except (TypeError, ValueError):
        return None
    return max(0, value)
