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

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        self.answers.append((callback_query_id, text, show_alert))

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
        self.assertIsNone(_parse_callback_data("cyber"))
        self.assertIsNone(_parse_callback_data("run:latest 5"))

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
            self.assertIn("personal signal desk", telegram.edits[0][2])
            self.assertIn("inline_keyboard", telegram.edits[0][3])


if __name__ == "__main__":
    unittest.main()
