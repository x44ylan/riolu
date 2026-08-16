from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from riolu.host import HostSnapshot, TailscalePeer
from riolu.rendering import (
    RenderSection,
    ReplyMarkup,
    back_to_menu_markup,
    digest_markup,
    main_menu_markup,
    render_digest,
    render_daily_task_due,
    render_dailies,
    render_cart,
    render_dojo_overview,
    render_dojo_tailscale,
    render_help,
    render_feed_delete_confirmation,
    render_feed_detail,
    render_loading,
    render_notice,
    render_plain_error,
    render_reminder_added,
    render_reminder_due,
    render_reminder_delete_confirmation,
    render_reminder_detail,
    render_reminders,
    render_sources,
    render_subscription_added,
    render_subscriptions,
    render_welcome,
    sources_markup,
)
from riolu.sources.base import Source


@dataclass(frozen=True)
class Screen:
    text: str
    markup: ReplyMarkup | None = None


def welcome() -> Screen:
    return Screen(render_welcome(), main_menu_markup())


def help_screen() -> Screen:
    return Screen(render_help(), back_to_menu_markup())


def loading(label: str) -> Screen:
    return Screen(render_loading(label), {"inline_keyboard": []})


def sources(sources: Iterable[Source]) -> Screen:
    values = tuple(sources)
    return Screen(render_sources(values), sources_markup(values))


def dojo_overview(snapshot: HostSnapshot) -> Screen:
    return Screen(
        render_dojo_overview(snapshot),
        _keyboard(
            (("↻", "dojo:refresh"), ("🔗 Tailscale", "dojo:tailscale")),
            (("‹", "nav:start"),),
        ),
    )


def dojo_tailscale(peers: tuple[TailscalePeer, ...]) -> Screen:
    return Screen(
        render_dojo_tailscale(peers),
        _keyboard(
            (("↻", "dojo:tailscale"),),
            (("‹ Dojo", "nav:dojo"),),
        ),
    )


def digest(
    title: str,
    sections: Iterable[RenderSection],
    *,
    source_id: str = "",
    limit: int = 0,
    max_limit: int = 0,
    page: int = 0,
    total: int = 0,
) -> Screen:
    values = tuple(sections)
    return Screen(
        render_digest(title, values),
        digest_markup(
            source_id,
            values,
            limit=limit,
            max_limit=max_limit,
            page=page,
            total=total,
        ),
    )


def subscriptions(values: list[dict[str, object]]) -> Screen:
    rows = [
        ((f"⚙ {str(value.get('title', 'RSS feed'))[:36]}", f"feed:open:{value.get('id', '')}"),)
        for value in values
        if value.get("id")
    ]
    rows.append((("‹ Menu", "nav:start"),))
    return Screen(render_subscriptions(values), _keyboard(*rows))


def subscription_added(value: dict[str, object]) -> Screen:
    feed_id = str(value.get("id", ""))
    return Screen(
        render_subscription_added(value),
        _keyboard((("⚙ Manage feed", f"feed:open:{feed_id}"),), (("‹ Menu", "nav:start"),)),
    )


def feed_detail(value: dict[str, object]) -> Screen:
    feed_id = str(value.get("id", ""))
    paused = bool(value.get("paused", False))
    toggle = ("▶ Resume", f"feed:resume:{feed_id}") if paused else ("⏸ Pause", f"feed:pause:{feed_id}")
    return Screen(
        render_feed_detail(value),
        _keyboard(
            (("↻ Get now", f"feed:get:{feed_id}"), toggle),
            (("🗑 Remove", f"feed:confirm_delete:{feed_id}"),),
            (("‹ Feeds", "nav:feeds"),),
        ),
    )


def confirm_feed_delete(value: dict[str, object]) -> Screen:
    feed_id = str(value.get("id", ""))
    return Screen(
        render_feed_delete_confirmation(value),
        _keyboard(
            (("Delete", f"feed:delete:{feed_id}"), ("Cancel", f"feed:open:{feed_id}")),
        ),
    )


def reminder_added(value: dict[str, object], due_at: datetime) -> Screen:
    return Screen(render_reminder_added(value, due_at), back_to_menu_markup())


def reminders(values: list[dict[str, object]]) -> Screen:
    rows = [
        ((("🔁" if value.get("repeat") == "daily" else "⏰") + f" {str(value.get('text', 'Reminder'))[:34]}", f"reminder:open:{value.get('id', '')}"),)
        for value in values
        if value.get("id")
    ]
    rows.append((("‹ Menu", "nav:start"),))
    return Screen(render_reminders(values), _keyboard(*rows))


def dailies(values: list[dict[str, object]]) -> Screen:
    rows = [
        ((f"🔁 {str(value.get('text', 'Daily task'))[:34]}", f"reminder:open:{value.get('id', '')}"),)
        for value in values
        if value.get("id")
    ]
    rows.append((("‹", "nav:start"),))
    return Screen(render_dailies(values), _keyboard(*rows))


def cart(values: list[dict[str, object]]) -> Screen:
    rows = [
        ((f"Delete {str(value.get('text', 'item'))[:28]}", f"cart:delete:{value.get('id', '')}"),)
        for value in values
        if value.get("id")
    ]
    rows.append((("‹", "nav:start"),))
    return Screen(render_cart(values), _keyboard(*rows))


def reminder_detail(value: dict[str, object]) -> Screen:
    reminder_id = str(value.get("id", ""))
    actions = []
    if value.get("repeat") == "daily":
        actions.append((("✅ Done", f"reminder:done:{reminder_id}"),))
    actions.extend(
        [
            (("🗑 Delete", f"reminder:confirm_delete:{reminder_id}"),),
            (("‹ Dailies", "nav:dailies"),),
        ]
    )
    return Screen(
        render_reminder_detail(value),
        _keyboard(*actions),
    )


def confirm_reminder_delete(value: dict[str, object]) -> Screen:
    reminder_id = str(value.get("id", ""))
    return Screen(
        render_reminder_delete_confirmation(value),
        _keyboard(
            (("Delete", f"reminder:delete:{reminder_id}"), ("Cancel", f"reminder:open:{reminder_id}")),
        ),
    )


def reminder_due(text: str) -> Screen:
    return Screen(render_reminder_due(text), back_to_menu_markup())


def daily_task_due(value: dict[str, object]) -> Screen:
    reminder_id = str(value.get("id", ""))
    return Screen(
        render_daily_task_due(str(value.get("text", "Daily task"))),
        _keyboard(
            (("✅ Done", f"reminder:done:{reminder_id}"),),
            (("Later", "nav:start"),),
        ),
    )


def error(title: str, message: str, *, markup: ReplyMarkup | None = None) -> Screen:
    return Screen(render_plain_error(title, message), markup or back_to_menu_markup())


def notice(title: str, message: str) -> Screen:
    return Screen(render_notice(title, message), back_to_menu_markup())


def _keyboard(*rows: tuple[tuple[str, str], ...]) -> ReplyMarkup:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": callback_data} for label, callback_data in row]
            for row in rows
        ]
    }
