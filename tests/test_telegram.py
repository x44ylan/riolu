from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from riolu.telegram import TelegramAPI


class _FakeResponse:
    def __init__(self, result: object, *, status_code: int = 200) -> None:
        self._result = result
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"ok": True, "result": self._result}


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> _FakeResponse:
        self.calls.append((url, json))
        if url.endswith("/sendMessage"):
            return _FakeResponse({"message_id": 42})
        if url.endswith("/editMessageText"):
            return _FakeResponse({"message_id": json["message_id"]})
        if url.endswith("/getUpdates"):
            return _FakeResponse([])
        return _FakeResponse(True)


class _RetryClient:
    def __init__(self) -> None:
        self.calls = 0

    async def post(self, url: str, *, json: dict[str, Any]) -> _FakeResponse:
        self.calls += 1
        if self.calls == 1:
            response = _FakeResponse(False, status_code=429)
            response.json = lambda: {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 2},
            }
            return response
        return _FakeResponse({"message_id": 7})


class TelegramAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_messages_support_inline_keyboards_and_editing(self) -> None:
        client = _FakeClient()
        api = TelegramAPI("token", client)  # type: ignore[arg-type]
        markup = {"inline_keyboard": [[{"text": "Menu", "callback_data": "nav:start"}]]}

        sent = await api.send_message(7, "Hello", reply_markup=markup)
        edited = await api.edit_message_text(7, 42, "Updated", reply_markup=markup)

        self.assertEqual(sent["message_id"], 42)
        self.assertEqual(edited["message_id"], 42)
        self.assertEqual(client.calls[0][1]["reply_markup"], markup)
        self.assertEqual(client.calls[1][1]["message_id"], 42)

    async def test_polling_requests_messages_and_button_callbacks(self) -> None:
        client = _FakeClient()
        api = TelegramAPI("token", client)  # type: ignore[arg-type]

        await api.get_updates(offset=12, timeout=45)

        self.assertEqual(client.calls[0][1]["allowed_updates"], ["message", "callback_query"])

    async def test_native_command_menu_is_sent_to_telegram(self) -> None:
        client = _FakeClient()
        api = TelegramAPI("token", client)  # type: ignore[arg-type]
        commands = ({"command": "start", "description": "Open menu"},)

        await api.set_my_commands(commands)

        self.assertTrue(client.calls[0][0].endswith("/setMyCommands"))
        self.assertEqual(client.calls[0][1]["commands"], list(commands))

    async def test_message_history_deletion_is_batched(self) -> None:
        client = _FakeClient()
        api = TelegramAPI("token", client)  # type: ignore[arg-type]

        await api.delete_messages(7, list(range(1, 151)))

        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call[0].endswith("/deleteMessages") for call in client.calls))
        self.assertEqual(len(client.calls[0][1]["message_ids"]), 100)
        self.assertEqual(len(client.calls[1][1]["message_ids"]), 50)

    async def test_rate_limit_is_retried_using_telegram_delay(self) -> None:
        client = _RetryClient()
        api = TelegramAPI("token", client)  # type: ignore[arg-type]

        with patch("riolu.telegram.asyncio.sleep", new=AsyncMock()) as sleep:
            sent = await api.send_message(7, "Hello")

        self.assertEqual(sent["message_id"], 7)
        self.assertEqual(client.calls, 2)
        sleep.assert_awaited_once_with(2)
