from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from riolu.bot import RioluBot, _parse_callback_data
from riolu.config import Settings
from riolu.state import StateStore


class _FakeTelegram:
    def __init__(self) -> None:
        self.answers: list[tuple[str, str, bool]] = []
        self.edits: list[tuple[int, int, str, dict[str, object] | None]] = []
        self.deleted: list[tuple[int, list[int]]] = []

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        self.answers.append((callback_query_id, text, show_alert))

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        return None

    async def delete_messages(self, chat_id: int, message_ids: list[int]) -> None:
        self.deleted.append((chat_id, message_ids))

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        self.edits.append((chat_id, message_id, text, reply_markup))
        return {"message_id": message_id}


class BotUITests(unittest.IsolatedAsyncioTestCase):
    def test_callback_data_parser_accepts_only_known_action_shapes(self) -> None:
        self.assertEqual(_parse_callback_data("nav:sources"), ("sources", []))
        self.assertEqual(_parse_callback_data("run:cyber"), ("cyber", []))
        self.assertEqual(_parse_callback_data("run:cyber:10"), ("cyber", ["10"]))
        self.assertEqual(_parse_callback_data("run:cyber:100:3"), ("cyber", ["100", "3"]))
        self.assertEqual(_parse_callback_data("dojo:tailscale"), ("dojo_tailscale", []))
        self.assertEqual(_parse_callback_data("cart:delete:cart_123"), ("cart_delete", ["cart_123"]))
        self.assertEqual(_parse_callback_data("feed:open:rss_123"), ("feed_open", ["rss_123"]))
        self.assertEqual(
            _parse_callback_data("reminder:confirm_delete:rem_123"),
            ("reminder_confirm_delete", ["rem_123"]),
        )
        self.assertIsNone(_parse_callback_data("cyber"))
        self.assertIsNone(_parse_callback_data("run:latest 5"))
        self.assertIsNone(_parse_callback_data("feed:open"))

    async def test_menu_button_edits_the_existing_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                telegram_token="test",
                allowed_chat_ids=frozenset(),
                target_chat_id=None,
                default_limit=5,
                max_limit=10,
                post_interval_minutes=60,
                state_path=str(Path(directory) / "state.json"),
                http_timeout_seconds=20,
                timezone=ZoneInfo("Asia/Singapore"),
                default_source_ids=("cyber",),
            )
            bot = RioluBot(settings, store=StateStore(settings.state_path))
            telegram = _FakeTelegram()
            bot.telegram = telegram  # type: ignore[assignment]

            await bot._handle_update(
                {
                    "callback_query": {
                        "id": "callback-1",
                        "data": "nav:start",
                        "message": {"message_id": 99, "chat": {"id": 7}},
                    }
                }
            )

            self.assertEqual(telegram.answers, [("callback-1", "", False)])
            self.assertEqual(telegram.edits[0][0:2], (7, 99))
            self.assertIn("Personal signal desk", telegram.edits[0][2])
            self.assertIn("inline_keyboard", telegram.edits[0][3])

    async def test_latest_without_source_opens_news_picker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                telegram_token="test",
                allowed_chat_ids=frozenset(),
                target_chat_id=None,
                default_limit=5,
                max_limit=10,
                post_interval_minutes=60,
                state_path=str(Path(directory) / "state.json"),
                http_timeout_seconds=20,
                timezone=ZoneInfo("Asia/Singapore"),
                default_source_ids=("cyber",),
            )
            bot = RioluBot(settings, store=StateStore(settings.state_path))
            telegram = _FakeTelegram()
            bot.telegram = telegram  # type: ignore[assignment]

            await bot._dispatch(7, "latest", [], message_id=99)

            self.assertIn("<b>News</b>", telegram.edits[0][2])
            keyboard = telegram.edits[0][3]["inline_keyboard"]  # type: ignore[index]
            callbacks = [button["callback_data"] for row in keyboard for button in row]
            self.assertIn("run:hello_github", callbacks)
            self.assertIn("run:cyber", callbacks)

    async def test_clear_deletes_tracked_private_chat_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                telegram_token="test",
                allowed_chat_ids=frozenset(),
                target_chat_id=None,
                default_limit=5,
                max_limit=10,
                post_interval_minutes=60,
                state_path=str(Path(directory) / "state.json"),
                http_timeout_seconds=20,
                timezone=ZoneInfo("Asia/Singapore"),
                default_source_ids=("cyber",),
            )
            store = StateStore(settings.state_path)
            await store.remember_message(7, 40)
            bot = RioluBot(settings, store=store)
            telegram = _FakeTelegram()
            bot.telegram = telegram  # type: ignore[assignment]

            await bot._handle_update(
                {"message": {"message_id": 41, "chat": {"id": 7}, "text": "/clear"}}
            )

            self.assertEqual(telegram.deleted, [(7, [40, 41])])
            self.assertEqual(await store.take_message_history(7), [])


if __name__ == "__main__":
    unittest.main()
