"""Telegram topics attached to durable OpenCode sessions."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import mimetypes
import re
import secrets
import string
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from riolu.agent.client import AgentWorkerClient
from riolu.agent.errors import OpenCodeError, failure_reason
from riolu.agent.commands import ALLOWED_COMMANDS
from riolu.agent.ui import HarnessUI
from riolu.agent.usage import ALL_PROVIDERS, format_usage, usage_rpc_path
from riolu.ui.style import code, commands, notice
from riolu.ui.text import markdown_chunks, markdown_to_telegram_html

LOGGER = logging.getLogger(__name__)

_TOPIC_NAME = re.compile(r"^#([\w-]{1,32})(?:\s+|$)")
_ARTIFACT_MARKER = re.compile(
    r"^[ \t]*@riolu-artifact[ \t]+"
    r"(?:\"(?P<quoted>[^\"]+)\"|'(?P<single>[^']+)'|(?P<plain>\S+))"
    r"(?:[ \t]+(?P<caption>.*?))?[ \t]*$",
    re.MULTILINE,
)
MAX_ARTIFACT_BYTES = 10_000_000
SENSITIVE_FILENAMES = frozenset({".env", "credentials", "credentials.json", "id_rsa", "id_ed25519"})
SENSITIVE_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})


def _split_artifact_match(match: re.Match[str]) -> tuple[str, str]:
    path = next(value for value in match.group("quoted", "single", "plain") if value is not None)
    return path, (match.group("caption") or "").strip()


def _extract_artifacts(
    text: str, cwd: str
) -> tuple[str, tuple[tuple[Path, str], ...], tuple[str, ...]]:
    """Extract file markers, returning clean text, sendable files, and errors."""
    artifacts: list[tuple[Path, str]] = []
    errors: list[str] = []
    for match in _ARTIFACT_MARKER.finditer(text):
        path_text, caption = _split_artifact_match(match)
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = Path(cwd or ".") / path
        try:
            path = path.resolve(strict=True)
        except OSError:
            errors.append(f"Artifact not found: {path_text}")
            continue
        if not path.is_file():
            errors.append(f"Artifact is not a regular file: {path_text}")
            continue
        if path.name.casefold() in SENSITIVE_FILENAMES or path.name.casefold().startswith(".env."):
            errors.append(f"Refusing to return credential-looking file: {path.name}")
            continue
        if path.suffix.casefold() in SENSITIVE_SUFFIXES:
            errors.append(f"Refusing to return credential-looking file: {path.name}")
            continue
        size = path.stat().st_size
        if size <= 0 or size > MAX_ARTIFACT_BYTES:
            errors.append(
                f"Artifact must be between 1 byte and {MAX_ARTIFACT_BYTES} bytes: {path_text}"
            )
            continue
        artifacts.append((path, caption[:800]))
    clean = _ARTIFACT_MARKER.sub("", text).replace("\n\n\n", "\n\n").strip()
    return clean, tuple(artifacts), tuple(errors)


def _harness_context(thread: dict) -> str:
    return (
        "[Riolu Telegram harness]\n"
        f"session_id={thread.get('session_id') or ''}\n"
        f"chat_id={thread.get('chat_id') or ''}\n"
        f"topic_id={thread.get('topic_id') or ''}\n"
        f"working_directory={thread.get('cwd') or ''}\n"
        "To return a local file in this Telegram topic, include one line per file:\n"
        "@riolu-artifact /absolute/path/to/file optional caption\n"
        "Use absolute paths. Maximum size is 10 MB. Never return credential files.\n"
        "[/Riolu Telegram harness]\n\n"
    )


def _session_missing(exc: BaseException) -> bool:
    """Recognize OpenCode's definitive missing-session error across RPC."""
    if isinstance(exc, OpenCodeError):
        return exc.code == "session_missing"
    message = str(exc).casefold()
    return "sessionnotfounderror" in message or "session not found" in message


def _message_created(message: dict) -> int:
    """Return a native message timestamp in milliseconds, or zero."""
    info = message.get("info") or {}
    value = info.get("time") or message.get("time") or {}
    if not isinstance(value, dict):
        return 0
    try:
        return int(value.get("created") or value.get("updated") or 0)
    except (TypeError, ValueError):
        return 0


def _message_text(message: dict) -> str:
    """Return an assistant or user message's visible text."""
    parts = message.get("parts") or message.get("content") or []
    return "\n\n".join(
        str(part.get("text") or "") for part in parts
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
    ).strip()


def _split_topic_name(args: str) -> tuple[str, str]:
    """Split a leading #topic-name token from command arguments."""
    stripped = args.strip()
    match = _TOPIC_NAME.match(stripped)
    if not match:
        return args, ""
    return stripped[match.end() :].lstrip(), match.group(1)


def command(text: str) -> tuple[str, str]:
    parts = text.split(maxsplit=1)
    head, tail = parts[0] if parts else "", parts[1] if len(parts) > 1 else ""
    return head.split("@", 1)[0].removeprefix("/").lower(), tail.strip()


class HarnessChat:
    def __init__(self, settings, store, telegram, http) -> None:
        self.settings, self.store = settings, store
        self.telegram, self.http = telegram, http
        self.threads: dict[str, dict] | None = None
        self.jobs: dict[str, asyncio.Task] = {}
        self.admissions: set[str] = set()
        self.actions: set[asyncio.Task] = set()
        self.locks: dict[str, asyncio.Lock] = {}
        self.pin_locks: dict[str, asyncio.Lock] = {}
        self.name_locks: dict[str, asyncio.Lock] = {}
        self.requests: dict[str, tuple[dict, dict]] = {}
        self.resolved_requests: set[tuple[str, str, str]] = set()
        self.reply_locks: dict[str, asyncio.Lock] = {}
        self.recovery_retry_after: dict[str, float] = {}
        self.closing = False
        self.load_lock = asyncio.Lock()
        self.attachment_lock = asyncio.Lock()
        self.ui = HarnessUI(self)
        self.worker = AgentWorkerClient(
            http,
            getattr(settings, "agent_url", "http://127.0.0.1:8302"),
            getattr(settings, "agent_token", ""),
        )

    async def close(self) -> None:
        self.closing = True
        await self.ui.close()
        for task in self.actions:
            task.cancel()
        await asyncio.gather(*self.actions, return_exceptions=True)
        for task in self.jobs.values():
            task.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        await self.worker.close()

    async def _load(self) -> None:
        async with self.load_lock:
            if self.threads is not None:
                return
            self.threads = await self.store.harness_threads()
            for thread in self.threads.values():
                thread["effort"] = thread.get("effort") or thread.pop("variant", None) or "default"
                thread.pop("queued_prompts", None)
                thread.pop("active_prompt", None)
                thread.pop("queue_paused", None)
                if thread.get("status") == "stopped":
                    thread["status"] = "interrupted"
                if thread.get("status") in {"working", "starting", "waiting for input", "stopping"}:
                    thread["status"] = "disconnected; checking session"
                    thread.pop("turn_id", None)

    async def _save(self, thread: dict) -> None:
        await self.store.save_harness_thread(self._key(thread), thread)

    async def _mark_session_missing(self, thread: dict) -> None:
        if thread.get("session_missing"):
            return
        thread["session_missing"] = True
        thread["status"] = "session unavailable"
        thread.pop("turn_anchor", None)
        key = self._key(thread)
        self.recovery_retry_after.pop(key, None)
        job = self.jobs.get(key)
        if job and job is not asyncio.current_task():
            job.cancel()
        for token, (owner, _) in list(self.requests.items()):
            if self._key(owner) == key:
                self.requests.pop(token, None)
        await self._save(thread)
        await self.pin(thread)

    async def refresh_panels(self) -> None:
        """Upgrade existing pins without resuming sessions or replaying prompts."""
        while True:
            try:
                await self._load()
                for thread in list(self.threads.values()):
                    if (
                        thread.get("chat_id") in self.settings.harness_chat_ids
                        and not thread.get("deleted")
                        and thread.get("pin_id")
                    ):
                        await self.pin(thread)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("Harness panel refresh failed; retrying", exc_info=True)
                await asyncio.sleep(5)

    async def _supervise_thread(self, thread: dict) -> None:
        """Reconcile one topic against native OpenCode and drain durable work."""
        key = self._key(thread)
        await self._check_requests(thread)
        await self._ensure_recovery(thread)

        job = self.jobs.get(key)
        if job and not job.done():
            return

        # Repair stale local statuses after native work has completed.
        if thread.get("turn_anchor"):
            return
        if thread.get("status") in {
            "working", "starting", "stopping", "checking OpenCode",
            "disconnected; checking session",
        }:
            try:
                await self._reconcile_native_state(thread)
            except Exception:
                LOGGER.warning(
                    "Could not reconcile OpenCode state for topic %s",
                    thread.get("topic_id"), exc_info=True,
                )

    async def watch_requests(self) -> None:
        """Keep native questions and permissions available across bot restarts."""
        first_pass = True
        while True:
            try:
                await self._load()
                for thread in list(self.threads.values()):
                    if (thread.get("deleted") or thread.get("session_missing") or not thread.get("session_id")
                            or thread.get("chat_id") not in self.settings.harness_chat_ids):
                        continue
                    key = self._key(thread)
                    job = self.jobs.get(key)
                    if (not first_pass and thread.get("status") not in {
                            "working", "waiting for input", "starting", "stopping",
                            "checking OpenCode", "disconnected; checking session"
                        } and not (job and not job.done())
                            and not thread.get("turn_anchor")
                            and not any(self._key(owner) == key for owner, _ in self.requests.values())):
                        continue
                    try:
                        await self._supervise_thread(thread)
                    except Exception:
                        # One stale or inaccessible topic must not stop checks
                        # for every other active harness topic.
                        LOGGER.warning(
                            "Could not check OpenCode requests for topic %s",
                            thread.get("topic_id"), exc_info=True,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("OpenCode request supervision failed", exc_info=True)
            first_pass = False
            await asyncio.sleep(3)

    async def _check_requests(self, thread: dict) -> None:
        active: set[tuple[str, str]] = set()
        for kind in ("permission", "question"):
            for request in await self._oc(thread, "GET", f"/{kind}"):
                request_id = str(request.get("id") or "")
                if request.get("sessionID") != thread["session_id"] or not request_id:
                    continue
                active.add((kind, request_id))
                if (self._key(thread), kind, request_id) in self.resolved_requests:
                    continue
                if not thread.get("turn_anchor") and not self.jobs.get(self._key(thread)):
                    messages = await self._oc(
                        thread, "GET", f"/session/{thread['session_id']}/message"
                    )
                    users = [index for index, message in enumerate(messages)
                             if (message.get("info") or {}).get("role") == "user"]
                    if users:
                        previous = messages[users[-1] - 1] if users[-1] else None
                        thread["turn_anchor"] = (previous or {}).get("info", {}).get("id") or "start"
                        await self._save(thread)
                if any(
                    self._key(owner) == self._key(thread)
                    and pending.get("kind") == kind
                    and pending.get("id") == request_id
                    for owner, pending in self.requests.values()
                ):
                    continue
                token = secrets.token_hex(3)
                pending = {"kind": kind, **request}
                self.requests[token] = (thread, pending)
                try:
                    await self.ui.request(thread, token, pending)
                except Exception:
                    self.requests.pop(token, None)
                    raise
        for token, (owner, pending) in list(self.requests.items()):
            if self._key(owner) != self._key(thread):
                continue
            if (pending.get("kind"), pending.get("id")) in active:
                continue
            async with self.reply_locks.setdefault(self._key(thread), asyncio.Lock()):
                if self.requests.pop(token, None):
                    await self.ui.resolved(thread, pending)
        self.resolved_requests.difference_update({
            entry for entry in self.resolved_requests
            if entry[0] == self._key(thread) and entry[1:] not in active
        })

    async def _ensure_recovery(self, thread: dict) -> None:
        key = self._key(thread)
        job = self.jobs.get(key)
        if key in self.admissions or thread.get("session_missing") or not thread.get("turn_anchor") or (job and not job.done()):
            return
        retry_after = self.recovery_retry_after.get(key)
        if retry_after and time.monotonic() < retry_after:
            return
        self.recovery_retry_after[key] = time.monotonic() + 15
        self.jobs[key] = asyncio.create_task(self._recover_turn(thread))

    async def _native_inbox(self, thread: dict) -> list[dict]:
        result = await self._oc(
            thread, "GET", f"/api/session/{thread['session_id']}/inbox"
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [item for item in result.get("data", []) if isinstance(item, dict)]
        return []

    async def _native_run_state(self, thread: dict) -> str:
        """Classify native execution using messages, inbox, outcomes, and idle."""
        if any(
            self._key(owner) == self._key(thread)
            for owner, _ in self.requests.values()
        ):
            return "waiting for input"
        if await self._native_inbox(thread):
            return "working"

        native = await self._oc_session(thread)
        messages = await self._oc(
            thread, "GET", f"/session/{thread['session_id']}/message"
        )
        last_user = -1
        last_idle = -1
        last_user_created = 0
        for index, message in enumerate(messages):
            role = (message.get("info") or {}).get("role") or message.get("type")
            if role == "user":
                last_user = index
                last_user_created = _message_created(message)
            elif role == "idle":
                last_idle = index

        native_idle = int((native.get("time") or {}).get("idle") or 0)
        outcome = native.get("outcome")
        if last_user <= last_idle or (native_idle and native_idle >= last_user_created):
            if outcome == "failed":
                return "error"
            if outcome == "interrupted":
                return "interrupted"
            return "ready"
        # A terminal outcome can remain from a prior turn while a newly
        # admitted prompt is running. Treat it as active until native idle
        # advances past the newest user message.
        return "working"

    async def _reconcile_native_state(self, thread: dict) -> str:
        state = await self._native_run_state(thread)
        if thread.get("status") != state:
            thread["status"] = state
            await self._save(thread)
            await self.pin(thread)
        return state

    async def _native_active_prompt(self, thread: dict, state: str) -> str:
        """Return the newest prompt that OpenCode is still working or waiting on."""
        if state not in {"working", "waiting for input"}:
            return ""
        messages = await self._oc(
            thread, "GET", f"/session/{thread['session_id']}/message"
        )
        users = [
            (index, message) for index, message in enumerate(messages)
            if ((message.get("info") or {}).get("role") or message.get("type")) == "user"
        ]
        if not users:
            return ""
        last_index, message = users[-1]
        if any(
            ((item.get("info") or {}).get("role") or item.get("type")) == "idle"
            for item in messages[last_index + 1:]
        ):
            return ""
        text = _message_text(message)
        context = _harness_context(thread)
        if context and text.startswith(context):
            text = text[len(context):]
        return text.strip()[:500]

    def _native_prompt_label(self, thread: dict, text: object) -> str:
        text = str(text or "").strip()
        context = _harness_context(thread)
        if context and text.startswith(context):
            text = text[len(context):]
        return text.strip()[:160]

    async def _recover_turn(self, thread: dict) -> None:
        """Deliver each native-queued prompt result in admission order."""
        key = self._key(thread)
        started = asyncio.get_running_loop().time()

        def role(message: dict) -> str:
            return (message.get("info") or {}).get("role") or message.get("type")

        def message_id(message: dict) -> str:
            return str((message.get("info") or {}).get("id") or message.get("id") or "")

        message_text = _message_text

        try:
            while not thread.get("deleted"):
                native = await self._oc_session(thread)
                messages = await self._oc(
                    thread, "GET", f"/session/{thread['session_id']}/message"
                )
                anchor = thread.get("turn_anchor")
                anchor_index = next(
                    (index for index, message in enumerate(messages)
                     if message_id(message) == anchor), -1
                )
                current = messages[anchor_index + 1:]
                users = [
                    index for index, message in enumerate(current)
                    if role(message) == "user"
                ]

                if not users:
                    inbox = await self._native_inbox(thread)
                    if inbox:
                        # Native queue items admitted after a terminal failure do
                        # not wake execution by themselves. Promote the first
                        # item to steering; OpenCode then drains the rest.
                        native_outcome = native.get("outcome")
                        elapsed = asyncio.get_running_loop().time() - started
                        if native_outcome in {"failed", "interrupted"} and elapsed >= 3:
                            inbox_id = str(inbox[0].get("id") or "")
                            if inbox_id:
                                await self._oc(
                                    thread,
                                    "PATCH",
                                    f"/api/session/{thread['session_id']}/inbox/{quote(inbox_id, safe='')}",
                                    {"delivery": "steer"},
                                )
                                started = asyncio.get_running_loop().time()
                        await asyncio.sleep(3)
                        continue
                    if asyncio.get_running_loop().time() - started >= 30:
                        await self.ui.notice(
                            thread, "prompt was not admitted", title="opencode"
                        )
                        thread["status"] = "error"
                        thread.pop("turn_anchor", None)
                        break
                    await asyncio.sleep(3)
                    continue

                if any(self._key(owner) == key for owner, _ in self.requests.values()):
                    await asyncio.sleep(3)
                    continue

                # A user segment is complete when the next admitted user or an
                # idle marker proves its assistant output is final. Segments
                # followed by another user completed successfully; the final
                # segment inherits the native run's terminal outcome.
                completed = []
                for position, user_index in enumerate(users):
                    next_user = users[position + 1] if position + 1 < len(users) else None
                    idle_after = next(
                        (index for index in range(user_index + 1, len(current))
                         if role(current[index]) == "idle"), None,
                    )
                    boundaries = [value for value in (next_user, idle_after) if value is not None]
                    boundary = min(boundaries) if boundaries else None
                    if boundary is None:
                        continue
                    segment = current[user_index + 1:boundary]
                    output = next(
                        (message_text(message) for message in reversed(segment)
                         if role(message) == "assistant" and message_text(message)),
                        "",
                    )
                    terminal = current[boundary].get("info") or current[boundary]
                    outcome = terminal.get("outcome") if role(current[boundary]) == "idle" else "succeeded"
                    completed.append((boundary, next_user, output, outcome or native.get("outcome"), segment))

                for boundary, next_user, output, terminal_outcome, segment in completed:
                    if terminal_outcome in {"failed", "interrupted"}:
                        if output:
                            await self.say(
                                thread, output,
                                status=f"opencode: {terminal_outcome}",
                            )
                        else:
                            await self.ui.notice(
                                thread, failure_reason(segment, native) if terminal_outcome == "failed"
                                else "The run was interrupted. Send a prompt to continue.",
                                title=f"opencode: {terminal_outcome}",
                            )
                    elif output:
                        await self.say(thread, output, status="opencode: completed")
                    else:
                        await self.ui.notice(thread, "completed", title="opencode")
                    if next_user is not None:
                        thread["turn_anchor"] = message_id(current[next_user - 1])
                    else:
                        thread["turn_anchor"] = message_id(current[boundary])
                    await self._save(thread)

                outcome = native.get("outcome")
                last_user_created = _message_created(current[users[-1]])
                native_idle = int((native.get("time") or {}).get("idle") or 0)
                finished = any(role(message) == "idle" for message in current[users[-1] + 1:])
                native_finished = bool(native_idle and native_idle >= last_user_created)
                if finished or native_finished:
                    if await self._native_inbox(thread):
                        # Another prompt was admitted while this result was being
                        # delivered; keep the same recovery supervisor active.
                        thread["status"] = "working"
                        await self.pin(thread)
                        await asyncio.sleep(3)
                        continue
                    if outcome in {"failed", "interrupted"}:
                        thread["status"] = "error" if outcome == "failed" else "interrupted"
                    else:
                        thread["status"] = "ready"
                    thread.pop("turn_anchor", None)
                    self.recovery_retry_after.pop(key, None)
                    break

                thread["status"] = "working"
                await self.pin(thread)
                await asyncio.sleep(3)
            await self.pin(thread)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _session_missing(exc):
                return
            LOGGER.warning(
                "Could not recover OpenCode turn for topic %s; retrying",
                thread.get("topic_id"), exc_info=True,
            )
            thread["status"] = "checking OpenCode"
            self.recovery_retry_after[key] = time.monotonic() + 15
            await self.pin(thread)
        finally:
            if self.jobs.get(key) is asyncio.current_task():
                self.jobs.pop(key, None)

    async def sync_names(self) -> None:
        """Read native names without resuming sessions or starting model turns."""
        while True:
            try:
                await self.refresh_names()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Name/runtime reconciliation is support machinery. A transient
                # failure must not terminate this long-running supervisor.
                LOGGER.warning("Harness name synchronization failed", exc_info=True)
            await asyncio.sleep(15)

    async def refresh_names(self) -> None:
        await self._load()
        results = await asyncio.gather(
            *(self._sync_name(t) for t in list(self.threads.values())
              if t.get("chat_id") in self.settings.harness_chat_ids
              and t.get("harness") == "opencode"
              and not t.get("deleted") and not t.get("session_missing") and t.get("session_id")),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                LOGGER.warning("Could not sync one harness topic: %s", type(result).__name__)

    async def _sync_name(self, thread: dict, title: str | None = None) -> bool:
        async with self.name_locks.setdefault(self._key(thread), asyncio.Lock()):
            if thread.get("deleted") or thread.get("session_missing") or not thread.get("session_id"):
                return False
            try:
                await self._sync_runtime(thread)
                if thread.get("session_missing"):
                    return False
                if title is None:
                    async with asyncio.timeout(10):
                        result = await self._oc(
                            thread,
                            "GET",
                            f"/session/{quote(thread['session_id'], safe='')}",
                        )
                        title = result.get("title")
                return await self._set_topic_name(thread, title)
            except Exception as exc:
                # Cosmetic failures must not stop a prompt; the next sweep retries.
                LOGGER.warning("Could not sync harness topic name: %s", type(exc).__name__)
                return False

    async def _sync_runtime(self, thread: dict) -> None:
        """Keep the panel aligned with settings chosen in either OpenCode UI."""
        if thread.get("deleted") or thread.get("session_missing"):
            return
        try:
            native = await self._oc_session(thread)
            selected = native.get("model") or {}
            runtime = {"model": f"{selected['providerID']}/{selected['id']}" if selected else "",
                       "variant": selected.get("variant") or "default", "agent": native.get("agent")}
            if not selected and thread.get("model") == "default":
                runtime = await self._oc(thread, "GET", f"/session/{quote(thread['session_id'], safe='')}/runtime")
        except Exception as exc:
            LOGGER.debug("Could not sync harness runtime settings: %s", type(exc).__name__)
            return
        model = runtime.get("model")
        if not model:
            return
        previous = dict(thread)
        thread["model"] = model
        thread["effort"] = runtime.get("variant") or "default"
        if runtime.get("agent"):
            thread["agent"] = runtime["agent"]
        if thread != previous:
            await self._save(thread)
            await self.pin(thread)

    async def _set_topic_name(self, thread: dict, title: str | None) -> bool:
        if not isinstance(title, str) or not title.strip() or thread.get("deleted"):
            return False
        if thread.get("session_name") == title and thread.get("name") == title[:128]:
            return True
        thread["session_name"] = title
        name = title[:128]  # Telegram's limit; never truncate the native title.
        if thread.get("name") != name:
            try:
                await self.telegram().request("editForumTopic", {
                    "chat_id": thread["chat_id"],
                    "message_thread_id": thread["topic_id"], "name": name})
            except Exception as exc:
                error = str(exc).lower()
                if any(marker in error for marker in ("topic_deleted", "topic_id_invalid", "message thread not found")):
                    thread["deleted"] = True
                    await self._save(thread)
                    return False
                if "not modified" not in error and "topic_not_modified" not in error:
                    await self._save(thread)
                    raise
            thread["name"] = name
        await self._save(thread)
        return True

    @staticmethod
    def _key(thread: dict) -> str:
        return f"{thread['chat_id']}:{thread['topic_id']}"

    async def handle(self, message: dict) -> bool:
        await self._load()
        chat_id = int(message["chat"]["id"])
        topic_id = int(message.get("message_thread_id") or 0)
        text = (message.get("text") or "").strip()
        cmd, args = command(text)
        thread = self.threads.get(f"{chat_id}:{topic_id}")
        if (not text.startswith("/") or cmd not in {"opencode", "resume"}) and not thread:
            return False
        # A harness can execute code as the service user. Require an explicitly
        # trusted chat (private or topic group); the general news bot can still
        # use open access.
        if not self.ui.trusted(message["chat"]):
            chat_type = message["chat"].get("type", "private")
            await self.telegram().send_message(
                chat_id,
                "Harness commands require an allowlisted chat. This chat id is "
                f"<code>{chat_id}</code> ({chat_type}); add it to RIOLU_HARNESS_CHAT_IDS.",
            )
            return True
        acknowledgement = asyncio.create_task(self._ack(chat_id, message))
        self.actions.add(acknowledgement)
        acknowledgement.add_done_callback(self.actions.discard)
        key = f"{chat_id}:{topic_id}"
        urgent = cmd in {
            "stop",
            "interrupt",
            "approve",
            "deny",
            "answer",
            "delete",
            "steer",
        } and text.startswith("/")
        task = asyncio.create_task(
            self._action(message, thread, cmd, args, urgent, key)
        )
        self.actions.add(task)
        task.add_done_callback(self.actions.discard)
        return True

    async def _ack(self, chat_id: int, message: dict) -> None:
        # Service updates have a message_id too, but are not user prompts.
        # Match user content positively so new service-message types stay quiet.
        if not any(message.get(field) for field in (
            "text", "caption", "photo", "document", "voice", "video",
            "audio", "animation", "sticker", "video_note", "contact",
            "location", "venue", "poll", "dice", "game", "paid_media",
            "story", "checklist",
        )):
            return
        try:
            message_id = int(message.get("message_id") or 0)
        except (TypeError, ValueError):
            return
        if not message_id:
            return
        for emoji in ("⚡", "👍"):
            try:
                await asyncio.wait_for(
                    self.telegram().set_reaction(chat_id, message_id, emoji), 2)
                return
            except Exception:
                LOGGER.debug("Reaction %s failed", emoji, exc_info=True)

    async def _action(
        self,
        message: dict,
        thread: dict | None,
        cmd: str,
        args: str,
        urgent: bool,
        key: str,
    ) -> None:
        chat_id = int(message["chat"]["id"])
        topic_id = int(message.get("message_thread_id") or 0)
        text = (message.get("text") or "").strip()
        lock = asyncio.Lock() if urgent else self.locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                edited_name = (message.get("forum_topic_edited") or {}).get("name")
                if thread and edited_name:
                    thread["name"] = edited_name
                    await self._save(thread)
                    await self._sync_name(thread)
                    return
                if thread and not text.startswith("/"):
                    if await self.ui.answer_message(thread, message, text):
                        return
                    if not text and any(k in message for k in ("photo", "document", "voice", "video")):
                        await self.ui.notice(thread, "Attachments are not forwarded. Send a server file path.", title="Text only")
                        return
                await self._dispatch(chat_id, topic_id, thread, text, cmd, args)
        except Exception as exc:
            LOGGER.exception("Harness action failed")
            label = f"/{cmd}" if text.startswith("/") and cmd else "error"
            try:
                if thread and not thread.get("deleted"):
                    await self.ui.notice(thread, str(exc), title=label)
                else:
                    await self.telegram().send_message(chat_id, notice(label, str(exc)))
            except Exception:
                LOGGER.exception("Could not deliver harness error")

    async def _dispatch(
        self,
        chat_id: int,
        topic_id: int,
        thread: dict | None,
        text: str,
        cmd: str,
        args: str,
    ) -> None:
        if text.startswith("/") and cmd == "opencode":
            body, name = _split_topic_name(args)
            if name:
                raise ValueError("Topic names follow sessions. Use /rename inside the topic.")
            head, _, rest = body.partition(" ")
            action = head.casefold()
            if action in {"resume", "import"}:
                parts = rest.split()
                if len(parts) == 2 and parts[1].startswith("#") and len(parts[1]) > 1:
                    raise ValueError("Topic names follow sessions. Use /rename inside the topic.")
                if len(parts) != 1:
                    raise ValueError(
                        f"Usage: /{cmd} {action} <session-id-or-name>"
                    )
                session = await self._attach(parts[0], fork=action == "import")
                await self._create_attached(chat_id, session)
            else:
                await self.create(chat_id, rest if action == "new" else body)
        elif text.startswith("/") and cmd == "resume" and not thread:
            if args:
                session = await self._attach(args, fork=False)
                await self._create_attached(chat_id, session)
            else:
                root = {
                    "chat_id": chat_id,
                    "topic_id": topic_id,
                    "cwd": self.settings.harness_cwd,
                }
                await self._resume_picker(root, root=True)
        elif thread and not thread.get("deleted"):
            if not text:
                return
            if text.startswith("/"):
                await self._command(thread, cmd, args)
            else:
                await self._prompt(thread, text)

    async def create(
        self,
        chat_id: int,
        prompt: str = "",
        *,
        session: dict | None = None,
    ) -> dict:
        await self._load()
        names = {
            t.get("name")
            for t in self.threads.values()
            if t["chat_id"] == chat_id and not t.get("deleted")
        }
        name = ((session or {}).get("session_name") or "")[:128]
        if not name:
            while not name or name in names:
                name = "".join(
                    secrets.choice(string.ascii_letters + string.digits) for _ in range(5)
                )
        topic = await self.telegram().request(
            "createForumTopic", {"chat_id": chat_id, "name": name}
        )
        thread = {
            "chat_id": chat_id,
            "topic_id": topic["message_thread_id"],
            "name": name,
            "harness": "opencode",
            "cwd": self.settings.harness_cwd,
            "model": "default",
            "effort": "default",
            "status": "starting",
            "session_id": "",
            "ownership": "telegram-worker",
        }
        self.threads[self._key(thread)] = thread
        await self._save(thread)
        try:
            if session:
                thread.update(session)
            else:
                result = await self._oc(thread, "POST", "/session", {})
                thread["session_id"] = result["id"]
                thread["session_name"] = result.get("title")
                result = await self._oc_session(thread)
                model = result.get("model") or {}
                if model:
                    thread["model"] = f"{model['providerID']}/{model['id']}"
                    thread["effort"] = model.get("variant") or "default"
                thread["agent"] = result.get("agent") or "build"
            await self._sync_name(thread, thread.get("session_name") or "")
            thread["status"] = "ready"
            await self._save(thread)
            sent = await self.telegram().send_message(
                chat_id, self._card(thread), message_thread_id=thread["topic_id"],
            )
            thread["pin_id"] = sent["message_id"]
            thread["pin_text"] = self._card(thread)
            thread["pin_ui_version"] = 5
            await self._save(thread)
            try:
                await self.telegram().request(
                    "pinChatMessage",
                    {"chat_id": chat_id, "message_id": sent["message_id"],
                     "disable_notification": True},
                )
            except Exception:
                LOGGER.warning("Topic created but pinning is unavailable")
            if prompt:
                await self.say(thread, prompt)
                await self._prompt(thread, prompt)
        except Exception as exc:
            LOGGER.exception("Could not start harness topic")
            thread["status"] = "error"
            await self._save(thread)
            await self.ui.notice(
                thread,
                f"Could not start: {exc}\nUse /new to retry, or /delete to remove this topic.",
                title="Session unavailable",
            )
        return thread

    def _card(self, t: dict, *, details: bool = False) -> str:
        body = (
            f"Model · {code(t['model'])}\nEffort · {code(t['effort'])}"
            + (f"\nSession · {code(t['session_id'] or 'not created')}"
               f"\nDirectory · {code(t['cwd'])}" if details else "")
        )
        if t.get("session_missing"):
            body += "\nSession unavailable · /resume or /new"
        # Omit the status title so the pinned card contains only settings.
        return f"<blockquote>{body}</blockquote>"

    async def pin(self, thread: dict) -> None:
        async with self.pin_locks.setdefault(self._key(thread), asyncio.Lock()):
            await self._pin(thread)

    async def _pin(self, thread: dict) -> None:
        self.ui.sync_typing(thread)
        await self._save(thread)
        if thread.get("deleted") or not thread.get("pin_id"):
            return
        card = self._card(thread)
        if card == thread.get("pin_text") and thread.get("pin_ui_version") == 5:
            return
        try:
            await self.telegram().edit_message_text(
                thread["chat_id"], thread["pin_id"], card,
                reply_markup={"inline_keyboard": []},
            )
            thread["pin_text"] = card
            thread["pin_ui_version"] = 5
            await self._save(thread)
        except Exception as exc:
            error = str(exc).lower()
            if "message to edit not found" in error:
                thread.pop("pin_id", None)
                thread.pop("pin_text", None)
                await self._save(thread)
            elif "not modified" not in error:
                LOGGER.warning(
                    "Could not refresh harness pin: %s",
                    type(exc).__name__,
                    exc_info=True,
                )

    async def say(self, thread: dict, text: str, *, status: str = "") -> None:
        if thread.get("deleted"):
            return
        text, artifacts, artifact_errors = _extract_artifacts(
            text, str(thread.get("cwd") or "")
        )
        delivery_errors = list(artifact_errors)
        if not text and artifacts and status:
            path, caption = artifacts[0]
            artifact_caption = html.escape(status)
            if caption:
                artifact_caption += "\n" + html.escape(caption)
            await self._send_artifact(thread, path, artifact_caption)
            for path, caption in artifacts[1:]:
                await self._send_artifact(thread, path, html.escape(caption))
            return
        if len(text) > 12000:
            await self.telegram().send_document(
                thread["chat_id"],
                "harness-output.txt",
                text.encode(),
                caption=html.escape(status) if status else "",
                message_thread_id=thread["topic_id"],
            )
        else:
            # Convert each chunk separately so entities never straddle a split.
            chunks = markdown_chunks(text)
            for index, chunk in enumerate(chunks):
                rendered = markdown_to_telegram_html(chunk)
                fallback = chunk
                if status and index == len(chunks) - 1:
                    # The terminal status belongs to this turn's final message.
                    rendered += f"\n\n<blockquote><i>{commands(status)}</i></blockquote>"
                    fallback += f"\n\n{status}"
                try:
                    await self._send_html(thread, rendered, fallback)
                except Exception as exc:
                    if any(
                        value in str(exc).lower()
                        for value in ("thread not found", "topic_deleted", "topic deleted")
                    ):
                        thread["deleted"] = True
                        await self._save(thread)
                        return
                    raise
        for path, caption in artifacts:
            artifact_caption = html.escape(caption)
            if status and caption:
                artifact_caption = f"{artifact_caption}\n{html.escape(status)}"
            elif status:
                artifact_caption = html.escape(status)
            try:
                await self._send_artifact(thread, path, artifact_caption)
            except Exception:
                LOGGER.warning(
                    "Could not deliver harness artifact %s", path, exc_info=True
                )
                delivery_errors.append(f"Could not deliver artifact: {path.name}")
        if delivery_errors:
            await self.ui.notice(
                thread, "\n".join(delivery_errors), title="Artifact issue"
            )

    async def _send_artifact(self, thread: dict, path: Path, caption: str = "") -> None:
        content = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or ""
        # Telegram renders these photo types inline; less common image formats
        # are safer as documents.
        send = (
            self.telegram().send_photo
            if content_type in {"image/jpeg", "image/png", "image/webp"}
            else self.telegram().send_document
        )
        await send(
            thread["chat_id"],
            path.name,
            content,
            caption=caption,
            message_thread_id=thread["topic_id"],
        )

    async def _send_html(self, thread: dict, html_text: str, chunk: str = "") -> None:
        try:
            await self.telegram().send_message(
                thread["chat_id"], html_text, message_thread_id=thread["topic_id"]
            )
        except Exception as exc:
            if "parse entities" in str(exc).lower() or "can't parse" in str(exc).lower():
                # Formatting fallback: deliver the chunk as escaped plain text.
                await self.telegram().send_message(
                    thread["chat_id"],
                    html.escape(chunk or html_text),
                    message_thread_id=thread["topic_id"],
                )
                return
            raise

    async def _attach(self, sid: str, cwd: str = "", *, fork: bool = True) -> dict:
        """Resolve a native session, optionally forking it before attachment."""
        probe = {"cwd": cwd or self.settings.harness_cwd}
        resolved = await self._resolve_oc_session(probe, sid)
        if fork:
            result = await self._oc(
                probe, "POST", f"/session/{quote(resolved, safe='')}/fork", {}
            )
        else:
            result = await self._oc(
                probe, "GET", f"/session/{quote(resolved, safe='')}"
            )
        native = await self._oc_session({**probe, "session_id": result["id"]})
        model = native.get("model") or {}
        return {
            "session_id": result["id"],
            "session_name": result.get("title"),
            "model": f"{model['providerID']}/{model['id']}" if model else "default",
            "effort": model.get("variant") or "default",
            "agent": native.get("agent") or "build",
            "cwd": result.get("directory", probe["cwd"]),
        }

    async def _resolve_oc_session(self, probe: dict, key: str) -> str:
        """Accept a session id or a session title (exact, then substring)."""
        if key.startswith("ses_"):
            return key
        try:
            await self._oc(probe, "GET", f"/session/{quote(key, safe='')}")
            return key
        except Exception:
            pass
        candidates = await self._oc_sessions(probe)

        wanted = key.casefold()
        exact = [
            value for value in candidates if str(value["title"]).casefold() == wanted
        ]
        fuzzy = [
            value
            for value in candidates
            if wanted in str(value["title"]).casefold()
        ]
        for pool in (exact, fuzzy):
            if pool:
                return pool[0]["id"]
        raise ValueError(f"No OpenCode session matches '{key}'.")

    async def _oc_sessions(self, probe: dict, *, unattached: bool = False) -> list[dict]:
        listed = await self._oc(probe, "GET", "/session")
        items = listed if isinstance(listed, list) else listed.get("data") or []
        attached = {
            thread.get("session_id")
            for thread in (self.threads or {}).values()
            if not thread.get("deleted")
        } if unattached else set()
        sessions = [
            value
            for value in items
            if isinstance(value, dict)
            and value.get("id")
            and (not unattached or value["id"] not in attached)
        ]

        def updated(value: dict) -> int:
            timestamp = value.get("time") or {}
            return int(timestamp.get("updated") or 0) if isinstance(timestamp, dict) else 0

        return sorted(sessions, key=updated, reverse=True)

    async def _resume_picker(self, thread: dict, *, page: int = 0, root: bool = False) -> None:
        sessions = await self._oc_sessions(thread, unattached=True)
        await self.ui.session_picker(thread, sessions, page, root=root)

    async def _resume_from_picker(self, thread: dict, session_id: str) -> None:
        session = await self._attach(session_id, thread["cwd"], fork=False)
        await self._create_attached(thread["chat_id"], session)

    async def _oc_session(self, thread: dict) -> dict:
        return (await self._oc(thread, "GET", f"/api/session/{thread['session_id']}"))[
            "data"
        ]

    async def _provider_catalog(self, thread: dict) -> dict:
        """Fetch provider metadata with a short retry for transient catalog gaps."""
        providers = {}
        for attempt in range(3):
            providers = await self._oc(thread, "GET", "/provider")
            if providers.get("all") or attempt == 2:
                return providers
            await asyncio.sleep(0.25)
        return providers

    async def _effort_levels(self, thread: dict) -> list[str]:
        """Return supported efforts, retrying a transient empty variant list."""
        def lookup(providers: dict) -> tuple[dict, list[str]]:
            provider, _, mid = thread["model"].partition("/")
            model = next(
                (
                    p.get("models", {}).get(mid, {})
                    for p in providers.get("all", [])
                    if p.get("id") == provider
                ),
                {},
            )
            return model, list(model.get("variants", {}))

        providers = await self._provider_catalog(thread)
        _, levels = lookup(providers)
        if not levels:
            await asyncio.sleep(0.25)
            providers = await self._provider_catalog(thread)
            _, levels = lookup(providers)
        return levels

    async def _oc(self, thread: dict, method: str, path: str, body: dict | None = None):
        try:
            return await self.worker.opencode(thread, method, path, body)
        except OpenCodeError as exc:
            if exc.code == "session_missing" and thread.get("topic_id"):
                await self._mark_session_missing(thread)
            raise

    async def _usage(self, thread: dict) -> None:
        """Show the usage tracker's full all-providers view."""
        response = await self._oc(
            thread,
            "POST",
            usage_rpc_path(),
            {"input": {"provider": ALL_PROVIDERS}},
        )
        result = response.get("output") if isinstance(response, dict) else response
        await self.ui.output(thread, format_usage(result), command="usage")

    async def _steer(self, thread: dict, text: str) -> None:
        """Send guidance into an already-active OpenCode run."""
        key = self._key(thread)
        job = self.jobs.get(key)
        if job and not job.done():
            state = "working"
        else:
            await self._check_requests(thread)
            state = await self._native_run_state(thread)
        if state != "working":
            raise ValueError(f"Cannot steer while OpenCode is {state}.")
        await self._oc(
            thread,
            "POST",
            f"/session/{thread['session_id']}/prompt",
            {"text": text, "delivery": "steer"},
        )
        await self.ui.notice(thread, "steered", title="/steer")

    async def _prompt(self, thread: dict, text: str, *, slash: str = "") -> None:
        """Admit a prompt to OpenCode's native durable queue."""
        if not thread["session_id"]:
            raise ValueError("This topic has no session. Use /new to start one.")
        if thread.get("session_missing"):
            raise ValueError("This session no longer exists. Use /resume to attach a saved session or /new to start one.")
        await self._claim_legacy_opencode(thread)
        anchor = thread.get("turn_anchor")
        new_anchor = not anchor
        if not anchor:
            previous = await self._oc(
                thread, "GET", f"/session/{thread['session_id']}/message"
            )
            anchor = (
                (previous[-1].get("info") or {}).get("id") if previous else "start"
            )

        body: dict[str, Any] = {"delivery": "queue"}
        if slash:
            body.update(command=slash, arguments=text)
        else:
            body["parts"] = [{"type": "text", "text": _harness_context(thread) + text}]
        try:
            await self.telegram().send_chat_action(
                thread["chat_id"], message_thread_id=thread["topic_id"]
            )
        except Exception:
            LOGGER.debug("Could not send typing indicator", exc_info=True)
        try:
            self.admissions.add(self._key(thread))
            if new_anchor:
                thread["turn_anchor"] = anchor
                await self._save(thread)
            await self._oc(thread, "POST", f"/session/{thread['session_id']}/admit", body)
        except OpenCodeError as exc:
            if exc.code == "unavailable":
                thread["status"] = "checking OpenCode"
                await self._save(thread)
                self.admissions.discard(self._key(thread))
                await self._ensure_recovery(thread)
            elif new_anchor and thread.get("turn_anchor") == anchor:
                thread.pop("turn_anchor", None)
                await self._save(thread)
            raise
        finally:
            self.admissions.discard(self._key(thread))
        thread["status"] = "working"
        await self._save(thread)
        await self.pin(thread)
        await self._ensure_recovery(thread)

    async def _claim_legacy_opencode(self, thread: dict) -> None:
        if thread.get("ownership") == "telegram-worker":
            return
        result = await self._oc(
            thread,
            "POST",
            f"/session/{quote(thread['session_id'], safe='')}/fork",
            {},
        )
        thread["session_id"] = result["id"]
        thread["session_name"] = result.get("title")
        thread["cwd"] = result.get("directory", thread["cwd"])
        native = await self._oc_session(thread)
        model = native.get("model") or {}
        if model:
            thread["model"] = f"{model['providerID']}/{model['id']}"
            thread["effort"] = model.get("variant") or "default"
        thread["agent"] = native.get("agent") or thread.get("agent", "build")
        thread["ownership"] = "telegram-worker"
        await self._save(thread)
        await self._sync_name(thread, thread.get("session_name") or "")

    async def _clear_topic_history(self, thread: dict) -> None:
        """Delete and recreate the Telegram topic, preserving its session."""
        key = self._key(thread)
        job = self.jobs.get(key)
        if job and not job.done():
            raise ValueError("Interrupt the active run before clearing this topic.")
        if any(self._key(owner) == key for owner, _ in self.requests.values()):
            raise ValueError("Answer the pending OpenCode request before clearing this topic.")

        chat_id = thread["chat_id"]
        old_topic_id = thread["topic_id"]
        name = " ".join(str(
            thread.get("name") or thread.get("session_name") or "OpenCode"
        ).split())[:128] or "OpenCode"

        # Create the replacement before deleting history. A rejected creation
        # leaves the original topic and its durable ownership usable.
        created = await self.telegram().request("createForumTopic", {
            "chat_id": chat_id,
            "name": name,
        })
        replacement_id = int(created["message_thread_id"])
        try:
            await self.telegram().request("deleteForumTopic", {
                "chat_id": chat_id,
                "message_thread_id": old_topic_id,
            })
        except Exception as exc:
            if "topic_id_invalid" not in str(exc).lower() and "topic deleted" not in str(exc).lower():
                try:
                    await self.telegram().request("deleteForumTopic", {
                        "chat_id": chat_id, "message_thread_id": replacement_id,
                    })
                except Exception:
                    LOGGER.warning("Could not remove unused replacement topic", exc_info=True)
                raise

        # Remove stale panels/tokens tied to the deleted topic.
        for token, choice in list(self.ui.choices.items()):
            if choice.get("key") == key:
                self.ui.choices.pop(token, None)
        for token, (owner, _) in list(self.requests.items()):
            if self._key(owner) == key:
                self.requests.pop(token, None)

        thread["topic_id"] = replacement_id
        thread.pop("pin_id", None)
        thread.pop("pin_text", None)
        if thread.get("status") in {"checking OpenCode", "disconnected; checking session"}:
            thread["status"] = "ready"

        self.threads.pop(key, None)
        self.threads[self._key(thread)] = thread
        await self.store.replace_harness_thread(key, self._key(thread), thread)

        sent = await self.telegram().send_message(
            chat_id, self._card(thread), message_thread_id=thread["topic_id"]
        )
        thread["pin_id"] = int(sent.get("message_id"))
        thread["pin_text"] = self._card(thread)
        await self._save(thread)
        try:
            await self.telegram().request("pinChatMessage", {
                "chat_id": chat_id,
                "message_id": thread["pin_id"],
                "disable_notification": True,
            })
        except Exception:
            LOGGER.warning("Could not pin recreated harness topic", exc_info=True)

    async def _command(self, t: dict, cmd: str, arg: str) -> None:
        if cmd not in ALLOWED_COMMANDS:
            raise ValueError(f"/{cmd} is not an allowed OpenCode command. Use /help.")
        sid = t["session_id"]
        if cmd in {"help", "commands"}:
            await self.ui.help(t, arg if cmd == "commands" else "")
        elif cmd == "delete":
            # Deleting the native session also deletes its native inbox.
            key = self._key(t)
            job = self.jobs.pop(key, None)
            if job:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
            for token, (owner, _) in list(self.requests.items()):
                if self._key(owner) == key:
                    self.requests.pop(token, None)
            try:
                await self._oc(
                    t, "DELETE", f"/session/{quote(sid, safe='')}"
                )
            except Exception as exc:
                # A previously removed native session is already in the
                # desired state; anything else should stop the topic delete.
                if not _session_missing(exc):
                    raise
            await self.telegram().request(
                "deleteForumTopic",
                {"chat_id": t["chat_id"], "message_thread_id": t["topic_id"]},
            )
            t["deleted"] = True
            self.recovery_retry_after.pop(key, None)
            await self.pin(t)
        elif cmd == "clear":
            await self._clear_topic_history(t)
            return
        elif cmd in {"new"}:
            body, name = _split_topic_name(arg)
            if name:
                raise ValueError("Topic names follow sessions. Use /rename inside the topic.")
            await self.create(t["chat_id"], body)
        elif cmd in {"approve", "deny", "answer"}:
            await self._reply(t, cmd, arg)
        elif cmd in {"stop", "interrupt"}:
            key = self._key(t)
            t["status"] = "stopping"
            await self.pin(t)
            for item in await self._native_inbox(t):
                inbox_id = str(item.get("id") or "")
                if inbox_id:
                    await self._oc(
                        t, "DELETE", f"/api/session/{sid}/inbox/{quote(inbox_id, safe='')}"
                    )
            await self._oc(t, "POST", f"/session/{sid}/abort", {})
            job = self.jobs.pop(key, None)
            if job:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
            t["status"] = "interrupted"
            t.pop("turn_anchor", None)
            self.recovery_retry_after.pop(self._key(t), None)
            for token, (owner, request) in list(self.requests.items()):
                if self._key(owner) == self._key(t):
                    self.requests.pop(token, None)
                    await self.ui.resolved(t, request)
            await self.ui.notice(t, "interrupted", title="opencode")
            await self.pin(t)
        elif cmd == "steer":
            if not arg:
                raise ValueError("Usage: /steer <prompt>")
            await self._steer(t, arg)
        elif cmd == "queue":
            inbox = await self._native_inbox(t)
            if arg == "clear":
                for item in inbox:
                    inbox_id = str(item.get("id") or "")
                    if inbox_id:
                        await self._oc(
                            t, "DELETE",
                            f"/api/session/{sid}/inbox/{quote(inbox_id, safe='')}",
                        )
                await self.ui.notice(
                    t,
                    "Native queued prompts cleared. The active run continues.",
                    title="/queue",
                )
            elif arg == "run":
                raise ValueError("OpenCode drains its native queue automatically.")
            elif arg:
                raise ValueError("Usage: /queue [clear]")
            else:
                await self._check_requests(t)
                state = await self._native_run_state(t)
                active = await self._native_active_prompt(t, state)
                lines = []
                if active:
                    label = "Waiting for input" if state == "waiting for input" else "Running"
                    lines.append(f"{label} · {active[:160]}")
                lines.extend(
                    f"{index}. {self._native_prompt_label(t, (item.get('payload') or {}).get('text'))}"
                    for index, item in enumerate(inbox, 1)
                )
                summary = (
                    f"{len(inbox)} prompt{'s' if len(inbox) != 1 else ''} waiting"
                    if inbox else "No prompts are waiting"
                )
                lines.append(summary)
                await self.ui.notice(t, "\n".join(lines), title="/queue")
        elif cmd == "usage":
            if arg:
                raise ValueError("Usage: /usage")
            await self._usage(t)
        elif cmd in {"model", "models", "effort", "agent", "agents"}:
            await self._settings(t, cmd, arg)
        elif cmd == "status":
            native = await self._oc_session(t)
            model = native.get("model") or {}
            if model:
                t.update(
                    model=f"{model['providerID']}/{model['id']}",
                    effort=model.get("variant") or "default",
                )
            await self.pin(t)
            await self.telegram().send_message(
                t["chat_id"], self._card(t, details=True),
                message_thread_id=t["topic_id"],
            )
        elif cmd in {"resume", "import", "fork"}:
            if cmd in {"resume", "import"}:
                if not arg:
                    if cmd == "resume":
                        await self._resume_picker(t)
                        return
                    raise ValueError("Usage: /import <session-id-or-name>")
                session = await self._attach(arg, t["cwd"], fork=cmd == "import")
            else:
                result = await self._oc(
                    t,
                    "POST",
                    f"/session/{quote(arg or sid, safe='')}/fork",
                    {},
                )
                native = await self._oc_session({**t, "session_id": result["id"]})
                model = native.get("model") or {}
                session = {
                    "session_id": result["id"],
                    "session_name": result.get("title"),
                    "model": f"{model['providerID']}/{model['id']}"
                    if model
                    else "default",
                    "effort": model.get("variant") or "default",
                    "agent": native.get("agent") or "build",
                    "cwd": result.get("directory", t["cwd"]),
                }
            await self._create_attached(t["chat_id"], session)
        elif cmd == "rename":
            if not arg:
                raise ValueError("Usage: /rename session title")
            title = arg.strip()
            async with self.name_locks.setdefault(self._key(t), asyncio.Lock()):
                await self._oc(
                    t, "PATCH", f"/api/session/{sid}", {"title": title}
                )
                native = await self._oc_session(t)
                native_title = native.get("title")
                if native_title != title:
                    raise ValueError("OpenCode did not apply the session rename.")
                t["session_name"] = native_title
                try:
                    renamed = await self._set_topic_name(t, title)
                except Exception:
                    await self.ui.notice(t, "Topic sync will retry.", title="/rename")
                    return
            if not renamed:
                return
            await self.ui.notice(t, title, title="/rename")
        else:
            await self._oc_command(t, cmd, arg)

    async def _create_attached(self, chat_id: int, session: dict) -> None:
        async with self.attachment_lock:
            await self._load()
            await self._create_attached_once(chat_id, session)

    async def _create_attached_once(self, chat_id: int, session: dict) -> None:
        for existing in self.threads.values():
            if existing.get("session_id") != session["session_id"] or existing.get(
                "deleted"
            ):
                continue
            try:
                await self.telegram().request(
                    "sendChatAction",
                    {
                        "chat_id": existing["chat_id"],
                        "message_thread_id": existing["topic_id"],
                        "action": "typing",
                    },
                )
            except RuntimeError as exc:
                if (
                    "thread not found" not in str(exc).lower()
                    and "topic_deleted" not in str(exc).lower()
                ):
                    raise
                existing["deleted"] = True
                await self._save(existing)
            else:
                raise ValueError(
                    "That session already has an attached topic: " + existing["name"]
                )
        await self.create(chat_id, session=session)

    async def _settings(self, t: dict, cmd: str, arg: str, *, page: int = 0) -> None:
        selection = dict(t)
        if arg and (
            t.get("turn_id")
            or (
                self._key(t) in self.jobs
                and not self.jobs[self._key(t)].done()
            )
        ):
            raise ValueError("Interrupt the run before changing settings.")
        if arg:
            await self._check_requests(t)
            native_state = await self._native_run_state(t)
            if native_state in {"working", "waiting for input"}:
                raise ValueError("Interrupt the run before changing settings.")
        if cmd in {"model", "models"}:
            providers = await self._provider_catalog(t)
            choices = {
                f"{p['id']}/{mid}": model
                for p in providers["all"]
                if p["id"] != "opencode"
                and p["id"] in providers["connected"]
                for mid, model in p["models"].items()
            }
            if not arg:
                await self.ui.picker(t, "model", choices, page)
                return
            if arg not in choices:
                raise ValueError("Unknown model. Use /models for available names.")
            selection["model"] = arg
            selection["effort"] = "default"
        elif cmd == "effort":
            levels = await self._effort_levels(t)
            if not arg:
                await self.ui.picker(t, "effort", dict.fromkeys(["default", *levels]), page)
                return
            if arg not in ["default", *levels]:
                raise ValueError(
                    "Unsupported effort. Use /effort to list supported values."
                )
            selection["effort"] = arg
        else:
            agents = await self._oc(t, "GET", "/agent")
            names = [a["name"] for a in agents if not a.get("hidden")]
            if not arg:
                await self.ui.picker(t, "agent", names, page)
                return
            if arg not in names:
                raise ValueError("Unknown agent: " + arg)
            selection["agent"] = arg
        if cmd in {"model", "models", "effort"}:
            provider, _, mid = selection["model"].partition("/")
            model = {"providerID": provider, "id": mid}
            if selection["effort"] != "default":
                model["variant"] = selection["effort"]
            await self._oc(
                t, "POST", f"/api/session/{t['session_id']}/model", {"model": model}
            )
        else:
            await self._oc(
                t,
                "POST",
                f"/api/session/{t['session_id']}/agent",
                {"agent": selection["agent"]},
            )
        for field in ("model", "effort", "agent"):
            if field in selection:
                t[field] = selection[field]
        await self._save(t)
        await self.pin(t)
        label = {"models": "model", "agents": "agent"}.get(cmd, cmd)
        await self.ui.notice(t, arg, title=f"/{label}")

    async def _oc_command(self, t: dict, cmd: str, arg: str) -> None:
        sid = t["session_id"]
        base = f"/session/{sid}"
        if cmd in {"compact", "summarize"}:
            provider, _, model = t["model"].partition("/")
            result = await self._oc(
                t,
                "POST",
                base + "/summarize",
                {"providerID": provider, "modelID": model},
            )
        elif cmd in {"diff", "sessions", "export", "redo"}:
            method, path = {
                "diff": ("GET", base + "/diff"),
                "sessions": ("GET", "/session"),
                "export": ("GET", base + "/message"),
                "redo": ("POST", base + "/unrevert"),
            }[cmd]
            result = await self._oc(t, method, path, {} if method == "POST" else None)
        elif cmd == "undo":
            messages = await self._oc(t, "GET", base + "/message")
            latest = next(
                (m for m in reversed(messages) if m["info"]["role"] == "user"), None
            )
            if not latest:
                raise ValueError("No user turn to undo.")
            result = await self._oc(
                t, "POST", base + "/revert", {"messageID": latest["info"]["id"]}
            )
        else:
            raise ValueError(f"/{cmd} is not an allowed OpenCode command. Use /help.")
        await self.ui.output(
            t,
            json.dumps(result, ensure_ascii=False, indent=2),
            command=cmd,
        )

    async def _reply(self, t: dict, cmd: str, arg: str,
                     *, selected: list[str] | None = None) -> None:
        # Buttons and slash replies can arrive together. Only one native answer wins.
        async with self.reply_locks.setdefault(self._key(t), asyncio.Lock()):
            await self._reply_once(t, cmd, arg, selected=selected)

    async def _reply_once(self, t: dict, cmd: str, arg: str,
                          *, selected: list[str] | None = None) -> None:
        token, _, answer = arg.partition(" ")
        pending = self.requests.get(token)
        if not pending or self._key(pending[0]) != self._key(t):
            raise ValueError("Request not found in this topic, or it expired.")
        _, request = pending
        if request["kind"] == "permission":
            if cmd not in {"approve", "deny"}:
                raise ValueError("Use /approve or /deny for permission requests.")
            await self._oc(
                t,
                "POST",
                f"/permission/{request['id']}/reply",
                {"reply": "once" if cmd == "approve" else "reject"},
            )
        else:
            if cmd != "answer":
                raise ValueError("Use /answer for this question.")
            if selected is not None:
                if len(request["questions"]) != 1 or not request["questions"][0].get("multiple"):
                    raise ValueError("This is not a multi-select question.")
                answers = [selected]
            else:
                answers = ([[answer.strip()]] if len(request["questions"]) == 1
                           else [[x.strip()] for x in answer.split("|")])
            if len(answers) != len(request["questions"]):
                raise ValueError("Separate one answer per question with |.")
            await self._oc(
                t, "POST", f"/question/{request['id']}/reply",
                {"answers": answers, "exact": selected is not None},
            )
        self.resolved_requests.add((self._key(t), request["kind"], request["id"]))
        self.requests.pop(token, None)
        await self.ui.resolved(t, request)
        await self.ui.notice(t, "Sent.", title=f"/{cmd}")
