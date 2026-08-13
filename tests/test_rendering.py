from __future__ import annotations

import unittest
from datetime import UTC, datetime

from riolu.models import IntelItem
from riolu.rendering import (
    MAX_TELEGRAM_TEXT,
    RenderSection,
    main_menu_markup,
    render_digest,
    render_reminder_added,
    render_reminders,
    render_subscriptions,
    render_welcome,
    sources_markup,
)
from riolu.sources.registry import SourceRegistry


class RenderingTests(unittest.TestCase):
    def test_welcome_and_menu_offer_clear_quick_actions(self) -> None:
        rendered = render_welcome()
        keyboard = main_menu_markup()["inline_keyboard"]

        self.assertIn("personal signal desk", rendered)
        self.assertEqual(keyboard[0][0]["callback_data"], "run:latest")
        self.assertEqual(keyboard[0][1]["callback_data"], "nav:sources")

    def test_source_buttons_map_to_registered_source_ids(self) -> None:
        sources = SourceRegistry().all()
        keyboard = sources_markup(sources)["inline_keyboard"]
        callbacks = [button["callback_data"] for row in keyboard for button in row]

        for source in sources:
            self.assertIn(f"run:{source.id}", callbacks)

    def test_digest_stays_within_limit_and_preserves_html_blocks(self) -> None:
        items = tuple(
            IntelItem(
                source_id="test",
                source_name="Test source",
                title=f"Item {index} " + "T" * 300,
                url=f"https://example.com/{index}",
                summary="S" * 600,
                published_at=datetime(2026, 7, 10, tzinfo=UTC),
            )
            for index in range(30)
        )

        rendered = render_digest("riolu test", [RenderSection(title="Test", items=items)])

        self.assertLessEqual(len(rendered), MAX_TELEGRAM_TEXT)
        self.assertEqual(rendered.count("<blockquote>"), rendered.count("</blockquote>"))
        self.assertIn("More items were skipped", rendered)

    def test_subscription_list_clamps_untrusted_fields(self) -> None:
        subscriptions = [
            {"id": "rss_" + "i" * 500, "title": "T" * 3000, "url": "https://example.com/" + "u" * 5000}
            for _ in range(10)
        ]

        rendered = render_subscriptions(subscriptions)

        self.assertLessEqual(len(rendered), MAX_TELEGRAM_TEXT)
        self.assertEqual(rendered.count("<blockquote>"), rendered.count("</blockquote>"))

    def test_reminder_confirmation_clamps_long_text(self) -> None:
        due_at = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)
        rendered = render_reminder_added({"id": "rem_test", "text": "x" * 10000}, due_at)

        self.assertLessEqual(len(rendered), MAX_TELEGRAM_TEXT)
        self.assertEqual(rendered.count("<blockquote>"), rendered.count("</blockquote>"))

    def test_reminder_list_formats_iso_datetime_for_people(self) -> None:
        rendered = render_reminders(
            [{"id": "rem_test", "text": "Daily review", "due_at": "2026-07-11T09:00:00+08:00"}]
        )

        self.assertIn("2026-07-11 09:00 UTC+08:00", rendered)
        self.assertNotIn("T09:00:00", rendered)

    def test_digest_does_not_render_unsafe_links(self) -> None:
        item = IntelItem(
            source_id="test",
            source_name="Test",
            title="Unsafe link",
            url="javascript:alert(1)",
        )

        rendered = render_digest("Test", [RenderSection(title="Test", items=(item,))])

        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("<a href=", rendered)


if __name__ == "__main__":
    unittest.main()
