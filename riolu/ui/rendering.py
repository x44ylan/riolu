from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Iterable
from urllib.parse import urlparse

from riolu.features.host import HostSnapshot, TailscalePeer
from riolu.models import IntelItem
from riolu.ui.style import card, commands, notice
from riolu.source import Source
from riolu.ui.text import clamp_multiline_text, clamp_text, clean_text


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
    return "ℹ️ <b>Riolu</b>\n<i>Personal assistance.</i>"


def render_help(*, include_ctftime: bool = False) -> str:
    lines = ["/latest  /sources — news"]
    if include_ctftime:
        lines.append("/ctftime — ongoing and upcoming CTFs")
    lines.extend((
        "/note  /notes — notes",
        "/dojo — server",
        "/opencode — coding sessions",
        "/clear — clear recent chat messages",
    ))
    return card(
        "Riolu · Quick guide",
        commands("\n".join(lines)),
        hint="Choose from the menu or send a command.",
    )


def render_loading(label: str) -> str:
    return (
        "🔎 <b>Scanning for updates…</b>\n"
        f"<blockquote>{escape(clamp_text(label, 160))}</blockquote>"
    )


def render_dojo_overview(snapshot: HostSnapshot) -> str:
    return (
        f"💻 <b>{escape(snapshot.hostname)}</b>\n"
        "<blockquote>"
        f"Uptime · {_duration(snapshot.uptime_seconds)}\n"
        f"Load · {snapshot.load_1m:.2f} / {snapshot.cpu_count} cores\n"
        f"Memory · {_usage(snapshot.memory_used, snapshot.memory_total)}\n"
        f"Disk · {_usage(snapshot.disk_used, snapshot.disk_total)}"
        "</blockquote>"
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
            heading = f"<b>{escape(section.title)}</b>\n" if section.title else ""
            lines.append(
                "<blockquote>"
                f"{heading}"
                "Could not load this source right now.\n"
                f"<code>{escape(clamp_text(section.error, 220))}</code>"
                "</blockquote>"
            )
            continue

        if not section.items:
            heading = f"<b>{escape(section.title)}</b>\n" if section.title else ""
            lines.append(
                "<blockquote>"
                f"{heading}"
                "Nothing surfaced right now."
                "</blockquote>"
            )
            continue

        if section.title:
            lines.append(f"<b>{escape(section.title)}</b>")
        for index, item in enumerate(section.items, start=1):
            card = _item_card(index, item)
            candidate = "\n".join([*lines, card])
            if len(candidate) > MAX_TELEGRAM_TEXT:
                footer = "<i>More stories ↓</i>"
                return _fit_lines([*lines, footer], footer=footer)
            lines.append(card)
    return _fit_lines(lines)


def render_notes(values: list[dict[str, object]]) -> str:
    if not values:
        return card("Notes", "Your vault is empty.", hint="Tap Add note to begin.")
    return card(f"Notes · {len(values)}", "Choose a note below.")


def render_note(value: dict[str, object]) -> str:
    name = escape(clamp_text(str(value.get("name", "Note")), 100))
    body = escape(clamp_multiline_text(str(value.get("body", "")), 3500))
    return f"<b>{name}</b>\n<blockquote>{body}</blockquote>"


def render_note_body_prompt() -> str:
    return card("Add note · 1/2", "Send the text to save.", hint="Cancel anytime using the button below.")


def render_note_name_prompt() -> str:
    return card("Add note · 2/2", "Give this note a unique name.", hint="Cancel anytime using the button below.")


def render_note_delete_confirmation(value: dict[str, object]) -> str:
    name = escape(clamp_text(str(value.get("name", "Note")), 100))
    return card("Delete note?", name, hint="This cannot be undone.")


def render_plain_error(title: str, message: str) -> str:
    return notice(title, clamp_multiline_text(message, 900))


def render_notice(title: str, message: str) -> str:
    return notice(title, clamp_multiline_text(message, 900))


def render_event(payload: dict[str, object]) -> str:
    status = str(payload.get("status", ""))
    ctf = escape(clamp_text(str(payload.get("ctf", "")), 80))
    challenge = escape(clamp_text(str(payload.get("challenge", "")), 120))
    lines = ["🏴‍☠️ <b>OpenCook</b>", f"<b>{ctf} / {challenge}</b>"]
    if status == "solved":
        lines.append("Status: ✅ solved")
        flag = escape(clamp_text(str(payload.get("flag") or ""), 200))
        if flag:
            lines.append(f"Flag: <code>{flag}</code>")
    else:
        lines.append("Status: ⚠️ blocked")
        blocker = escape(clamp_multiline_text(str(payload.get("blocker") or ""), 400))
        if blocker:
            lines.append(f"Blocker: {blocker}")
    notes = escape(clamp_multiline_text(str(payload.get("summary") or ""), 600))
    if notes:
        lines.append(f"Notes: {notes}")
    return lines[0] + "\n<blockquote>" + "\n".join(lines[1:]) + "</blockquote>"


def event_markup(url: str) -> ReplyMarkup | None:
    safe = _safe_web_url(url)
    if not safe:
        return None
    return {"inline_keyboard": [[{"text": "OpenCook", "url": safe}]]}


def main_menu_markup(*, include_ctftime: bool = False) -> ReplyMarkup:
    rows = []
    if include_ctftime:
        rows.append((("CTFtime", "run:ctftime"),))
    rows.extend((
        (("News", "nav:sources"),),
        (("Notes", "nav:notes"),),
        (("Dojo", "nav:dojo"),),
    ))
    return _inline_keyboard(*rows)


def sources_markup(sources: Iterable[Source]) -> ReplyMarkup:
    buttons = [(source.name, f"run:{source.id}") for source in sources]
    rows = [tuple(buttons[index : index + 2]) for index in range(0, len(buttons), 2)]
    rows.append((("‹ Menu", "nav:start"),))
    return _inline_keyboard(*rows)


def source_options_markup(
    source_id: str,
    options: Iterable[tuple[str, ...]],
) -> ReplyMarkup:
    rows = [
        [{"text": option[1], "callback_data": f"run:{source_id}:{option[0]}"}]
        for option in options
        if len(option) >= 2
    ]
    rows.extend(
        [
            [{"text": "‹ News", "callback_data": "nav:sources"}],
            [{"text": "‹ Menu", "callback_data": "nav:start"}],
        ]
    )
    return {"inline_keyboard": rows}


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
    source_navigation = (
        {"text": "📰", "callback_data": f"run:{source_id.split(':', 1)[0]}"}
        if ":" in source_id
        else {"text": "📰", "callback_data": "nav:sources"}
    )
    rows.extend(
        [
            [
                {"text": "↻", "callback_data": f"run:{refresh}"},
                source_navigation,
            ],
            [{"text": "‹", "callback_data": "nav:start"}],
        ]
    )
    return {"inline_keyboard": rows}


def back_to_menu_markup() -> ReplyMarkup:
    return _inline_keyboard((("‹ Menu", "nav:start"),))


def _item_card(index: int, item: IntelItem) -> str:
    meta = _meta_line(item)
    for title_limit, summary_limit, meta_limit in ((180, 420, 0), (120, 180, 120)):
        summary = clamp_text(item.summary, summary_limit)
        visible_meta = clamp_text(meta, meta_limit) if meta_limit else meta
        lines = [f"<blockquote><b>{index}. {escape(clamp_text(item.title, title_limit))}</b>"]
        if summary:
            lines.append(escape(summary))
        if visible_meta:
            lines.append(f"<i>{escape(visible_meta)}</i>")
        lines[-1] += "</blockquote>"
        rendered = "\n".join(lines)
        if len(rendered) <= MAX_TELEGRAM_TEXT - 200:
            return rendered
    return rendered


def _meta_line(item: IntelItem) -> str:
    parts = [clean_text(item.source_name)]
    if item.published_at is not None:
        parts.append(item.published_at.strftime("%Y-%m-%d"))
    if item.facts:
        parts.extend(clean_text(fact) for fact in item.facts[:3])
    elif item.tags:
        parts.append(", ".join(item.tags[:3]))
    return " · ".join(part for part in parts if part)


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
