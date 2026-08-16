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
            self.assertEqual(
                await store.subscriptions_for_chat(2),
                [{"id": "rss_ok", "keywords": [], "paused": False}],
            )
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

    async def test_subscription_delivery_controls_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.json"))
            subscription = await store.add_subscription(42, "https://example.com/feed", "Example")
            subscription_id = str(subscription["id"])

            filtered = await store.set_subscription_keywords(42, subscription_id, ["Python", " AI ", "python"])
            paused = await store.set_subscription_paused(42, subscription_id, True)

            self.assertEqual(filtered["keywords"], ["ai", "python"])  # type: ignore[index]
            self.assertTrue(paused["paused"])  # type: ignore[index]
            self.assertEqual(await store.subscription_chat_ids(), set())

            await store.set_subscription_paused(42, subscription_id, False)
            self.assertEqual(await store.subscription_chat_ids(), {42})

    async def test_daily_reminder_reschedules_until_completed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.json"))
            reminder = await store.add_reminder(
                42,
                "Exercise",
                datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
                repeat="daily",
            )

            due = await store.due_reminders(datetime(2026, 7, 10, 9, 1, tzinfo=UTC))
            await store.reschedule_daily_reminder(
                str(reminder["id"]),
                datetime(2026, 7, 10, 9, 1, tzinfo=UTC),
            )
            queued = await store.reminders_for_chat(42)

            self.assertEqual(due[0]["repeat"], "daily")
            self.assertEqual(queued[0]["due_at"], "2026-07-11T09:00:00+00:00")
            await store.complete_reminder(str(reminder["id"]))
            self.assertEqual(await store.reminders_for_chat(42), [])

    async def test_cart_add_deduplicate_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.json"))

            first = await store.add_cart_item(42, "Test item")
            duplicate = await store.add_cart_item(42, "  test   item ")

            self.assertEqual(first["id"], duplicate["id"])
            self.assertEqual(len(await store.cart_for_chat(42)), 1)
            self.assertIsNotNone(await store.remove_cart_item(42, str(first["id"])))
            self.assertEqual(await store.cart_for_chat(42), [])

    async def test_message_history_is_unique_bounded_and_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.json"))
            for message_id in range(1, 103):
                await store.remember_message(42, message_id)
            await store.remember_message(42, 102)

            values = await store.take_message_history(42)

            self.assertEqual(values, list(range(3, 103)))
            self.assertEqual(await store.take_message_history(42), [])


if __name__ == "__main__":
    unittest.main()
