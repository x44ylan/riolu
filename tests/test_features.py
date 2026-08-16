from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from riolu.config import Settings
from riolu.features.news import NewsFeature
from riolu.features.cart import CartFeature
from riolu.features.dojo import DojoFeature
from riolu.features.reminders import RemindersFeature
from riolu.features.subscriptions import SubscriptionsFeature
from riolu.sources.registry import SourceRegistry
from riolu.host import HostSnapshot, ServiceStatus, TailscalePeer
from riolu.state import StateStore


def _settings(path: str) -> Settings:
    return Settings(
        telegram_token="test",
        allowed_chat_ids=frozenset(),
        target_chat_id=None,
        default_limit=5,
        max_limit=10,
        post_interval_minutes=60,
        state_path=path,
        http_timeout_seconds=20,
        timezone=ZoneInfo("Asia/Singapore"),
        default_source_ids=("cyber",),
    )


class FeatureInterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cart_add_and_button_delete_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.json"))
            feature = CartFeature(store)

            added = await feature.execute(7, "cart", ["add", "Test", "item"])
            value = (await store.cart_for_chat(7))[0]
            removed = await feature.execute(7, "cart_delete", [str(value["id"])])

            self.assertIn("Test item", added.text)
            self.assertIn(f"cart:delete:{value['id']}", str(added.markup))
            self.assertIn("Nothing listed", removed.text)

    async def test_dojo_returns_health_and_tailscale_screens(self) -> None:
        class FakeInspector:
            async def overview(self) -> HostSnapshot:
                return HostSnapshot(
                    hostname="dojo",
                    uptime_seconds=90_000,
                    load_1m=0.25,
                    cpu_count=4,
                    memory_used=4 * 1024**3,
                    memory_total=8 * 1024**3,
                    disk_used=20 * 1024**3,
                    disk_total=100 * 1024**3,
                    services=(ServiceStatus("riolu", "active"),),
                )

            async def tailscale(self) -> tuple[TailscalePeer, ...]:
                return (TailscalePeer("laptop", "100.64.0.2", True, "windows"),)

        feature = DojoFeature(FakeInspector())  # type: ignore[arg-type]

        overview = await feature.execute("dojo", [])
        tailscale = await feature.execute("dojo_tailscale", [])

        self.assertIn("<b>dojo</b>", overview.text)
        self.assertIn("riolu · active", overview.text)
        self.assertIn("laptop", tailscale.text)

    async def test_news_returns_complete_source_picker_screen(self) -> None:
        settings = _settings("unused.json")
        feature = NewsFeature(settings, SourceRegistry(), lambda: None)  # type: ignore[arg-type]

        screen = await feature.execute("latest", [])

        self.assertIn("<b>News</b>", screen.text)
        callbacks = [
            button["callback_data"]
            for row in screen.markup["inline_keyboard"]  # type: ignore[index]
            for button in row
        ]
        self.assertIn("run:hello_github", callbacks)

    async def test_subscription_and_reminder_failures_are_complete_screens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(str(Path(directory) / "state.json"))
            store = StateStore(settings.state_path)
            subscriptions = SubscriptionsFeature(settings, store, lambda: None)  # type: ignore[arg-type]
            reminders = RemindersFeature(settings, store)

            feed_screen = await subscriptions.execute(7, "pause", ["missing"])
            reminder_screen = await reminders.execute(7, "forget", ["missing"])

            self.assertIn("No matching feed", feed_screen.text)
            self.assertIn("No matching reminder", reminder_screen.text)
            self.assertIsNotNone(feed_screen.markup)
            self.assertIsNotNone(reminder_screen.markup)

    async def test_feed_management_flow_is_button_driven(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(str(Path(directory) / "state.json"))
            store = StateStore(settings.state_path)
            value = await store.add_subscription(7, "https://example.com/feed", "Example")
            feature = SubscriptionsFeature(settings, store, lambda: None)  # type: ignore[arg-type]

            detail = await feature.execute(7, "feed_open", [str(value["id"])])
            paused = await feature.execute(7, "feed_pause", [str(value["id"])])
            confirmation = await feature.execute(7, "feed_confirm_delete", [str(value["id"])])

            detail_callbacks = [
                button["callback_data"]
                for row in detail.markup["inline_keyboard"]  # type: ignore[index]
                for button in row
            ]
            self.assertIn(f"feed:pause:{value['id']}", detail_callbacks)
            self.assertIn("⏸ Paused", paused.text)
            self.assertIn("Remove feed?", confirmation.text)

    async def test_reminder_management_flow_has_delete_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(str(Path(directory) / "state.json"))
            store = StateStore(settings.state_path)
            request = await store.add_reminder(
                7,
                "Review Riolu",
                datetime(2026, 8, 16, 9, 0, tzinfo=settings.timezone),
            )
            feature = RemindersFeature(settings, store)

            detail = await feature.execute(7, "reminder_open", [str(request["id"])])
            confirmation = await feature.execute(7, "reminder_confirm_delete", [str(request["id"])])

            self.assertIn("Reminder details", detail.text)
            self.assertIn("Delete reminder?", confirmation.text)

    async def test_daily_task_stays_queued_until_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(str(Path(directory) / "state.json"))
            store = StateStore(settings.state_path)
            feature = RemindersFeature(settings, store)

            added = await feature.execute(7, "daily", ["09:00", "Exercise"])
            queued = await store.reminders_for_chat(7)
            reminder_id = str(queued[0]["id"])
            detail = await feature.execute(7, "reminder_open", [reminder_id])
            listing = await feature.execute(7, "dailies", [])
            done = await feature.execute(7, "reminder_done", [reminder_id])

            self.assertIn("Daily task set", added.text)
            self.assertIn("<b>Dailies</b>", listing.text)
            self.assertIn(f"reminder:done:{reminder_id}", str(detail.markup))
            self.assertIn("Task complete", done.text)
            self.assertEqual(await store.reminders_for_chat(7), [])


if __name__ == "__main__":
    unittest.main()
