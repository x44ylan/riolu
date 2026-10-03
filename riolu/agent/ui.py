"""Telegram-native controls for harness topics; no native protocol changes."""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time

from riolu.ui.style import card, code, commands, notice

LOGGER = logging.getLogger(__name__)


class HarnessUI:
    def __init__(self, bridge):
        self.bridge = bridge
        self.choices: dict[str, dict] = {}
        self.selections: dict[str, set[int]] = {}
        self.typing: dict[str, asyncio.Task] = {}
        self.closed = False

    def trusted(self, chat: dict) -> bool:
        settings = self.bridge.settings
        # Harness execution is more privileged than normal bot features. Keep
        # it on its own explicit allowlist instead of inheriting news targets
        # or the general bot allowlist.
        return chat.get("id") in settings.harness_chat_ids and chat.get("type", "private") in {
            "private", "group", "supergroup"
        }

    async def help(self, thread, section=""):
        sections = {
            "session": "/status — session details\n/new [prompt] · /resume [id-or-name] · /import id-or-name · /fork\n/rename name · /effort · /sessions\n/delete — delete topic and session",
            "tools": "/agent · /diff · /undo · /redo\n/export · /queue [clear] · /usage",
            "advanced": "/approve id · /deny id · /answer id text",
        }
        if section and section not in sections:
            raise ValueError("/commands session|tools|advanced")
        text = (card(section.title(), commands(sections[section])) if section else
                card(f"{thread['harness'].title()} · Quick guide",
                     commands("/model  /effort\n/new  /resume  /import  /status\n/steer — guide active run\n/interrupt — stop this run"),
                     hint="Type a prompt to continue. Your session stays saved."))
        rows = [[self.button(thread, label.title(), "commands", label)
                 for label in sections]]
        if section == "session":
            rows.append([self.button(thread, "Delete topic…", "delete")])
        await self.panel(thread, text, rows)

    async def notice(self, thread, text, *, title="Session"):
        if not thread.get("deleted"):
            rendered = notice(title, str(text))
            if thread.get("topic_id"):
                await self.bridge._send_html(thread, rendered, str(text)[:1800])
            else:
                await self.bridge.telegram().send_message(thread["chat_id"], rendered)

    async def output(self, thread, text, *, command="output"):
        if thread.get("deleted"):
            return
        text = str(text)
        rendered = (
            f"<blockquote>{code('/' + command.lower())}: "
            f"{html.escape(text)}</blockquote>"
        )
        if len(rendered) > 3900:
            await self.bridge.telegram().send_document(
                thread["chat_id"], "output.txt", text.encode(),
                message_thread_id=thread["topic_id"])
        else:
            await self.bridge._send_html(thread, rendered, text)

    def button(self, thread, label, command, argument="") -> dict:
        now = time.monotonic()
        self.choices = {k: v for k, v in self.choices.items() if v["expires"] > now}
        while len(self.choices) >= 512:
            self.choices.pop(next(iter(self.choices)))
        token = secrets.token_hex(6)
        self.choices[token] = {
            "key": self.bridge._key(thread), "command": command,
            "argument": argument, "expires": now + (86400 if command in {
                "approve", "deny", "answer", "toggle_answer", "submit_answer"
            } else 900),
        }
        return {"text": label[:60], "callback_data": "h:pick:" + token}

    def _multi_rows(self, thread, token, question):
        selected = self.selections.setdefault(token, set())
        rows = []
        for index, option in enumerate((question.get("options") or [])[:20]):
            label = option.get("label", "") if isinstance(option, dict) else str(option)
            rows.append([self.button(
                thread, ("☑ " if index in selected else "☐ ") + str(label),
                "toggle_answer", f"{token} {index}",
            )])
        rows.append([self.button(
            thread, f"confirm ({len(selected)} selected)", "submit_answer", token,
        )])
        return rows

    async def panel(self, thread, text, rows):
        kwargs = {"reply_markup": {"inline_keyboard": rows}}
        if thread.get("topic_id"):
            kwargs["message_thread_id"] = thread["topic_id"]
        sent = await self.bridge.telegram().send_message(thread["chat_id"], text, **kwargs)
        for row in rows:
            for button in row:
                token = button["callback_data"].removeprefix("h:pick:")
                if token in self.choices:
                    self.choices[token]["message_id"] = sent.get("message_id")
        return sent

    async def picker(self, thread, command, values, page=0):
        values = list(values)
        page = min(max(0, page), max(0, (len(values) - 1) // 8))
        current = thread.get("model" if command == "model" else command, "default")
        rows = [[self.button(thread, ("✓ " if value == current else "") + value,
                             command, value)] for value in values[page * 8:page * 8 + 8]]
        navigation = []
        if page:
            navigation.append(self.button(thread, "‹ Previous", "picker_page", f"{command} {page - 1}"))
        if (page + 1) * 8 < len(values):
            navigation.append(self.button(thread, "Next ›", "picker_page", f"{command} {page + 1}"))
        navigation.append({"text": "✕ Cancel", "callback_data": "h:cancel_panel"})
        rows.append(navigation)
        await self.panel(thread,
            card(command.title(), f"Current · {code(current)}",
                 hint=(f"Choose below · {page + 1}/{max(1, (len(values) + 7) // 8)}"
                       if values else "No options available.")), rows)

    async def session_picker(self, thread, sessions, page=0, *, root=False):
        sessions = list(sessions)
        page = min(max(0, page), max(0, (len(sessions) - 1) // 8))
        command = "resume_root" if root else "resume"
        rows = []
        for session in sessions[page * 8:page * 8 + 8]:
            title = str(session.get("title") or session["id"])
            label = f"{title} · {str(session['id'])[-8:]}"
            rows.append([self.button(thread, label, command, session["id"])])
        navigation = []
        if page:
            navigation.append(
                self.button(thread, "‹ Previous", "picker_page", f"{command} {page - 1}")
            )
        if (page + 1) * 8 < len(sessions):
            navigation.append(
                self.button(thread, "Next ›", "picker_page", f"{command} {page + 1}")
            )
        navigation.append({"text": "✕ Cancel", "callback_data": "h:cancel_panel"})
        rows.append(navigation)
        await self.panel(
            thread,
            card(
                "Resume session",
                f"{len(sessions)} unattached session{'s' if len(sessions) != 1 else ''}",
                hint=(
                    f"Choose below · {page + 1}/{max(1, (len(sessions) + 7) // 8)}"
                    if sessions
                    else "No sessions are available to resume."
                ),
            ),
            rows,
        )

    async def _clear_panel(self, query: dict, message: dict, chat: dict) -> None:
        key = f"{chat.get('id')}:{message.get('message_thread_id', 0)}"
        message_id = message.get("message_id")
        for token, choice in list(self.choices.items()):
            if choice["key"] == key and choice.get("message_id") == message_id:
                self.choices.pop(token, None)
        try:
            await self.bridge.telegram().request("editMessageReplyMarkup", {
                "chat_id": chat.get("id"),
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            })
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        if query.get("id"):
            await self.bridge.telegram().answer_callback_query(query["id"], text="Cancelled.")

    async def callback(self, query: dict) -> bool:
        data = str(query.get("data") or "")
        if not data.startswith("h:"):
            return False
        await self.bridge._load()
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        key = f"{chat.get('id')}:{message.get('message_thread_id', 0)}"
        thread = self.bridge.threads.get(key)
        error = ""
        action, _, argument = data[2:].partition(":")
        if not self.trusted(chat):
            error = "Chat not allowed."
        elif action == "cancel_panel":
            await self._clear_panel(query, message, chat)
            return True
        elif action == "pick":
            choice = self.choices.get(argument)
            if (not choice or choice["expires"] <= time.monotonic()
                    or choice["key"] != key or choice.get("message_id") != message.get("message_id")):
                error = "Expired. Reopen the menu."
            else:
                action, argument = choice["command"], choice["argument"]
                # Keep request buttons usable if the native reply fails.
                if action not in {"approve", "deny", "answer", "toggle_answer", "submit_answer"}:
                    for token, sibling in list(self.choices.items()):
                        if sibling["key"] == key and sibling.get("message_id") == message.get("message_id"):
                            self.choices.pop(token, None)
                if not thread and (
                    action == "resume_root"
                    or (action == "picker_page" and argument.startswith("resume_root "))
                ):
                    thread = {
                        "chat_id": int(chat["id"]),
                        "topic_id": int(message.get("message_thread_id") or 0),
                        "cwd": self.bridge.settings.harness_cwd,
                    }
        elif not thread or thread.get("deleted"):
            error = "Topic unavailable."
        else:
            # Legacy pinned-panel callbacks are no longer valid. Dynamic
            # h:pick buttons continue through the token branch above.
            error = "This control was removed. Use the command menu."
        if not error and (not thread or thread.get("deleted")):
            error = "Topic unavailable."
        if query.get("id"):
            await self.bridge.telegram().answer_callback_query(
                query["id"], text=error, show_alert=bool(error))
        if error:
            return True
        task = asyncio.create_task(self._run_control(thread, action, argument))
        self.bridge.actions.add(task)
        task.add_done_callback(self.bridge.actions.discard)
        return True

    async def _run_control(self, thread, action, argument):
        bridge = self.bridge
        lock = asyncio.Lock() if action in {"stop", "interrupt", "approve", "deny", "answer", "delete_confirm"} else bridge.locks.setdefault(bridge._key(thread), asyncio.Lock())
        try:
            async with lock:
                if thread.get("deleted"):
                    return
                if action == "delete":
                    await self.panel(thread,
                        card("Delete topic?", "Messages and the OpenCode session are removed permanently."),
                        [[self.button(thread, "Delete topic", "delete_confirm"),
                          self.button(thread, "Keep topic", "keep")]])
                elif action == "delete_confirm":
                    await bridge._command(thread, "delete", "")
                elif action == "keep":
                    await self.notice(thread, "Topic kept.", title="/delete")
                elif action == "picker_page":
                    command, page = argument.split()
                    if command in {"resume", "resume_root"}:
                        await bridge._resume_picker(
                            thread, page=int(page), root=command == "resume_root"
                        )
                    else:
                        await bridge._settings(thread, command, "", page=int(page))
                elif action == "resume_root":
                    await bridge._resume_from_picker(thread, argument)
                elif action == "toggle_answer":
                    await self.toggle_answer(thread, argument)
                elif action == "submit_answer":
                    await self.submit_answer(thread, argument)
                else:
                    await bridge._command(thread, action, argument)
        except Exception as exc:
            LOGGER.warning("Harness control failed: %s", type(exc).__name__)
            if action == "picker_page":
                label = argument.split(maxsplit=1)[0]
            elif action in {"toggle_answer", "submit_answer"}:
                label = "answer"
            elif action in {"delete_confirm", "keep"}:
                label = "delete"
            else:
                label = action.removesuffix("_root")
            await self.notice(thread, str(exc), title=f"/{label}")

    def sync_typing(self, thread):
        key = self.bridge._key(thread)
        task = self.typing.get(key)
        if self.closed or thread.get("status") != "working" or thread.get("deleted"):
            if task:
                task.cancel()
            return
        if not task or task.done():
            task = asyncio.create_task(self._typing(thread))
            self.typing[key] = task
            task.add_done_callback(lambda done: self.typing.pop(key, None)
                                   if self.typing.get(key) is done else None)

    async def _typing(self, thread):
        while thread.get("status") == "working" and not thread.get("deleted"):
            try:
                await self.bridge.telegram().send_chat_action(
                    thread["chat_id"], message_thread_id=thread["topic_id"])
            except Exception:
                LOGGER.debug("Harness typing indicator unavailable")
            await asyncio.sleep(4)

    async def close(self):
        self.closed = True
        tasks = list(self.typing.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def request(self, thread, token, request):
        method = request.get("method", "")
        params = request.get("params", request)
        approval = method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"} or request.get("kind") == "permission"
        questions = params.get("questions") or []
        rows = []
        if approval:
            action = params.get("permission") or params.get("action") or "Permission"
            resources = params.get("patterns") or params.get("resources") or []
            if isinstance(resources, str):
                resources = [resources]
            details = [code(str(action)[:150])]
            details.extend(code(str(resource)[:250]) for resource in resources[:8])
            if params.get("command"):
                details.append(code(str(params["command"])[:800]))
            if params.get("reason"):
                details.append(html.escape(str(params["reason"])[:350]))
            text = card("approval needed", "\n".join(details))
            decisions = params.get("availableDecisions")
            rows = [[self.button(thread, label, action, token)
                     for label, action, decision in [("Allow once", "approve", "accept"), ("Deny", "deny", "decline")]
                     if decisions is None or decision in decisions]]
            rows = [row for row in rows if row]
            if rows:
                text += f"\n{code('/approve ' + token)}  {code('/deny ' + token)}"
        elif questions:
            lines = []
            for question in questions:
                lines.append(str(question.get("question") or question.get("header")
                                 or question.get("id") or "Question")[:300])
                for index, option in enumerate(question.get("options") or [], 1):
                    label = option.get("label", "") if isinstance(option, dict) else str(option)
                    description = option.get("description", "") if isinstance(option, dict) else ""
                    lines.append(f"{index}. {str(label)[:160]}"
                                 + (f" — {str(description)[:160]}" if description else ""))
                lines.append("")
            visible = []
            length = 0
            for line in lines:
                escaped = html.escape(line)
                if length + len(escaped) + 1 > 3200:
                    visible.append("…")
                    break
                visible.append(escaped)
                length += len(escaped) + 1
            hint = ("tap options to select them, then confirm."
                    if len(questions) == 1 and questions[0].get("multiple")
                    else "tap an option or send an answer in this topic.")
            text = card("your choice", "\n".join(visible),
                        hint=hint + (" separate answers with |." if len(questions) > 1 else ""))
            if len(questions) == 1 and questions[0].get("multiple") and not questions[0].get("isSecret"):
                rows = self._multi_rows(thread, token, questions[0])
            elif len(questions) == 1 and not questions[0].get("isSecret"):
                for option in (questions[0].get("options") or [])[:8]:
                    label = option.get("label", "") if isinstance(option, dict) else str(option)
                    if label and "|" not in label:
                        rows.append([self.button(thread, label, "answer", token + " " + label)])
        else:
            text = card("input needed", code(method or "opencode request"),
                        hint=f"use /answer {token} to continue.")
        sent = await self.panel(thread, text, rows)
        request["_message_id"] = sent.get("message_id")
        request["_token"] = token
        thread["status"] = "waiting for input"
        await self.bridge.pin(thread)

    async def toggle_answer(self, thread, argument):
        token, _, index_text = argument.partition(" ")
        pending = self.bridge.requests.get(token)
        if not pending or self.bridge._key(pending[0]) != self.bridge._key(thread):
            raise ValueError("This question expired. Use the newest panel.")
        request = pending[1]
        questions = request.get("questions") or []
        if request.get("kind") != "question" or len(questions) != 1 or not questions[0].get("multiple"):
            raise ValueError("This is not a multi-select question.")
        options = questions[0].get("options") or []
        index = int(index_text)
        if index < 0 or index >= min(len(options), 20):
            raise ValueError("That option is unavailable.")
        previous = set(self.selections.get(token, set()))
        selected = self.selections.setdefault(token, set())
        selected.symmetric_difference_update({index})
        message_id = request.get("_message_id")
        old_tokens = {key for key, choice in self.choices.items()
                      if choice["key"] == self.bridge._key(thread)
                      and choice.get("message_id") == message_id}
        rows = self._multi_rows(thread, token, questions[0])
        new_tokens = {button["callback_data"].removeprefix("h:pick:")
                      for row in rows for button in row}
        for new_token in new_tokens:
            self.choices[new_token]["message_id"] = message_id
        try:
            await self.bridge.telegram().request("editMessageReplyMarkup", {
                "chat_id": thread["chat_id"], "message_id": message_id,
                "reply_markup": {"inline_keyboard": rows},
            })
        except Exception:
            self.selections[token] = previous
            for new_token in new_tokens:
                self.choices.pop(new_token, None)
            raise
        for old_token in old_tokens:
            self.choices.pop(old_token, None)

    async def submit_answer(self, thread, token):
        pending = self.bridge.requests.get(token)
        if not pending or self.bridge._key(pending[0]) != self.bridge._key(thread):
            raise ValueError("This question expired. Use the newest panel.")
        request = pending[1]
        questions = request.get("questions") or []
        if request.get("kind") != "question" or len(questions) != 1 or not questions[0].get("multiple"):
            raise ValueError("This is not a multi-select question.")
        options = questions[0].get("options") or []
        labels = [str(options[index].get("label", "") if isinstance(options[index], dict)
                      else options[index]) for index in sorted(self.selections.get(token, set()))]
        await self.bridge._reply(thread, "answer", token, selected=labels)

    async def answer_message(self, thread, message, text):
        reply_id = (message.get("reply_to_message") or {}).get("message_id")
        pending = [(token, request) for token, (owner, request) in self.bridge.requests.items()
                   if self.bridge._key(owner) == self.bridge._key(thread)]
        for token, request in pending:
            if reply_id and request.get("_message_id") == reply_id:
                if request.get("method") == "item/tool/requestUserInput" or request.get("kind") == "question":
                    await self.bridge._reply(thread, "answer", token + " " + text)
                else:
                    await self.notice(thread, f"Use the buttons or /approve {token} /deny {token}.", title="Approval needed")
                return True
        questions = [(token, request) for token, request in pending if request.get("kind") == "question"]
        if len(questions) == 1:
            await self.bridge._reply(thread, "answer", questions[0][0] + " " + text)
            return True
        return False

    async def resolved(self, thread, request):
        self.selections.pop(request.get("_token"), None)
        if request.get("_message_id"):
            try:
                await self.bridge.telegram().request("editMessageReplyMarkup", {
                    "chat_id": thread["chat_id"], "message_id": request["_message_id"],
                    "reply_markup": {"inline_keyboard": []},
                })
            except Exception:
                LOGGER.debug("Could not clear resolved request buttons")
        for token, choice in list(self.choices.items()):
            if choice["key"] == self.bridge._key(thread) and choice.get("message_id") == request.get("_message_id"):
                self.choices.pop(token, None)
        if thread.get("status") == "waiting for input" and not any(
                self.bridge._key(owner) == self.bridge._key(thread) for owner, _ in self.bridge.requests.values()):
            thread["status"] = "working" if thread.get("turn_id") or self.bridge._key(thread) in self.bridge.jobs else "ready"
        await self.bridge.pin(thread)
