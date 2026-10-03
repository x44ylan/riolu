from __future__ import annotations

from dataclasses import dataclass

from riolu.ui.screens import (
    Screen,
    confirm_note_delete,
    error,
    note,
    note_body_prompt,
    note_name_prompt,
    notes,
)
from riolu.state import StateStore


@dataclass
class NoteDraft:
    body: str = ""


class NotesFeature:
    COMMANDS = frozenset(
        {"note", "notes", "cancel", "note_open", "note_confirm_delete", "note_delete"}
    )

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self._drafts: dict[int, NoteDraft] = {}

    def migrate_chat(self, old: int, new: int) -> None:
        draft = self._drafts.pop(old, None)
        if draft is not None:
            self._drafts.setdefault(new, draft)

    async def execute(self, chat_id: int, command: str, args: list[str]) -> Screen:
        if command == "note":
            self._drafts[chat_id] = NoteDraft()
            return note_body_prompt()
        if command == "cancel":
            if self._drafts.pop(chat_id, None) is None:
                return error("Nothing to cancel", "There is no note being added.")
            return notes(await self.store.notes_for_chat(chat_id))
        if command == "notes":
            return notes(await self.store.notes_for_chat(chat_id))
        if command == "note_open":
            value = await self._find(chat_id, args)
            return note(value) if value is not None else error("Notes", "Note not found.")
        if command == "note_confirm_delete":
            value = await self._find(chat_id, args)
            return confirm_note_delete(value) if value is not None else error("Notes", "Note not found.")
        if command == "note_delete":
            if not args or await self.store.remove_note(chat_id, args[0]) is None:
                return error("Notes", "Note not found.")
            return notes(await self.store.notes_for_chat(chat_id))
        return error("Unknown notes command", command)

    async def accept_text(self, chat_id: int, text: str) -> Screen | None:
        draft = self._drafts.get(chat_id)
        if draft is None:
            return None
        value = text.strip()
        if not value:
            return error("Add note", "The response cannot be empty. Send text or use /cancel.")
        if not draft.body:
            draft.body = value[:10_000]
            return note_name_prompt()
        try:
            stored = await self.store.add_note(chat_id, value, draft.body)
        except ValueError as exc:
            return error("Name this note", f"{exc} Send another name or use /cancel.")
        self._drafts.pop(chat_id, None)
        return note(stored)

    async def _find(self, chat_id: int, args: list[str]) -> dict[str, object] | None:
        if not args:
            return None
        return await self.store.note_for_chat(chat_id, args[0])
