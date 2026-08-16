from __future__ import annotations

import unittest
from datetime import UTC, datetime

from riolu.models import IntelItem
from riolu.rendering import (
    MAX_TELEGRAM_TEXT,
    RenderSection,
    digest_markup,
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

        self.assertIn("Personal signal desk", rendered)
        self.assertEqual(
            [row[0]["text"] for row in keyboard],
            ["News", "Feeds", "Dailies", "Cart", "Dojo"],
        )
        self.assertTrue(rendered.startswith("ℹ️ <b>Riolu</b>"))
        self.assertEqual(keyboard[2][0]["callback_data"], "nav:dailies")

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
        self.assertIn("More stories", rendered)

    def test_subscription_list_clamps_untrusted_fields(self) -> None:
        subscriptions = [
            {"id": "rss_" + "i" * 500, "title": "T" * 3000, "url": "https://example.com/" + "u" * 5000}
            for _ in range(10)
        ]

        rendered = render_subscriptions(subscriptions)

        self.assertLessEqual(len(rendered), MAX_TELEGRAM_TEXT)
        self.assertEqual(rendered.count("<blockquote>"), rendered.count("</blockquote>"))

    def test_subscription_list_shows_delivery_status_and_filters(self) -> None:
        rendered = render_subscriptions(
            [
                {
                    "id": "rss_test",
                    "title": "Python News",
                    "url": "https://example.com/feed",
                    "paused": True,
                    "keywords": ["python", "asyncio"],
                }
            ]
        )

        self.assertIn("⏸ Paused", rendered)
        self.assertIn("Filter: python, asyncio", rendered)

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

    def test_digest_prefers_source_specific_facts_in_quick_view(self) -> None:
        item = IntelItem(
            source_id="hello_github",
            source_name="HelloGitHub",
            title="owner/project",
            url="https://github.com/owner/project",
            tags=("github", "open-source"),
            facts=("Python", "★ 12.4k"),
        )

        rendered = render_digest("HelloGitHub", [RenderSection(title="HelloGitHub", items=(item,))])

        self.assertIn("HelloGitHub · Python · ★ 12.4k", rendered)
        self.assertNotIn("github, open-source", rendered)

    def test_digest_keyboard_uses_links_and_offers_more_items(self) -> None:
        item = IntelItem(
            source_id="cyber",
            source_name="Cyber News",
            title="Detailed security report",
            url="https://example.com/report",
        )

        markup = digest_markup(
            "cyber",
            [RenderSection(title="Cyber News", items=(item,))],
            limit=5,
            max_limit=10,
        )
        keyboard = markup["inline_keyboard"]

        self.assertEqual(keyboard[0][0]["url"], "https://example.com/report")
        callbacks = [button.get("callback_data") for row in keyboard for button in row]
        self.assertIn("run:cyber:10:0", callbacks)

    def test_digest_keyboard_offers_sizes_and_pagination(self) -> None:
        markup = digest_markup(
            "cyber",
            [],
            limit=50,
            max_limit=100,
            page=1,
            total=50,
        )
        keyboard = markup["inline_keyboard"]
        labels = [button["text"] for row in keyboard for button in row]
        callbacks = [button.get("callback_data") for row in keyboard for button in row]

        for value in ("10", "20", "• 50", "100"):
            self.assertIn(value, labels)
        self.assertIn("run:cyber:50:0", callbacks)
        self.assertIn("run:cyber:50:2", callbacks)


if __name__ == "__main__":
    unittest.main()
