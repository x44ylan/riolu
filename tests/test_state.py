from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from riolu.state import StateStore


class StateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_nested_state_is_ignored_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "seen": {"1": ["wrong shape"], "2": {"cyber": ["good", 42]}},
                        "subscriptions": {"1": "wrong shape", "2": [{"id": "rss_ok"}, "bad"]},
                        "reminders": [
                            "bad",
                            {"id": "missing_due", "chat_id": 2},
                            {"id": "bad_chat", "chat_id": "nope", "due_at": "2026-07-10T08:00:00"},
                            {"id": "rem_ok", "chat_id": "2", "due_at": "2026-07-10T08:00:00"},
                        ],
                        "chats": [1, "2", None, {"bad": True}],
                    }
                ),
                encoding="utf-8",
            )
            store = StateStore(str(path))

            self.assertEqual(await store.seen_urls(1, "cyber"), set())
            self.assertEqual(await store.seen_urls(2, "cyber"), {"good"})
            self.assertEqual(await store.known_chat_ids(), {1, 2})
            self.assertEqual(await store.subscriptions_for_chat(1), [])
            self.assertEqual(await store.subscriptions_for_chat(2), [{"id": "rss_ok"}])
            self.assertEqual(
                await store.due_reminders(datetime(2026, 7, 10, 9, 0, tzinfo=UTC)),
                [{"id": "rem_ok", "chat_id": 2, "due_at": "2026-07-10T08:00:00", "text": "Reminder"}],
            )

    async def test_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            store = StateStore(str(path))

            await store.remember_chat(42)
            subscription = await store.add_subscription(42, "https://example.com/feed.xml", "Example")
            await store.mark_seen(42, "cyber", ["https://example.com/item"])

            self.assertTrue(path.exists())
            self.assertEqual(await store.known_chat_ids(), {42})
            self.assertEqual((await store.subscriptions_for_chat(42))[0]["id"], subscription["id"])
            self.assertEqual(await store.seen_urls(42, "cyber"), {"https://example.com/item"})


if __name__ == "__main__":
    unittest.main()
