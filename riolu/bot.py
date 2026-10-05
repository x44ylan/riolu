from __future__ import annotations

import asyncio
import base64
import binascii
import html
import logging
from pathlib import Path
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from riolu.config import Settings
from riolu.diagnostics import error_context
from riolu.features import DojoFeature, NewsFeature, NotesFeature
from riolu.features.host import HostInspector
from riolu.models import IntelItem
from riolu.ui.rendering import RenderSection, event_markup, render_event
from riolu.ui.screens import Screen, digest, error, help_screen, new_updates, welcome
from riolu.source import Source, normalize_source_token
from riolu.source_registry import SourceRegistry
from riolu.state import StateStore
from riolu.agent.commands import TELEGRAM_COMMANDS as OPENCODE_COMMANDS
from riolu.agent.harness import HarnessChat
from riolu.ui.style import card
from riolu.telegram import ChatMigratedError, TelegramAPI
from riolu.webhook import HookServer, HandlerResult, validate_event, validate_message


LOGGER = logging.getLogger(__name__)
MAX_ARTIFACT_BYTES = 10_000_000
AGENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,48}$")


class DigestIncompleteError(RuntimeError):
    def __init__(self, sources: tuple[str, ...], chats: tuple[int, ...] = ()) -> None:
        self.sources, self.chats = sources, chats
        super().__init__(f"Daily digest incomplete: sources={','.join(sources) or '-'} chats={len(chats)}")


BASE_BOT_COMMANDS: tuple[dict[str, str], ...] = (
    {"command": "start", "description": "Open the Riolu menu"},
    {"command": "latest", "description": "Choose a news source"},
    {"command": "note", "description": "Add a named note"},
    {"command": "notes", "description": "Open the notes vault"},
    {"command": "dojo", "description": "Open tool links"},
    {"command": "opencode", "description": "Start an OpenCode session topic"},
    {"command": "clear", "description": "Clear recent Riolu chat history"},
    {"command": "help", "description": "Show examples and help"},
)
CTFTIME_BOT_COMMAND = {
    "command": "ctftime",
    "description": "Browse ongoing and upcoming CTFs",
}


class RioluBot:
    def __init__(
        self,
        settings: Settings,
        *,
        registry: SourceRegistry | None = None,
        store: StateStore | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry or SourceRegistry.load(
            settings.source_directory, settings.source_ids
        )
        self.registry.default_sources(settings.default_source_ids)
        self.registry.automated_sources(settings.automated_source_ids)
        self.store = store or StateStore(settings.state_path)
        self.http: httpx.AsyncClient | None = None
        self.telegram: TelegramAPI | None = None
        self._integration_lock = asyncio.Lock()
        self.news = NewsFeature(settings, self.registry, lambda: self._http)
        self.dojo = DojoFeature(
            HostInspector(settings.dojo_services, settings.dojo_tools)
        )
        self.notes = NotesFeature(self.store)
        self.harness = HarnessChat(settings, self.store, lambda: self._telegram, lambda: self._http)

    async def run(self) -> None:
        timeout = httpx.Timeout(self.settings.http_timeout_seconds, read=50.0)
        headers = {"User-Agent": "riolu/0.2"}
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout) as client:
            self.http = client
            self.telegram = TelegramAPI(self.settings.telegram_token, client)
            await self._configure_telegram()

            tasks = set()
            server = None
            try:
                listener = None
                if self.settings.hook_token or self.settings.mcp_token:
                    server = HookServer(
                        self.settings.hook_host,
                        self.settings.hook_port,
                        self.settings.hook_token,
                        self._handle_hook,
                        message_token=self.settings.mcp_token,
                        message_handler=self._handle_agent_message,
                        agent_handler=self._handle_agent_action,
                    )
                    await server.start()
                    listener = asyncio.create_task(server.run())
                    tasks.add(listener)
                    LOGGER.info("Local integration endpoint ready on %s:%s", self.settings.hook_host, server.port)
                tasks.update({
                    asyncio.create_task(self._digest_loop()),
                    asyncio.create_task(self.harness.refresh_panels()),
                    asyncio.create_task(self.harness.sync_names()),
                    asyncio.create_task(self.harness.watch_requests()),
                })
                for task in tasks:
                    if task is not listener:
                        task.add_done_callback(_log_background_failure)
                polling = asyncio.create_task(self._poll_forever())
                tasks.add(polling)
                if listener is not None:
                    done, _ = await asyncio.wait({polling, listener}, return_when=asyncio.FIRST_COMPLETED)
                    if listener in done:
                        await listener
                        raise RuntimeError("Required local integration listener stopped unexpectedly")
                await polling
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if server is not None:
                    await server.close()
                await self.harness.close()
                self.telegram = None
                self.http = None

    async def _configure_telegram(self) -> None:
        try:
            ctftime_commands = (
                (CTFTIME_BOT_COMMAND,)
                if self.registry.get("ctftime") is not None
                else ()
            )
            source_commands = tuple(
                {
                    "command": normalize_source_token(source.id),
                    "description": f"Browse {source.name}"[:256],
                }
                for source in self.registry.all()
                if normalize_source_token(source.id) != "ctftime"
                and re.fullmatch(r"[a-z0-9_]{1,32}", normalize_source_token(source.id))
            )
            commands = (
                *BASE_BOT_COMMANDS[:2],
                *ctftime_commands,
                *BASE_BOT_COMMANDS[2:],
                *source_commands,
            )
            await self._telegram.set_my_commands(commands)
            for chat_id in sorted(self.settings.harness_chat_ids):
                await self._telegram.set_my_commands(
                    OPENCODE_COMMANDS,
                    scope={"type": "chat", "chat_id": chat_id},
                )
        except Exception:
            LOGGER.warning("Could not update Telegram command menu", exc_info=True)

    async def _show(
        self,
        chat_id: int,
        screen: Screen,
        *,
        message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> int | None:
        if message_id is not None:
            await self._telegram.edit_message_text(
                chat_id,
                message_id,
                screen.text,
                reply_markup=screen.markup,
            )
            return message_id

        send_kwargs: dict[str, object] = {"reply_markup": screen.markup}
        if message_thread_id is not None:
            send_kwargs["message_thread_id"] = message_thread_id
        chat_id, sent = await self._send_to_chat(
            chat_id, self._telegram.send_message, screen.text, **send_kwargs,
        )
        try:
            sent_id = int(sent.get("message_id"))
        except (AttributeError, TypeError, ValueError):
            return None
        await self.store.remember_message(chat_id, sent_id)
        return sent_id

    async def _record_chat_migration(self, old: int, new: int) -> None:
        await self.store.save_chat_migration(old, new)
        self.notes.migrate_chat(old, await self.store.resolve_chat(new))

    async def _send_to_chat(self, chat_id: int, send, *args, **kwargs):
        chat_id = await self.store.resolve_chat(chat_id)
        attempted = set()
        while chat_id not in attempted:
            attempted.add(chat_id)
            try:
                return chat_id, await send(chat_id, *args, **kwargs)
            except ChatMigratedError as exc:
                # Telegram definitively rejected this send; it is safe to retry
                # at the authenticated destination. Never retarget message IDs.
                await self._record_chat_migration(chat_id, exc.new_chat_id)
                chat_id = await self.store.resolve_chat(exc.new_chat_id)
        raise ValueError("Chat migration did not reach a new destination.")

    async def _poll_forever(self) -> None:
        offset = 0
        LOGGER.info("Riolu is polling Telegram")
        while True:
            try:
                updates = await self._telegram.get_updates(offset=offset, timeout=45)
            except Exception:
                LOGGER.exception("Telegram polling failed")
                await asyncio.sleep(5)
                continue

            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                try:
                    await self._handle_update(update)
                except Exception:
                    LOGGER.exception("Failed to handle Telegram update")

    async def _handle_update(self, update: dict[str, Any]) -> None:
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self._handle_callback_query(callback_query)
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = (message.get("text") or "").strip()
        try:
            chat_id = int(chat.get("id"))
        except (TypeError, ValueError):
            return
        old = message.get("migrate_from_chat_id")
        new = message.get("migrate_to_chat_id")
        if old is not None or new is not None:
            source, destination = (int(old), chat_id) if old is not None else (chat_id, int(new))
            if await self._is_allowed(source):
                await self._record_chat_migration(source, destination)
            return
        allowed = await self._is_allowed(chat_id)
        if not allowed and chat_id not in self.settings.harness_chat_ids:
            LOGGER.warning("Rejected chat_id=%s", chat_id)
            await self._show(chat_id, error("Private bot", "This chat is not on the allowlist."))
            return
        if allowed:
            try:
                message_id = int(message.get("message_id"))
            except (TypeError, ValueError):
                message_id = 0
            await self.store.remember_message(chat_id, message_id)
            await self.store.remember_chat(chat_id)
        if await self.harness.handle(message):
            return
        if not allowed or not text:
            return
        if text and await self._handle_agent_text_reply(chat_id, message, text):
            return
        if not text.startswith("/"):
            screen = await self.notes.accept_text(chat_id, text)
            if screen is not None:
                await self._show(chat_id, screen)
            return

        command, args = _parse_command(text)
        try:
            await self._dispatch(chat_id, command, args)
        except Exception as exc:
            LOGGER.exception("Command failed: /%s", command)
            await self._show(chat_id, error("Something went wrong", str(exc)))

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        if await self.harness.ui.callback(callback_query):
            return
        callback_id = str(callback_query.get("id") or "")
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        try:
            chat_id = int(chat.get("id"))
            message_id = int(message.get("message_id"))
        except (TypeError, ValueError):
            return

        if not await self._is_allowed(chat_id):
            if callback_id:
                await self._telegram.answer_callback_query(
                    callback_id,
                    text="This chat is not on the allowlist.",
                    show_alert=True,
                )
            return

        callback_data = str(callback_query.get("data") or "")
        if callback_data.startswith("agent_answer:"):
            await self._handle_agent_choice(callback_id, chat_id, message_id, callback_data)
            return

        parsed = _parse_callback_data(callback_data)
        if parsed is None:
            if callback_id:
                await self._telegram.answer_callback_query(callback_id, text="That action has expired.")
            return

        if callback_id:
            await self._telegram.answer_callback_query(callback_id)
        await self.store.remember_chat(chat_id)
        command, args = parsed
        try:
            await self._dispatch(chat_id, command, args, message_id=message_id)
        except Exception as exc:
            LOGGER.exception("Button action failed: %s", callback_query.get("data"))
            await self._show(chat_id, error("Something went wrong", str(exc)), message_id=message_id)

    async def _dispatch(
        self,
        chat_id: int,
        command: str,
        args: list[str],
        *,
        message_id: int | None = None,
    ) -> None:
        normalized = normalize_source_token(command)
        has_ctftime = self.registry.get("ctftime") is not None
        if normalized == "start":
            screen = welcome(
                snapshot=await self.dojo.inspector.overview(),
                include_ctftime=has_ctftime,
            )
        elif normalized == "clear":
            await self._clear_chat(chat_id)
            return
        elif normalized == "help":
            screen = help_screen(include_ctftime=has_ctftime)
        elif normalized in DojoFeature.COMMANDS:
            screen = await self.dojo.execute(normalized, args)
        elif normalized in {"latest", "sources"} or normalized in self.registry.command_aliases():
            await self._telegram.send_chat_action(chat_id)
            screen = await self.news.execute(normalized, args)
        elif normalized in NotesFeature.COMMANDS:
            screen = await self.notes.execute(chat_id, normalized, args)
        else:
            screen = error(
                f"Unknown command: /{command}",
                "Open /help or use the menu below.",
                markup=welcome(include_ctftime=has_ctftime).markup,
            )
        await self._show(chat_id, screen, message_id=message_id)

    async def _clear_chat(self, chat_id: int) -> None:
        message_ids = await self.store.take_message_history(chat_id)
        if not message_ids:
            return
        try:
            await self._telegram.delete_messages(chat_id, message_ids)
        except Exception:
            LOGGER.warning("Could not delete every tracked message for chat_id=%s", chat_id, exc_info=True)
            for message_id in message_ids:
                await self.store.remember_message(chat_id, message_id)

    async def _digest_loop(self) -> None:
        failures = 0
        retry_delay = None
        while True:
            try:
                if retry_delay is not None:
                    delay = retry_delay
                else:
                    delay, catching_up = _digest_delay(
                        datetime.now(ZoneInfo(self.settings.timezone)),
                        await self.store.digest_last_date(),
                        self.settings.daily_hour,
                    )
                    if catching_up:
                        LOGGER.info("Daily news digest was missed; catching up in %.0f seconds", delay)
                    else:
                        LOGGER.info("Next daily news digest in %.0f seconds", delay)
                await asyncio.sleep(delay)
                date = datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat()
                digest_id = f"{date}:{failures + 1}"
                started = time.monotonic()
                LOGGER.info("Digest started digest=%s attempt=%s", digest_id, failures + 1)
                await self._post_periodic_updates(digest_id=digest_id)
                await self.store.save_digest_date(date)
                LOGGER.info("Digest completed digest=%s elapsed_ms=%.0f", digest_id, 1000 * (time.monotonic() - started))
                failures, retry_delay = 0, None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                retry_delay = min(3600, 60 * 2 ** min(failures - 1, 6))
                sources = ",".join(exc.sources) if isinstance(exc, DigestIncompleteError) else "-"
                LOGGER.warning("Digest failed attempt=%s sources=%s retry_in=%s %s",
                               failures, sources, retry_delay, error_context(exc))

    async def _post_periodic_updates(self, *, digest_id: str = "manual") -> None:
        news_thread_id = self.settings.daily_news_thread_id
        if self.settings.daily_news_chat_id is not None:
            chat_ids = {self.settings.daily_news_chat_id}
        elif self.settings.target_chat_id is not None:
            chat_ids = {self.settings.target_chat_id}
            news_thread_id = None
        else:
            chat_ids = await self.store.known_chat_ids()
            news_thread_id = None
        failed_chats: list[int] = []
        failed_sources: set[str] = set()
        resolved_chats = {await self.store.resolve_chat(chat_id) for chat_id in chat_ids}
        for chat_id in resolved_chats:
            if not await self._is_allowed(chat_id):
                continue
            try:
                await self._post_new_items(
                    chat_id,
                    self.registry.automated_sources(self.settings.automated_source_ids),
                    message_thread_id=news_thread_id,
                    digest_id=digest_id,
                )
            except Exception as exc:
                # One broken chat must not starve the remaining digest targets.
                failed_chats.append(chat_id)
                if isinstance(exc, DigestIncompleteError):
                    failed_sources.update(exc.sources)
                else:
                    LOGGER.error("Digest chat failed digest=%s chat_id=%s %s", digest_id, chat_id, error_context(exc))
        if failed_chats:
            raise DigestIncompleteError(tuple(sorted(failed_sources)), tuple(failed_chats))

    async def _post_new_items(
        self,
        chat_id: int,
        sources: tuple[Source, ...],
        *,
        message_thread_id: int | None = None,
        digest_id: str = "manual",
    ) -> None:
        failed_sources: list[str] = []
        for source in sources:
            started = time.monotonic()
            try:
                items = await source.fetch(self.settings.max_limit, self._http)
            except Exception as exc:
                LOGGER.warning("Source failed digest=%s source=%s chat_id=%s phase=fetch elapsed_ms=%.0f %s",
                               digest_id, source.id, chat_id, 1000 * (time.monotonic() - started), error_context(exc))
                failed_sources.append(source.id)
                continue

            seen = await self.store.seen_urls(chat_id, source.id)
            fresh = []
            for item in items:
                if item.identity not in seen:
                    fresh.append(item)
                    seen.add(item.identity)
            LOGGER.info("Source fetched digest=%s source=%s chat_id=%s fetched=%s fresh=%s elapsed_ms=%.0f",
                        digest_id, source.id, chat_id, len(items), len(fresh), 1000 * (time.monotonic() - started))
            delivered = 0
            try:
                for batch in _update_batches(source.name, fresh):
                    await self._show(
                        chat_id,
                        new_updates((RenderSection(title=source.name, items=batch),)),
                        message_thread_id=message_thread_id,
                    )
                    # Checkpoint only accepted batches; retry unsent items.
                    await self.store.mark_seen(
                        chat_id, source.id, [item.identity for item in batch],
                    )
                    delivered += len(batch)
                if delivered:
                    LOGGER.info("Source delivered digest=%s source=%s chat_id=%s items=%s", digest_id, source.id, chat_id, delivered)
            except ChatMigratedError:
                raise
            except Exception as exc:
                LOGGER.warning("Source failed digest=%s source=%s chat_id=%s phase=delivery delivered=%s elapsed_ms=%.0f %s",
                               digest_id, source.id, chat_id, delivered, 1000 * (time.monotonic() - started), error_context(exc))
                failed_sources.append(source.id)

        if failed_sources:
            raise DigestIncompleteError(tuple(failed_sources))
        LOGGER.info("Daily news ingest completed digest=%s chat_id=%s", digest_id, chat_id)

    async def _is_allowed(self, chat_id: int) -> bool:
        if not self.settings.allowed_chat_ids or chat_id in self.settings.allowed_chat_ids:
            return True
        destination = await self.store.resolve_chat(chat_id)
        return destination in {await self.store.resolve_chat(allowed)
                               for allowed in self.settings.allowed_chat_ids}

    async def _handle_hook(self, payload: dict[str, Any]) -> int:
        async with self._integration_lock:
            return await self._deliver_hook(payload)

    async def _deliver_hook(self, payload: dict[str, Any]) -> int:
        error = validate_event(payload)
        if error:
            LOGGER.warning("Rejected hook event: %s", error)
            return 400
        chat_ids = await self._hook_chats()
        if not chat_ids:
            LOGGER.warning("Hook event %s arrived before any chat is known", payload.get("id"))
            return 503  # sender retries until a target chat exists
        event_id = str(payload["id"])
        screen = Screen(render_event(payload), event_markup(str(payload.get("url") or "")))
        for chat_id in sorted(chat_ids):
            if event_id in await self.store.hook_seen(chat_id):
                continue
            try:
                await self._show(chat_id, screen)
            except Exception:
                LOGGER.exception("Hook delivery failed for chat_id=%s", chat_id)
                return 502  # unsent chats stay unmarked and get the retry
            await self.store.mark_hook_sent(chat_id, event_id)
        return 200

    async def _hook_chats(self) -> set[int]:
        if self.settings.target_chat_id is not None:
            return {await self.store.resolve_chat(self.settings.target_chat_id)}
        return {chat_id for chat_id in await self.store.known_chat_ids() if await self._is_allowed(chat_id)}

    async def _handle_agent_message(self, payload: dict[str, Any]) -> HandlerResult:
        async with self._integration_lock:
            return await self._deliver_agent_message(payload)

    async def _deliver_agent_message(self, payload: dict[str, Any]) -> HandlerResult:
        validation_error = validate_message(payload)
        if validation_error:
            LOGGER.warning("Rejected agent message: %s", validation_error)
            return 400
        chat_ids = await self._hook_chats()
        if not chat_ids:
            LOGGER.warning("Agent message arrived before any chat is known")
            return 503

        dedupe_key = str(payload.get("dedupe_key") or "").strip()
        event_id = f"mcp:{dedupe_key}" if dedupe_key else ""
        screen = Screen(_render_agent_message(payload))
        delivered = 0
        skipped = 0
        for chat_id in sorted(chat_ids):
            if event_id and event_id in await self.store.hook_seen(chat_id):
                skipped += 1
                continue
            try:
                await self._show(chat_id, screen)
            except Exception:
                LOGGER.exception("Agent message delivery failed for chat_id=%s", chat_id)
                return 502
            delivered += 1
            if event_id:
                await self.store.mark_hook_sent(chat_id, event_id)
        return 200, {"ok": True, "delivered": delivered, "skipped": skipped}

    async def _handle_agent_action(self, payload: dict[str, Any]) -> HandlerResult:
        action = payload.get("action")
        if action == "check_reply":
            return await self._handle_agent_reply_check(payload)
        async with self._integration_lock:
            if action == "ask_user":
                return await self._handle_agent_question(payload)
            if action == "send_artifact":
                return await self._handle_agent_artifact(payload)
        return 400

    async def _handle_agent_question(self, payload: dict[str, Any]) -> HandlerResult:
        request_id = str(payload.get("request_id") or f"ask_{secrets.token_hex(8)}")
        question = str(payload.get("question") or "").strip()
        source = str(payload.get("source") or "").strip()
        choices = payload.get("choices", [])
        if not AGENT_REQUEST_ID.fullmatch(request_id):
            return 400
        if not question or len(question) > 1_000 or len(source) > 100:
            return 400
        if not isinstance(choices, list) or len(choices) > 6:
            return 400
        clean_choices: list[str] = []
        for choice in choices:
            if not isinstance(choice, str) or not choice.strip() or len(choice.strip()) > 40:
                return 400
            clean_choices.append(choice.strip())
        if len(set(clean_choices)) != len(clean_choices):
            return 400

        chat_ids = await self._hook_chats()
        if not chat_ids:
            return 503
        created = await self.store.create_agent_request(request_id, question, clean_choices, source)
        sent_chats: set[int] = set()
        if not created:
            existing = await self.store.agent_request(request_id)
            if existing is None or any(existing[key] != value for key, value in (
                ("question", question), ("choices", clean_choices), ("source", source),
            )):
                return 400
            if existing.get("status") == "answered":
                return 200, {"ok": True, "request_id": request_id, "status": "answered",
                             "delivered": 0, "skipped": len(chat_ids)}
            sent_chats = {prompt["chat_id"] for prompt in existing.get("prompts", [])}

        markup: dict[str, object]
        if clean_choices:
            markup = {
                "inline_keyboard": [
                    [{"text": choice, "callback_data": f"agent_answer:{request_id}:{index}"}]
                    for index, choice in enumerate(clean_choices)
                ]
            }
        else:
            markup = {
                "force_reply": True,
                "selective": True,
                "input_field_placeholder": "Reply to the agent",
            }
        screen = Screen(_render_agent_question(question, source), markup)
        delivered = 0
        skipped = 0
        for chat_id in sorted(chat_ids):
            if chat_id in sent_chats:
                skipped += 1
                continue
            try:
                message_id = await self._show(chat_id, screen)
            except Exception:
                LOGGER.exception("Agent question delivery failed for chat_id=%s", chat_id)
                return 502
            delivered += 1
            if message_id is not None:
                await self.store.remember_agent_prompt(
                    request_id, await self.store.resolve_chat(chat_id), message_id,
                )
        return 200, {
            "ok": True,
            "request_id": request_id,
            "status": "pending",
            "delivered": delivered,
            "skipped": skipped,
        }

    async def _handle_agent_reply_check(self, payload: dict[str, Any]) -> HandlerResult:
        request_id = str(payload.get("request_id") or "")
        if not AGENT_REQUEST_ID.fullmatch(request_id):
            return 400
        request = await self.store.agent_request(request_id)
        if request is None:
            return 404
        return 200, {
            "ok": True,
            "request_id": request_id,
            "status": str(request.get("status", "pending")),
            "answer": str(request.get("answer", "")),
        }

    async def _handle_agent_artifact(self, payload: dict[str, Any]) -> HandlerResult:
        filename = Path(str(payload.get("filename") or "artifact.bin")).name[:120]
        encoded = payload.get("content_base64")
        caption = str(payload.get("caption") or "").strip()
        source = str(payload.get("source") or "").strip()
        dedupe_key = str(payload.get("dedupe_key") or "").strip()
        if not filename or not isinstance(encoded, str) or len(caption) > 800 or len(source) > 100:
            return 400
        if len(dedupe_key) > 100:
            return 400
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return 400
        if not content or len(content) > MAX_ARTIFACT_BYTES:
            return 413

        chat_ids = await self._hook_chats()
        if not chat_ids:
            return 503
        event_id = f"mcp-artifact:{dedupe_key}" if dedupe_key else ""
        telegram_caption = _render_artifact_caption(caption, source)
        delivered = 0
        skipped = 0
        for chat_id in sorted(chat_ids):
            if event_id and event_id in await self.store.hook_seen(chat_id):
                skipped += 1
                continue
            try:
                destination, sent = await self._send_to_chat(
                    chat_id, self._telegram.send_document,
                    filename,
                    content,
                    caption=telegram_caption,
                )
                await self.store.remember_message(destination, int(sent.get("message_id", 0)))
            except Exception:
                LOGGER.exception("Agent artifact delivery failed for chat_id=%s", chat_id)
                return 502
            delivered += 1
            if event_id:
                await self.store.mark_hook_sent(chat_id, event_id)
        return 200, {"ok": True, "delivered": delivered, "skipped": skipped}

    async def _handle_agent_choice(
        self,
        callback_id: str,
        chat_id: int,
        message_id: int,
        callback_data: str,
    ) -> None:
        _, _, value = callback_data.partition(":")
        request_id, separator, raw_index = value.rpartition(":")
        request = await self.store.agent_request(request_id)
        try:
            index = int(raw_index)
            choices = list((request or {}).get("choices", []))
            if (not separator or not 0 <= index < len(choices)
                    or {"chat_id": chat_id, "message_id": message_id} not in (request or {}).get("prompts", [])):
                raise ValueError("Invalid question button")
            answer = str(choices[index])
        except (TypeError, ValueError, IndexError):
            if callback_id:
                await self._telegram.answer_callback_query(callback_id, text="That question has expired.")
            return
        answered = await self.store.answer_agent_request(request_id, answer)
        if callback_id:
            text = "Sent to the agent." if (request or {}).get("status") == "pending" else "Already answered."
            await self._telegram.answer_callback_query(callback_id, text=text)
        if answered is not None:
            await self._show(
                chat_id,
                Screen(_render_answered_question(answered)),
                message_id=message_id,
            )

    async def _handle_agent_text_reply(
        self,
        chat_id: int,
        message: dict[str, Any],
        text: str,
    ) -> bool:
        reply_to = message.get("reply_to_message") or {}
        try:
            prompt_message_id = int(reply_to.get("message_id"))
        except (AttributeError, TypeError, ValueError):
            return False
        pending = await self.store.pending_agent_request_for_message(chat_id, prompt_message_id)
        if pending is None:
            return False
        request_id, _ = pending
        answered = await self.store.answer_agent_request(request_id, text)
        if answered is None:
            return False
        await self._show(
            chat_id,
            Screen(_render_answered_question(answered)),
            message_id=prompt_message_id,
        )
        return True

    @property
    def _http(self) -> httpx.AsyncClient:
        if self.http is None:
            raise RuntimeError("HTTP client is not ready")
        return self.http

    @property
    def _telegram(self) -> TelegramAPI:
        if self.telegram is None:
            raise RuntimeError("Telegram client is not ready")
        return self.telegram


def _seconds_until_daily_digest(now: datetime, hour: int) -> float:
    local_now = now
    target = local_now.replace(
        hour=hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if target <= local_now:
        target += timedelta(days=1)
    return max(0.0, target.timestamp() - local_now.timestamp())


def _digest_delay(now: datetime, last_date: str, hour: int) -> tuple[float, bool]:
    """Schedule the digest; catch up quickly if today's slot was missed."""
    local_now = now
    if local_now.hour >= hour and last_date != local_now.date().isoformat():
        return 60.0, True
    return _seconds_until_daily_digest(local_now, hour), False


def _update_batches(source_name: str, items: list[IntelItem]) -> list[tuple[IntelItem, ...]]:
    batches: list[tuple[IntelItem, ...]] = []
    current: list[IntelItem] = []
    for item in items:
        candidate = [*current, item]
        preview = new_updates(
            (RenderSection(title=source_name, items=tuple(candidate)),)
        )
        if current and "More stories" in preview.text:
            batches.append(tuple(current))
            current = [item]
        else:
            current = candidate
    if current:
        batches.append(tuple(current))
    return batches


def _parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.split()
    command = parts[0].split("@", 1)[0].removeprefix("/").casefold()
    return command, parts[1:]


def _parse_callback_data(value: str) -> tuple[str, list[str]] | None:
    action, separator, command = value.partition(":")
    if separator != ":" or action not in {"nav", "run", "note", "dojo"} or not command:
        return None
    if action == "note":
        operation, operation_separator, item_id = command.partition(":")
        if operation_separator != ":" or not operation or not item_id:
            return None
        if any(character.isspace() for character in operation + item_id):
            return None
        return f"{action}_{operation}", [item_id]
    if action == "dojo":
        if any(character.isspace() for character in command) or ":" in command:
            return None
        return f"dojo_{command}", []
    if any(character.isspace() for character in command):
        return None
    if action == "run":
        parts = command.split(":")
        if not all(parts):
            return None
        return parts[0], parts[1:]
    return _parse_command(f"/{command}")


def _log_background_failure(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        LOGGER.exception("Background task crashed")


def _render_agent_message(payload: dict[str, Any]) -> str:
    urgency = str(payload.get("urgency") or "info")
    icon = {"info": "🤖", "success": "✅", "warning": "⚠️", "error": "🚨"}[urgency]
    title = str(payload.get("title") or "Agent update").strip()
    message = html.escape(str(payload["message"]).strip())
    source = str(payload.get("source") or "").strip()
    return icon + " " + card(title, message, hint=f"From {source}" if source else "")


def _render_agent_question(question: str, source: str) -> str:
    return card("Agent question", html.escape(question), hint=f"From {source}" if source else "")


def _render_answered_question(request: dict[str, Any]) -> str:
    body = (html.escape(str(request.get("question", "")))
            + f"\n\n<b>Answer</b>\n{html.escape(str(request.get('answer', '')))}")
    source = str(request.get("source", ""))
    return card("Answered", body, hint=f"From {source}" if source else "")


def _render_artifact_caption(caption: str, source: str) -> str:
    lines = []
    if caption:
        lines.append(html.escape(caption))
    if source:
        lines.append(f"<i>From {html.escape(source)}</i>")
    return "\n\n".join(lines)
