from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Iterable
from urllib.parse import urlparse

from riolu.models import IntelItem
from riolu.sources.base import Source
from riolu.text import clamp_text, clean_text


MAX_TELEGRAM_TEXT = 3900
ReplyMarkup = dict[str, object]


@dataclass(frozen=True)
class RenderSection:
    title: str
    items: tuple[IntelItem, ...] = ()
    error: str = ""


def render_welcome() -> str:
    return (
        "🐾 <b>Riolu</b>\n"
        "<i>Your personal signal desk.</i>\n\n"
        "I track useful tech, security, open-source, and esports updates—"
        "and keep the reminders you do not want to drop.\n\n"
        "Choose a quick action below, or type /help to see everything I can do."
    )


def render_help() -> str:
    return (
        "❔ <b>How to use Riolu</b>\n\n"
        "<b>Find a signal</b>\n"
        "/latest — your default digest\n"
        "/latest cyber 3 — three security stories\n"
        "/sources — browse every source\n\n"
        "<b>Follow your own feeds</b>\n"
        "/subscribe &lt;feed_url&gt; [name]\n"
        "/subscriptions — view and manage feeds\n\n"
        "<b>Remember something</b>\n"
        "/remind in 30m Stand up\n"
        "/remind tomorrow 09:00 Daily review\n"
        "/reminders — see what is queued\n\n"
        "<i>Tip: most screens have buttons, but every action also works as a command.</i>"
    )


def render_loading(label: str) -> str:
    return (
        "🔎 <b>Scanning for updates…</b>\n"
        f"<blockquote>{escape(clamp_text(label, 160))}</blockquote>"
    )


def render_sources(sources: Iterable[Source]) -> str:
    lines = ["🗂 <b>Sources</b>\nTap a button below to fetch a source now."]
    for source in sources:
        command = source.aliases[0] if source.aliases else source.id
        lines.append(
            "<blockquote>"
            f"<b>{escape(clamp_text(source.name, 120))}</b> "
            f"· {escape(clamp_text(source.category.replace('_', ' ').title(), 80))}\n"
            f"{escape(clamp_text(source.description, 240))}\n"
            f"<code>/{escape(command)}</code>"
            "</blockquote>"
        )
    return _fit_lines(lines)


def render_digest(title: str, sections: Iterable[RenderSection]) -> str:
    lines = [f"✨ <b>{escape(title)}</b>"]
    for section in sections:
        if section.error:
            lines.append(
                "<blockquote>"
                f"<b>{escape(section.title)}</b>\n"
                "Could not load this source right now.\n"
                f"<code>{escape(clamp_text(section.error, 220))}</code>"
                "</blockquote>"
            )
            continue

        if not section.items:
            lines.append(
                "<blockquote>"
                f"<b>{escape(section.title)}</b>\n"
                "Nothing surfaced right now."
                "</blockquote>"
            )
            continue

        lines.append(f"\n<b>{escape(section.title)}</b>")
        for index, item in enumerate(section.items, start=1):
            card = _item_card(index, item)
            candidate = "\n".join([*lines, card])
            if len(candidate) > MAX_TELEGRAM_TEXT:
                footer = "<i>More items were skipped to keep this digest readable.</i>"
                return _fit_lines([*lines, footer], footer=footer)
            lines.append(card)
    return _fit_lines(lines)


def render_subscription_added(subscription: dict[str, object]) -> str:
    return _fit_lines([(
        "✅ <b>Feed added</b>\n"
        "<blockquote>"
        f"<b>{escape(clamp_text(str(subscription.get('title', 'RSS feed')), 180))}</b>\n"
        f"{escape(clamp_text(str(subscription.get('url', '')), 1000))}\n"
        f"ID <code>{escape(clamp_text(str(subscription.get('id', '')), 80))}</code>"
        "</blockquote>"
        "I’ll include new posts in your scheduled updates."
    )])


def render_subscriptions(subscriptions: list[dict[str, object]]) -> str:
    if not subscriptions:
        return (
            "📡 <b>Your feeds</b>\n"
            "No custom feeds yet.\n\n"
            "Add one with:\n<code>/subscribe https://example.com/feed.xml Example Feed</code>"
        )
    lines = ["📡 <b>Your feeds</b>\nNew posts are checked with your scheduled updates."]
    for subscription in subscriptions:
        lines.append(
            "<blockquote>"
            f"<b>{escape(clamp_text(str(subscription.get('title', 'RSS feed')), 180))}</b>\n"
            f"{escape(clamp_text(str(subscription.get('url', '')), 1000))}\n"
            f"ID <code>{escape(clamp_text(str(subscription.get('id', '')), 80))}</code>"
            "</blockquote>"
        )
    lines.append("Remove one with <code>/unsubscribe &lt;id&gt;</code>.")
    return _fit_lines(lines)


def render_reminder_added(reminder: dict[str, object], due_at: datetime) -> str:
    return _fit_lines([(
        "✅ <b>Reminder set</b>\n"
        "<blockquote>"
        f"<b>{escape(clamp_text(str(reminder.get('text', 'Reminder')), 1000))}</b>\n"
        f"⏰ {escape(format_datetime(due_at))}\n"
        f"ID <code>{escape(clamp_text(str(reminder.get('id', '')), 80))}</code>"
        "</blockquote>"
        "I’ll nudge you here when it is due."
    )])


def render_reminders(reminders: list[dict[str, object]]) -> str:
    if not reminders:
        return (
            "⏰ <b>Your reminders</b>\n"
            "Nothing queued.\n\n"
            "Try <code>/remind in 30m Take a break</code>."
        )
    lines = [f"⏰ <b>Your reminders</b>\n{len(reminders)} queued."]
    for reminder in reminders:
        due_at = _display_datetime(str(reminder.get("due_at", "")))
        lines.append(
            "<blockquote>"
            f"<b>{escape(clamp_text(str(reminder.get('text', 'Reminder')), 1000))}</b>\n"
            f"⏰ {escape(clamp_text(due_at, 80))}\n"
            f"ID <code>{escape(clamp_text(str(reminder.get('id', '')), 80))}</code>"
            "</blockquote>"
        )
    lines.append("Remove one with <code>/forget &lt;id&gt;</code>.")
    return _fit_lines(lines)


def render_reminder_due(text: str) -> str:
    return (
        "⏰ <b>Reminder</b>\n"
        "<blockquote>"
        f"<b>{escape(clamp_text(text, 1000))}</b>"
        "</blockquote>"
    )


def render_plain_error(title: str, message: str) -> str:
    return f"⚠️ <b>{escape(title)}</b>\n{escape(clamp_text(message, 900))}"


def render_notice(title: str, message: str) -> str:
    return f"✅ <b>{escape(title)}</b>\n{escape(clamp_text(message, 900))}"


def main_menu_markup() -> ReplyMarkup:
    return _inline_keyboard(
        (("✨ Latest", "run:latest"), ("🗂 Sources", "nav:sources")),
        (("📡 Feeds", "nav:subscriptions"), ("⏰ Reminders", "nav:reminders")),
        (("❔ Help", "nav:help"),),
    )


def sources_markup(sources: Iterable[Source]) -> ReplyMarkup:
    buttons = [(source.name, f"run:{source.id}") for source in sources]
    rows = [tuple(buttons[index : index + 2]) for index in range(0, len(buttons), 2)]
    rows.append((("‹ Menu", "nav:start"),))
    return _inline_keyboard(*rows)


def digest_markup(source_id: str = "") -> ReplyMarkup:
    refresh = source_id or "latest"
    return _inline_keyboard(
        (("↻ Refresh", f"run:{refresh}"), ("🗂 Sources", "nav:sources")),
        (("‹ Menu", "nav:start"),),
    )


def back_to_menu_markup() -> ReplyMarkup:
    return _inline_keyboard((("‹ Menu", "nav:start"),))


def format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M %Z")


def _item_card(index: int, item: IntelItem) -> str:
    summary = clamp_text(item.summary, 260)
    meta = _meta_line(item)
    lines = [
        "<blockquote>",
        f"<b>{index}. {escape(clamp_text(item.title, 180))}</b>",
    ]
    if summary:
        lines.append(escape(summary))
    if meta:
        lines.append(f"<i>{escape(meta)}</i>")
    url = _safe_web_url(item.url)
    if url:
        lines.append(f'<a href="{escape(url, quote=True)}">Read →</a>')
    lines.append("</blockquote>")
    return "\n".join(lines)


def _meta_line(item: IntelItem) -> str:
    parts = [clean_text(item.source_name)]
    if item.published_at is not None:
        parts.append(item.published_at.strftime("%Y-%m-%d"))
    if item.tags:
        parts.append(", ".join(item.tags[:3]))
    return " · ".join(part for part in parts if part)


def _display_datetime(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return format_datetime(parsed)


def _safe_web_url(value: str) -> str:
    if len(value) > 2000 or any(character in value for character in "\r\n\t"):
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return value


def _inline_keyboard(*rows: tuple[tuple[str, str], ...]) -> ReplyMarkup:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": callback_data} for label, callback_data in row]
            for row in rows
        ]
    }


def _fit_lines(lines: list[str], *, footer: str = "<i>Trimmed to fit Telegram.</i>") -> str:
    text = "\n".join(lines)
    if len(text) <= MAX_TELEGRAM_TEXT:
        return text
    trimmed: list[str] = []
    for line in lines:
        candidate = "\n".join([*trimmed, line, footer])
        if len(candidate) > MAX_TELEGRAM_TEXT:
            break
        trimmed.append(line)
    trimmed.append(footer)
    return "\n".join(trimmed)
