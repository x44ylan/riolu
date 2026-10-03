from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from riolu.features.host import HostSnapshot, TailscalePeer
from riolu.ui.style import card
from riolu.ui.rendering import (
    RenderSection,
    ReplyMarkup,
    back_to_menu_markup,
    digest_markup,
    main_menu_markup,
    source_options_markup,
    render_digest,
    render_dojo_overview,
    render_dojo_tailscale,
    render_help,
    render_loading,
    render_notice,
    render_note,
    render_note_body_prompt,
    render_note_delete_confirmation,
    render_note_name_prompt,
    render_notes,
    render_plain_error,
    render_sources,
    render_welcome,
    sources_markup,
)
from riolu.source import Source


@dataclass(frozen=True)
class Screen:
    text: str
    markup: ReplyMarkup | None = None


def welcome(*, include_ctftime: bool = False) -> Screen:
    return Screen(
        render_welcome(),
        main_menu_markup(include_ctftime=include_ctftime),
    )


def help_screen(*, include_ctftime: bool = False) -> Screen:
    return Screen(
        render_help(include_ctftime=include_ctftime),
        back_to_menu_markup(),
    )


def loading(label: str) -> Screen:
    return Screen(render_loading(label), {"inline_keyboard": []})


def sources(sources: Iterable[Source]) -> Screen:
    values = tuple(sources)
    return Screen(render_sources(values), sources_markup(values))


def source_options(
    source_name: str,
    source_id: str,
    options: Iterable[tuple[str, ...]],
) -> Screen:
    values = tuple(options)
    return Screen(
        card(source_name, "Choose a view below."),
        source_options_markup(source_id, values),
    )


def dojo_overview(snapshot: HostSnapshot) -> Screen:
    rows: list[list[dict[str, str]]] = [
        [{"text": "↻", "callback_data": "dojo:refresh"}]
    ]
    tool_buttons = [
        {"text": tool.name.casefold(), "url": tool.url}
        for tool in sorted(snapshot.tools, key=lambda value: value.name.casefold())
        if tool.url
    ]
    rows.extend([button] for button in tool_buttons)
    rows.append([{"text": "‹", "callback_data": "nav:start"}])
    return Screen(
        render_dojo_overview(snapshot),
        {"inline_keyboard": rows},
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


def new_updates(sections: Iterable[RenderSection]) -> Screen:
    values = tuple(sections)
    if len(values) == 1:
        section = values[0]
        compact = RenderSection(title="", items=section.items, error=section.error)
        return digest(f"New Updates - {section.title}", (compact,))
    return digest("New Updates", values)


def notes(values: list[dict[str, object]]) -> Screen:
    rows = [
        ((str(value.get("name", "Note"))[:40], f"note:open:{value.get('id', '')}"),)
        for value in values
        if value.get("id")
    ]
    rows.extend([(("＋ Add note", "nav:note"),), (("‹ Menu", "nav:start"),)])
    return Screen(render_notes(values), _keyboard(*rows))


def note(value: dict[str, object]) -> Screen:
    note_id = str(value.get("id", ""))
    return Screen(
        render_note(value),
        _keyboard(
            (("🗑 Delete", f"note:confirm_delete:{note_id}"),),
            (("‹ Notes", "nav:notes"),),
        ),
    )


def confirm_note_delete(value: dict[str, object]) -> Screen:
    note_id = str(value.get("id", ""))
    return Screen(
        render_note_delete_confirmation(value),
        _keyboard(
            (("Delete", f"note:delete:{note_id}"), ("Cancel", f"note:open:{note_id}")),
        ),
    )


def note_body_prompt() -> Screen:
    return Screen(render_note_body_prompt(), _keyboard((("Cancel", "nav:cancel"),)))


def note_name_prompt() -> Screen:
    return Screen(render_note_name_prompt(), _keyboard((("Cancel", "nav:cancel"),)))


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
