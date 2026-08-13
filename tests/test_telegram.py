from __future__ import annotations

import unittest
from typing import Any

from riolu.telegram import TelegramAPI


class _FakeResponse:
    def __init__(self, result: object) -> None:
        self._result = result

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
