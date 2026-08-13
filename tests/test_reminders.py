from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from riolu.reminders import parse_reminder_request


class ReminderParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.timezone = ZoneInfo("Asia/Singapore")
        self.now = datetime(2026, 7, 10, 12, 0, tzinfo=self.timezone)

    def test_relative_reminder(self) -> None:
        request = parse_reminder_request("in 30m Stretch", now=self.now, timezone=self.timezone)

        self.assertEqual(request.due_at, datetime(2026, 7, 10, 12, 30, tzinfo=self.timezone))
        self.assertEqual(request.text, "Stretch")

    def test_time_today_rolls_forward_to_tomorrow(self) -> None:
        request = parse_reminder_request("09:00 Daily review", now=self.now, timezone=self.timezone)

        self.assertEqual(request.due_at, datetime(2026, 7, 11, 9, 0, tzinfo=self.timezone))

    def test_rejects_past_absolute_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "future"):
            parse_reminder_request("2026-07-09 09:00 Too late", now=self.now, timezone=self.timezone)

    def test_rejects_invalid_calendar_date(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid"):
            parse_reminder_request("2026-02-30 09:00 Impossible", now=self.now, timezone=self.timezone)

    def test_rejects_zero_delay(self) -> None:
        with self.assertRaisesRegex(ValueError, "future"):
            parse_reminder_request("in 0m Immediately", now=self.now, timezone=self.timezone)


if __name__ == "__main__":
    unittest.main()
