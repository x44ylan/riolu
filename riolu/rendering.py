from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Iterable
from urllib.parse import urlparse

from riolu.host import HostSnapshot, TailscalePeer
from riolu.models import IntelItem
from riolu.sources.base import Source
from riolu.text import clamp_text, clean_text


MAX_TELEGRAM_TEXT = 3900
DIGEST_PAGE_SIZE = 10
DIGEST_LIMITS = (10, 20, 50, 100)
ReplyMarkup = dict[str, object]


@dataclass(frozen=True)
class RenderSection:
    title: str
    items: tuple[IntelItem, ...] = ()
    error: str = ""


def render_welcome() -> str:
    return (
        "ℹ️ <b>Riolu</b>\n"
        "<i>Personal signal desk.</i>"
    )


def render_help() -> str:
    return (
        "❔ <b>How to use Riolu</b>\n\n"
        "<b>Dojo</b>\n"
        "/dojo — host, services, disk and Tailscale\n\n"
        "<b>Find a signal</b>\n"
        "/latest — choose a news source\n"
        "/latest cyber 3 — three security stories\n"
        "/latest all 3 — combined default digest\n"
        "/sources — browse every source\n\n"
        "<b>Follow your own feeds</b>\n"
        "/subscribe &lt;feed_url&gt; [name]\n"
        "/feeds — view and manage feeds\n\n"
        "/get &lt;id&gt; [limit] — fetch a feed now\n"
        "/pause &lt;id&gt; · /resume &lt;id&gt;\n"
        "/filter &lt;id&gt; keyword1, keyword2\n\n"
        "<b>Dailies</b>\n"
        "/daily 09:00 Exercise — repeat until Done\n"
        "/dailies — active daily tasks\n\n"
        "<b>Cart</b>\n"
        "/cart add &lt;item&gt;\n"
        "/cart — shopping list\n\n"
        "/clear — delete recent Riolu chat messages"
    )


def render_loading(label: str) -> str:
    return (
        "🔎 <b>Scanning for updates…</b>\n"
        f"<blockquote>{escape(clamp_text(label, 160))}</blockquote>"
    )


def render_dojo_overview(snapshot: HostSnapshot) -> str:
    service_lines = "\n".join(
        f"{'🟢' if service.state == 'active' else '🔴'} {escape(service.name)} · {escape(service.state)}"
        for service in snapshot.services
    )
    return (
        f"🥋 <b>{escape(snapshot.hostname)}</b>\n"
        "<blockquote>"
        f"Uptime · {_duration(snapshot.uptime_seconds)}\n"
        f"Load · {snapshot.load_1m:.2f} / {snapshot.cpu_count} cores\n"
        f"Memory · {_usage(snapshot.memory_used, snapshot.memory_total)}\n"
        f"Disk · {_usage(snapshot.disk_used, snapshot.disk_total)}"
        "</blockquote>\n"
        f"<b>Services</b>\n{service_lines}"
    )


def render_dojo_tailscale(peers: tuple[TailscalePeer, ...]) -> str:
    online = sum(peer.online for peer in peers)
    lines = [f"🔗 <b>Tailscale</b> · {online}/{len(peers)} online"]
    for peer in peers:
        details = " · ".join(value for value in (peer.address, peer.os) if value)
        lines.append(
            f"{'🟢' if peer.online else '⚫'} <b>{escape(clamp_text(peer.name, 100))}</b>"
            + (f"\n<code>{escape(details)}</code>" if details else "")
        )
    return _fit_lines(lines)


def render_sources(sources: Iterable[Source]) -> str:
    lines = ["📰 <b>News</b>"]
    for source in sources:
        lines.append(
            "<blockquote>"
            f"<b>{escape(clamp_text(source.name, 120))}</b> "
            f"· {escape(clamp_text(source.category.replace('_', ' ').title(), 80))}\n"
            f"{escape(clamp_text(source.description, 240))}"
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
                footer = "<i>More stories ↓</i>"
                return _fit_lines([*lines, footer], footer=footer)
            lines.append(card)
    return _fit_lines(lines)


def render_subscription_added(subscription: dict[str, object]) -> str:
    return _fit_lines([(
        "✅ <b>Feed added</b>\n"
        "<blockquote>"
        f"<b>{escape(clamp_text(str(subscription.get('title', 'RSS feed')), 180))}</b>\n"
        f"{escape(clamp_text(str(subscription.get('url', '')), 1000))}"
        "</blockquote>"
    )])


def render_subscriptions(subscriptions: list[dict[str, object]]) -> str:
    if not subscriptions:
        return (
            "📡 <b>Your feeds</b>\n"
            "No custom feeds yet.\n\n"
            "Add one with:\n<code>/subscribe https://example.com/feed.xml Example Feed</code>"
        )
    lines = ["📡 <b>Feeds</b>"]
    for subscription in subscriptions:
        paused = bool(subscription.get("paused", False))
        keywords = [str(value) for value in subscription.get("keywords", []) if str(value).strip()]
        status = "⏸ Paused" if paused else "▶ Active"
        filter_line = f"\nFilter: {escape(', '.join(keywords))}" if keywords else ""
        lines.append(
            "<blockquote>"
            f"<b>{escape(clamp_text(str(subscription.get('title', 'RSS feed')), 180))}</b>\n"
            f"{escape(clamp_text(str(subscription.get('url', '')), 1000))}\n"
            f"{status}{filter_line}"
            "</blockquote>"
        )
    return _fit_lines(lines)


def render_feed_detail(subscription: dict[str, object]) -> str:
    paused = bool(subscription.get("paused", False))
    keywords = [str(value) for value in subscription.get("keywords", []) if str(value).strip()]
    status = "⏸ Paused" if paused else "▶ Active"
    filter_text = ", ".join(keywords) if keywords else "All items"
    return _fit_lines(
        [
            "📡 <b>Feed</b>",
            "<blockquote>"
            f"<b>{escape(clamp_text(str(subscription.get('title', 'RSS feed')), 180))}</b>\n"
            f"{escape(clamp_text(str(subscription.get('url', '')), 1000))}\n"
            f"{status}\n"
            f"Filter: {escape(clamp_text(filter_text, 300))}"
            "</blockquote>",
            f"Filter command: <code>/filter {escape(clamp_text(str(subscription.get('id', '')), 80))} keyword1, keyword2</code>",
        ]
    )


def render_feed_delete_confirmation(subscription: dict[str, object]) -> str:
    title = escape(clamp_text(str(subscription.get("title", "RSS feed")), 180))
    return f"🗑 <b>Remove feed?</b>\n<blockquote><b>{title}</b></blockquote>"


def render_reminder_added(reminder: dict[str, object], due_at: datetime) -> str:
    daily = reminder.get("repeat") == "daily"
    return _fit_lines([(
        f"✅ <b>{'Daily task set' if daily else 'Reminder set'}</b>\n"
        "<blockquote>"
        f"<b>{escape(clamp_text(str(reminder.get('text', 'Reminder')), 1000))}</b>\n"
        f"{'🔁 Daily · ' if daily else '⏰ '}{escape(due_at.strftime('%H:%M %Z') if daily else format_datetime(due_at))}"
        "</blockquote>"
    )])


def render_reminders(reminders: list[dict[str, object]]) -> str:
    if not reminders:
        return (
            "⏰ <b>Your reminders</b>\n"
            "Nothing queued.\n\n"
            "Try <code>/remind in 30m Take a break</code>."
        )
    lines = [f"⏰ <b>Reminders</b> · {len(reminders)}"]
    for reminder in reminders:
        due_at = _display_datetime(str(reminder.get("due_at", "")))
        daily = reminder.get("repeat") == "daily"
        lines.append(
            "<blockquote>"
            f"<b>{escape(clamp_text(str(reminder.get('text', 'Reminder')), 1000))}</b>\n"
            f"{'🔁 Daily · ' if daily else '⏰ '}{escape(clamp_text(_display_time(due_at) if daily else due_at, 80))}"
            "</blockquote>"
        )
    return _fit_lines(lines)


def render_dailies(values: list[dict[str, object]]) -> str:
    if not values:
        return "🔁 <b>Dailies</b>\nNothing active."
    lines = [f"🔁 <b>Dailies</b> · {len(values)}"]
    for value in values:
        due_at = _display_datetime(str(value.get("due_at", "")))
        lines.append(
            "<blockquote>"
            f"<b>{escape(clamp_text(str(value.get('text', 'Daily task')), 1000))}</b>\n"
            f"{escape(clamp_text(_display_time(due_at), 80))}"
            "</blockquote>"
        )
    return _fit_lines(lines)


def render_cart(values: list[dict[str, object]]) -> str:
    if not values:
        return "<b>Cart</b>\nNothing listed.\n\n<code>/cart add &lt;item&gt;</code>"
    lines = [f"<b>Cart</b> · {len(values)}"]
    for index, value in enumerate(values, start=1):
        lines.append(f"{index}. {escape(clamp_text(str(value.get('text', 'Cart item')), 500))}")
    return _fit_lines(lines)


def render_reminder_detail(reminder: dict[str, object]) -> str:
    due_at = _display_datetime(str(reminder.get("due_at", "")))
    daily = reminder.get("repeat") == "daily"
    return (
        "⏰ <b>Reminder details</b>\n"
        "<blockquote>"
        f"<b>{escape(clamp_text(str(reminder.get('text', 'Reminder')), 1000))}</b>\n"
        f"{'🔁 Daily · ' if daily else '⏰ '}{escape(clamp_text(_display_time(due_at) if daily else due_at, 80))}"
        "</blockquote>"
    )


def render_reminder_delete_confirmation(reminder: dict[str, object]) -> str:
    text = escape(clamp_text(str(reminder.get("text", "Reminder")), 300))
    return f"🗑 <b>Delete reminder?</b>\n<blockquote><b>{text}</b></blockquote>"


def render_reminder_due(text: str) -> str:
    return (
        "⏰ <b>Reminder</b>\n"
        "<blockquote>"
        f"<b>{escape(clamp_text(text, 1000))}</b>"
        "</blockquote>"
    )


def render_daily_task_due(text: str) -> str:
    return (
        "🔁 <b>Daily task</b>\n"
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
        (("News", "nav:sources"),),
        (("Feeds", "nav:feeds"),),
        (("Dailies", "nav:dailies"),),
        (("Cart", "nav:cart"),),
        (("Dojo", "nav:dojo"),),
    )


def sources_markup(sources: Iterable[Source]) -> ReplyMarkup:
    buttons = [(source.name, f"run:{source.id}") for source in sources]
    rows = [tuple(buttons[index : index + 2]) for index in range(0, len(buttons), 2)]
    rows.append((("‹ Menu", "nav:start"),))
    return _inline_keyboard(*rows)


def digest_markup(
    source_id: str = "",
    sections: Iterable[RenderSection] = (),
    *,
    limit: int = 0,
    max_limit: int = 0,
    page: int = 0,
    total: int = 0,
) -> ReplyMarkup:
    refresh = f"{source_id}:{limit}:{page}" if source_id and limit else source_id or "latest"
    rows: list[list[dict[str, str]]] = []
    link_index = 0
    for section in sections:
        for item in section.items:
            url = _safe_web_url(item.url)
            if not url or link_index >= 10:
                continue
            link_index += 1
            label = f"{link_index}. {clamp_text(item.title, 42)} ↗"
            rows.append([{"text": label, "url": url}])
    if source_id and max_limit:
        sizes = [value for value in DIGEST_LIMITS if value <= max_limit]
        rows.append(
            [
                {
                    "text": f"• {value}" if value == limit else str(value),
                    "callback_data": f"run:{source_id}:{value}:0",
                }
                for value in sizes
            ]
        )
    if source_id and total > DIGEST_PAGE_SIZE:
        last_page = (total - 1) // DIGEST_PAGE_SIZE
        navigation: list[dict[str, str]] = []
        if page > 0:
            navigation.append(
                {
                    "text": f"‹ {page}/{last_page + 1}",
                    "callback_data": f"run:{source_id}:{limit}:{page - 1}",
                }
            )
        if page < last_page:
            navigation.append(
                {
                    "text": f"{page + 2}/{last_page + 1} ›",
                    "callback_data": f"run:{source_id}:{limit}:{page + 1}",
                }
            )
        rows.append(navigation)
    rows.extend(
        [
            [
                {"text": "↻", "callback_data": f"run:{refresh}"},
                {"text": "📰", "callback_data": "nav:sources"},
            ],
            [{"text": "‹", "callback_data": "nav:start"}],
        ]
    )
    return {"inline_keyboard": rows}


def back_to_menu_markup() -> ReplyMarkup:
    return _inline_keyboard((("‹ Menu", "nav:start"),))


def format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M %Z")


def _item_card(index: int, item: IntelItem) -> str:
    summary = clamp_text(item.summary, 420)
    meta = _meta_line(item)
    lines = [
        "<blockquote>",
        f"<b>{index}. {escape(clamp_text(item.title, 180))}</b>",
    ]
    if summary:
        lines.append(escape(summary))
    if meta:
        lines.append(f"<i>{escape(meta)}</i>")
    lines.append("</blockquote>")
    return "\n".join(lines)


def _meta_line(item: IntelItem) -> str:
    parts = [clean_text(item.source_name)]
    if item.published_at is not None:
        parts.append(item.published_at.strftime("%Y-%m-%d"))
    if item.facts:
        parts.extend(clean_text(fact) for fact in item.facts[:3])
    elif item.tags:
        parts.append(", ".join(item.tags[:3]))
    return " · ".join(part for part in parts if part)


def _display_datetime(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return format_datetime(parsed)


def _display_time(value: str) -> str:
    parts = value.split(" ")
    return " ".join(parts[1:]) if len(parts) > 1 else value


def _duration(seconds: int) -> str:
    days, remainder = divmod(max(0, seconds), 86_400)
    hours, minutes = divmod(remainder // 60, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m"


def _usage(used: int, total: int) -> str:
    if total <= 0:
        return "unknown"
    percent = used / total * 100
    return f"{_bytes(used)} / {_bytes(total)} · {percent:.0f}%"


def _bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit in {"GB", "TB"} else f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


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
