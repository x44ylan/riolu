from __future__ import annotations

from typing import Any

import httpx


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
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
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

    async def set_my_commands(self, commands: tuple[dict[str, str], ...]) -> None:
        await self.request("setMyCommands", {"commands": list(commands)})

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self.request("sendChatAction", {"chat_id": chat_id, "action": action})

    async def request(self, method: str, payload: dict[str, Any]) -> Any:
        response = await self.client.post(self._url(method), json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram request failed"))
        return data.get("result")

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"
