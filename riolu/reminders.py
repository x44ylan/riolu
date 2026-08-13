from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ReminderRequest:
    due_at: datetime
    text: str


def parse_reminder_request(value: str, *, now: datetime, timezone: ZoneInfo) -> ReminderRequest:
    text = value.strip()
    if not text:
        raise ValueError("Use /remind <when> <text>.")

    request = (
        _parse_relative(text, now)
        or _parse_absolute(text, timezone)
        or _parse_tomorrow(text, now, timezone)
        or _parse_time_today(text, now, timezone)
    )
    if request is not None:
        if request.due_at <= now.astimezone(timezone):
            raise ValueError("Reminder time must be in the future.")
        return request

    raise ValueError("I understand: in 30m, in 2h, 09:00, tomorrow 09:00, or 2026-06-25 21:30.")


def _parse_relative(value: str, now: datetime) -> ReminderRequest | None:
    match = re.match(r"^in\s+(\d+)\s*(m|min|mins|minute|minutes|h|hr|hour|hours|d|day|days)\s+(.+)$", value, re.I)
    if match is None:
        return None

    amount = int(match.group(1))
    unit = match.group(2).casefold()
    text = match.group(3).strip()
    if unit.startswith("m"):
        delta = timedelta(minutes=amount)
    elif unit.startswith("h"):
        delta = timedelta(hours=amount)
    else:
        delta = timedelta(days=amount)
    return ReminderRequest(due_at=now + delta, text=text)


def _parse_absolute(value: str, timezone: ZoneInfo) -> ReminderRequest | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s+(.+)$", value)
    if match is None:
        return None

    raw = f"{match.group(1)} {match.group(2)}"
    try:
        due_at = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone)
    except ValueError as exc:
        raise ValueError("Date and time must be valid and use YYYY-MM-DD HH:MM.") from exc
    return ReminderRequest(due_at=due_at, text=match.group(3).strip())


def _parse_tomorrow(value: str, now: datetime, timezone: ZoneInfo) -> ReminderRequest | None:
    match = re.match(r"^tomorrow\s+(\d{1,2}:\d{2})\s+(.+)$", value, re.I)
    if match is None:
        return None

    hour, minute = _split_time(match.group(1))
    base = now.astimezone(timezone) + timedelta(days=1)
    due_at = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return ReminderRequest(due_at=due_at, text=match.group(2).strip())


def _parse_time_today(value: str, now: datetime, timezone: ZoneInfo) -> ReminderRequest | None:
    match = re.match(r"^(\d{1,2}:\d{2})\s+(.+)$", value)
    if match is None:
        return None

    hour, minute = _split_time(match.group(1))
    local_now = now.astimezone(timezone)
    due_at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due_at <= local_now:
        due_at += timedelta(days=1)
    return ReminderRequest(due_at=due_at, text=match.group(2).strip())


def _split_time(value: str) -> tuple[int, int]:
    hour_text, minute_text = value.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if hour > 23 or minute > 59:
        raise ValueError("Time must be HH:MM in 24-hour format.")
    return hour, minute
