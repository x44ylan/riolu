#!/usr/bin/env python3
"""Exercise Riolu through local HTTP integrations and Telegram updates.

Failure surfaces: unknown/unauthorized chats, malformed integration payloads,
concurrent duplicate deliveries, interrupted multi-chat question delivery,
question retries after restart, invalid or unrelated answer buttons, source
fetch failures, duplicate source items, partial digest delivery, checkpoint
retention, note persistence, failed clear operations, and callback navigation.
Also check corrupt-state preservation and an accepted Telegram write whose
response is lost, so retry behavior cannot silently duplicate user messages.
MCP checks cover endpoint routing, notifications, questions, Markdown files,
artifact cleanup after success, and retention after failed delivery. Host
checks execute read-only inspection and verify its Telegram menu callbacks.
Scheduling checks cover catch-up, completed slots, failed-delivery checkpoints,
and daylight-saving transitions with a controlled clock and real HTTP delivery.
Long-line rendering runs in bounded subprocesses to catch event-loop stalls
without hanging the remaining checks.
Migration failures: lost notes/checkpoints/dedupe after restart, duplicate
destinations, stale message namespaces, cyclic mappings, unauthorized migration
claims, and privileged coding access inherited from a general chat allowlist.
Source failures: aliases selecting the wrong view, option refresh losing mode,
category limits, silent missing configured IDs, normalized configuration IDs,
and duplicate defaults. Telegram boundaries include explicit rate limits and
harmless repeated edits; artifacts include sensitive aliases and invalid sizes.
All Telegram traffic and source traffic stays on disposable loopback servers.
No production settings, state, Telegram chats, or OpenCode sessions are used.
Run with Riolu's Python environment; RIOLU_E2E_REPORT selects the JSON artifact.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from bs4 import BeautifulSoup

from riolu.bot import RioluBot
from riolu import bot as bot_module
from riolu.config import Settings
from riolu.source_registry import SourceRegistry
from riolu.telegram import TelegramAPI
from riolu.webhook import HookServer


class LocalTelegram(TelegramAPI):
    def _url(self, method):
        return self.token + "/telegram/" + method


class Services:
    def __init__(self):
        self.sent = []
        self.calls = []
        self.fail_chats = set()
        self.fail_methods = set()
        self.fail_text = set()
        self.fail_sources = set()
        self.drop_responses = set()
        self.migrations = {}
        self.rate_limits = {}
        self.unchanged_edits = False
        self.topics = set()
        self.native_handler = None
        self.feeds = {"first": [], "second": []}
        self.rss = ""
        self.counter = 100

    async def start(self):
        self.server = await asyncio.start_server(self.client, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def client(self, reader, writer):
        try:
            method, target, _ = (await reader.readline()).decode().split()
            headers = {}
            while line := await reader.readline():
                if line == b"\r\n":
                    break
                name, _, value = line.decode().partition(":")
                headers[name.lower()] = value.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            status = 200
            content_type = "application/json"
            if target.startswith("/rss/"):
                content_type, result = "application/xml", self.rss
            elif target.startswith("/news/"):
                source = target.rsplit("/", 1)[-1]
                status = 503 if source in self.fail_sources else 200
                result = self.feeds[source]
            elif target.startswith("/api/") and self.native_handler is not None:
                status, result = await self.native_handler(method, target.split("?", 1)[0], json.loads(body) if body else {})
            else:
                action = target.rsplit("/", 1)[-1]
                if headers.get("content-type", "").startswith("application/json"):
                    payload = json.loads(body)
                else:
                    match = re.search(rb'name="chat_id"\r\n\r\n(-?\d+)', body)
                    payload = {"chat_id": int(match[1]) if match else 0}
                self.calls.append((action, payload))
                await asyncio.sleep(0.01)
                rejected = action in self.fail_methods or (
                    action in {"sendMessage", "sendDocument"}
                    and (payload.get("chat_id") in self.fail_chats
                         or any(text in payload.get("text", "") for text in self.fail_text))
                )
                rate_limit = self.rate_limits.get(action, 0)
                if rate_limit:
                    self.rate_limits[action] -= 1
                    status, result = 429, {"ok": False, "description": "Too Many Requests",
                                          "parameters": {"retry_after": 0}}
                elif self.unchanged_edits and action in {"editMessageText", "editMessageReplyMarkup"}:
                    status, result = 400, {"ok": False, "description": "Bad Request: message is not modified"}
                elif payload.get("chat_id") in self.migrations:
                    status, result = 400, {"ok": False, "description": "chat was upgraded",
                                          "parameters": {"migrate_to_chat_id": self.migrations[payload["chat_id"]]}}
                elif rejected:
                    status, result = 400, {"ok": False, "description": "fixture delivery rejected"}
                else:
                    self.counter += 1
                    if action in {"sendMessage", "sendDocument"}:
                        self.sent.append((action, {**payload, "message_id": self.counter}))
                    if action in self.drop_responses:
                        return
                    if action == "createForumTopic":
                        self.topics.add((payload["chat_id"], self.counter))
                    elif action == "deleteForumTopic":
                        self.topics.discard((payload["chat_id"], payload["message_thread_id"]))
                    result = {"ok": True, "result": ({"message_thread_id": self.counter, "name": payload["name"]}
                                                      if action == "createForumTopic" else {"message_id": self.counter})}
            data = result.encode() if content_type == "application/xml" else json.dumps(result).encode()
            writer.write(f"HTTP/1.1 {status} Response\r\nContent-Type: {content_type}\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


def stories(count, *, prefix="story", summary=""):
    return [{"title": f"{prefix}-{index:03}", "url": f"https://example.test/{prefix}-{index:03}", "summary": summary}
            for index in range(count)]


async def main():
    report = {"checks": {}, "failures": {}, "external_messages_sent": 0}
    services = Services()
    await services.start()
    with tempfile.TemporaryDirectory(prefix="riolu-e2e-") as root:
        plugins = Path(root) / "sources"
        plugins.mkdir()
        for source_id in ("first", "second"):
            (plugins / f"{source_id}.py").write_text(
                "from riolu.models import IntelItem\n"
                "class FixtureSource:\n"
                f"    id = {source_id!r}\n"
                f"    name = {source_id.title()!r}\n"
                "    category = 'news'\n    description = 'E2E source'\n    aliases = ()\n"
                "    async def fetch(self, limit, client):\n"
                f"        response = await client.get({(services.url + '/news/' + source_id)!r})\n"
                "        response.raise_for_status()\n"
                "        return [IntelItem(source_id=self.id, source_name=self.name, **item) for item in response.json()[:limit]]\n"
                "SOURCE = FixtureSource()\n"
            )
        registry = SourceRegistry.load(str(plugins))
        async with httpx.AsyncClient() as http:
            counter = 0

            def bot(*, settings=None, sources=None):
                nonlocal counter
                counter += 1
                if settings is None:
                    settings = Settings("unused", frozenset({1, 2}), None, 5, 100,
                                        str(Path(root) / f"state-{counter}.json"), 20,
                                        tuple(s.id for s in (sources or registry).all()),
                                        automated_source_ids=tuple(s.id for s in (sources or registry).all()))
                value = RioluBot(settings, registry=sources or registry)
                value.http = http
                value.telegram = LocalTelegram(services.url, http)
                return value

            async def check(name, run):
                services.fail_chats.clear()
                services.fail_methods.clear()
                services.fail_text.clear()
                services.fail_sources.clear()
                services.drop_responses.clear()
                services.migrations.clear()
                services.rate_limits.clear()
                services.unchanged_edits = False
                services.feeds = {"first": [], "second": []}
                services.sent.clear()
                services.calls.clear()
                try:
                    await run()
                except Exception as exc:
                    report["checks"][name] = False
                    report["failures"][name] = f"{type(exc).__name__}: {exc}"
                else:
                    report["checks"][name] = True

            async def update(value, text, *, chat=1, reply_to=None):
                message = {"chat": {"id": chat}, "message_id": 50, "text": text}
                if reply_to:
                    message["reply_to_message"] = {"message_id": reply_to}
                await value._handle_update({"message": message})

            async def callback(value, data, *, chat=1, message_id=101):
                await value._handle_update({"callback_query": {
                    "id": "fixture-callback", "data": data,
                    "message": {"chat": {"id": chat}, "message_id": message_id},
                }})

            async def integration(run):
                value = bot()
                await value.store.remember_chat(1)
                await value.store.remember_chat(2)
                server = HookServer("127.0.0.1", 0, "hook-test", value._handle_hook,
                                    message_token="agent-test", message_handler=value._handle_agent_message,
                                    agent_handler=value._handle_agent_action)
                await server.start()
                async def post(payload, path="/agent", *, token="agent-test"):
                    return await http.post(f"http://127.0.0.1:{server.port}" + path,
                                           json=payload, headers={"Authorization": f"Bearer {token}"})
                try:
                    await run(value, post)
                finally:
                    await server.close()

            async def note_flow():
                value = bot()
                await update(value, "/note")
                await update(value, "Body <&>\nSecond line")
                await update(value, "Personal note")
                saved = await value.store.notes_for_chat(1)
                assert len(saved) == 1 and saved[0]["body"] == "Body <&>\nSecond line"
                reopened = bot(settings=value.settings)
                await callback(reopened, f"note:open:{saved[0]['id']}")
                assert "&lt;&amp;&gt;" in services.calls[-1][1]["text"]
                await callback(reopened, f"note:delete:{saved[0]['id']}", chat=2)
                assert len(await reopened.store.notes_for_chat(1)) == 1
                await callback(reopened, f"note:delete:{saved[0]['id']}")
                assert await reopened.store.notes_for_chat(1) == []

            await check("notes_update_callback_restart_and_chat_isolation", note_flow)

            async def news_navigation():
                value = bot()
                services.feeds["first"] = stories(20)
                await update(value, "/first 20")
                await callback(value, "run:first:20:1")
                text = services.calls[-1][1]["text"]
                assert "story-010" in text and "story-000" not in text
                await callback(value, "run:first:20:0", chat=999)
                assert services.calls[-1][0] == "answerCallbackQuery" and services.calls[-1][1]["show_alert"]

            await check("source_plugin_fetch_pagination_and_callback_authorization", news_navigation)

            async def question_retry(value, post):
                question = {"action": "ask_user", "request_id": "ask_retry_test", "question": "Choose?", "choices": ["Yes", "No"]}
                services.fail_chats.add(2)
                assert (await post(question)).status_code == 502
                services.fail_chats.clear()
                # Reopen durable state to prove failed recipients survive restart.
                value.store = type(value.store)(value.settings.state_path)
                reply = await post(question)
                assert reply.status_code == 200 and reply.json()["delivered"] == 1 and reply.json()["skipped"] == 1
                sent = [payload["chat_id"] for action, payload in services.sent if action == "sendMessage"]
                assert sent == [1, 2], f"question recipients: {sent}"
                prompt = services.sent[-1][1]
                await callback(value, "agent_answer:ask_retry_test:0", chat=2, message_id=prompt["message_id"])
                result = (await post({"action": "check_reply", "request_id": question["request_id"]})).json()
                assert result["status"] == "answered" and result["answer"] == "Yes"

            await check("question_partial_delivery_retry_and_answer", lambda: integration(question_retry))

            async def bad_question_buttons(value, post):
                question = {"action": "ask_user", "request_id": "ask_button_test", "question": "Choose?", "choices": ["Yes", "No"]}
                assert (await post(question)).status_code == 200
                prompt = services.sent[0][1]
                await callback(value, "agent_answer:ask_button_test:-1", message_id=prompt["message_id"])
                assert (await value.store.agent_request(question["request_id"]))["status"] == "pending", "negative index answered a question"
                await callback(value, "agent_answer:ask_button_test:0", message_id=999)
                assert (await value.store.agent_request(question["request_id"]))["status"] == "pending", "unrelated message answered a question"

            await check("question_buttons_require_valid_index_and_matching_prompt", lambda: integration(bad_question_buttons))

            async def typed_question(value, post):
                question = {"action": "ask_user", "request_id": "ask_typed_test", "question": "What next?"}
                assert (await post(question)).status_code == 200
                await update(value, "A typed reply", reply_to=services.sent[0][1]["message_id"])
                assert (await value.store.agent_request(question["request_id"]))["answer"] == "A typed reply"

            await check("question_typed_reply", lambda: integration(typed_question))

            async def concurrent_delivery(value, post, *, artifact=False):
                payload = ({"action": "send_artifact", "filename": "result.txt", "content_base64": base64.b64encode(b"result").decode(), "dedupe_key": "same-artifact"}
                           if artifact else {"message": "One update", "dedupe_key": "same-update"})
                responses = await asyncio.gather(post(payload, "/agent" if artifact else "/message"),
                                                 post(payload, "/agent" if artifact else "/message"))
                assert all(response.status_code == 200 for response in responses)
                delivered = sum(response.json()["delivered"] for response in responses)
                assert delivered == 2 and len(services.sent) == 2, f"concurrent deliveries: {delivered}"

            await check("concurrent_notifications_deduplicate_per_chat", lambda: integration(concurrent_delivery))
            await check("concurrent_artifacts_deduplicate_per_chat", lambda: integration(lambda value, post: concurrent_delivery(value, post, artifact=True)))

            async def malformed(value, post):
                assert (await post({}, token="wrong")).status_code == 401
                assert (await post([])).status_code == 400
                invalid_event = {"id": "event", "type": {"bad": "type"}, "ctf": "fixture", "challenge": "fixture", "status": "blocked"}
                assert (await post(invalid_event, "/hook", token="hook-test")).status_code == 400

            await check("integration_auth_and_malformed_payloads", lambda: integration(malformed))

            async def duplicate_items():
                value = bot()
                services.feeds["first"] = stories(1) * 2
                await value._post_new_items(1, (registry.get("first"),))
                text = "\n".join(payload["text"] for _, payload in services.sent)
                assert text.count("story-000") == 1, "source duplicate appeared twice"

            await check("digest_deduplicates_source_items", duplicate_items)

            async def retention():
                value = bot()
                await value.store.mark_seen(1, "first", [f"https://example.test/z-{index:04}" for index in range(1000)])
                services.feeds["first"] = stories(1, prefix="a-new")
                await value._post_new_items(1, (registry.get("first"),))
                reopened = bot(settings=value.settings)
                await reopened._post_new_items(1, (registry.get("first"),))
                assert len(services.sent) == 1, "new checkpoint was evicted by URL sorting"
                assert len(await reopened.store.seen_urls(1, "first")) == 1000

            await check("digest_retains_newest_checkpoints_after_restart", retention)

            async def failed_source():
                value = bot()
                services.fail_sources.add("first")
                services.feeds["second"] = stories(1)
                failed = False
                try:
                    await value._post_periodic_updates()
                except RuntimeError:
                    failed = True
                # Known recipients are required when no explicit destination exists.
                assert not services.sent
                await value.store.remember_chat(1)
                try:
                    await value._post_periodic_updates()
                except RuntimeError:
                    failed = True
                assert failed, "failed digest was reported as successful"
                assert len(services.sent) == 1, "healthy source was starved"
                services.fail_sources.clear()
                services.feeds["first"] = stories(1, prefix="recovered")
                await value._post_periodic_updates()
                assert len(services.sent) == 2, "retry duplicated the healthy source"

            await check("failed_digest_retries_without_starving_or_duplicating_sources", failed_source)

            async def failed_delivery():
                value = bot()
                await value.store.remember_chat(1)
                services.feeds = {"first": stories(1), "second": stories(1, prefix="other")}
                services.fail_text.add("New Updates - First")
                failed = False
                try:
                    await value._post_periodic_updates()
                except RuntimeError:
                    failed = True
                assert failed and len(services.sent) == 1 and "other-000" in services.sent[0][1]["text"], "delivery failure starved another source or was swallowed"

            await check("digest_delivery_failure_isolated_and_reported", failed_delivery)

            async def partial_digest():
                value = bot()
                services.feeds["first"] = stories(20, summary="Long summary " * 30)
                services.fail_text.add("story-010")
                try:
                    await value._post_new_items(1, (registry.get("first"),))
                except RuntimeError:
                    pass
                checkpoint = await value.store.seen_urls(1, "first")
                assert checkpoint and len(checkpoint) < 20
                services.fail_text.clear()
                reopened = bot(settings=value.settings)
                await reopened._post_new_items(1, (registry.get("first"),))
                output = "\n".join(payload["text"] for _, payload in services.sent)
                assert all(output.count(f"story-{index:03}") == 1 for index in range(20)), "partial digest replayed or lost stories"
                assert len(await reopened.store.seen_urls(1, "first")) == 20

            await check("partial_digest_checkpoints_only_accepted_batches", partial_digest)

            async def clear_retry():
                value = bot()
                await update(value, "/start")
                sent_id = services.sent[-1][1]["message_id"]
                services.fail_methods.add("deleteMessages")
                await update(value, "/clear")
                services.fail_methods.clear()
                await update(value, "/clear")
                deletions = [payload for method, payload in services.calls if method == "deleteMessages"]
                assert len(deletions) == 2 and sent_id in deletions[-1]["message_ids"], "failed clear forgot tracked messages"

            await check("failed_clear_preserves_message_history_for_retry", clear_retry)

            async def ambiguous_delivery(value, post):
                services.drop_responses.add("sendMessage")
                response = await post({"message": "Only once", "dedupe_key": "lost-response"}, "/message")
                assert response.status_code == 502
                assert len(services.sent) == 1, "accepted Telegram write was automatically replayed"

            await check("accepted_telegram_write_is_not_replayed_after_lost_response", lambda: integration(ambiguous_delivery))

            async def corrupt_state():
                value = bot()
                await update(value, "/note")
                await update(value, "Saved text")
                await update(value, "Saved name")
                state = Path(value.settings.state_path)
                broken = state.read_bytes()[:-1]
                state.write_bytes(broken)
                try:
                    await update(value, "/start")
                except RuntimeError:
                    pass
                assert state.read_bytes() == broken, "invalid state was replaced with an empty state"

            await check("corrupt_state_is_preserved_instead_of_overwritten", corrupt_state)

            async def host_menu():
                value = bot()
                await update(value, "/dojo")
                assert "Uptime" in services.sent[-1][1]["text"] and "Memory" in services.sent[-1][1]["text"]
                await callback(value, "dojo:refresh")
                assert services.calls[-1][0] == "editMessageText" and "Disk" in services.calls[-1][1]["text"]

            await check("host_inspection_and_menu_refresh", host_menu)

            async def mcp_bridge(value, post, *, worker_override=False):
                from riolu.agent import mcp as delivery
                # A separate local server exercises the actual MCP HTTP client.
                bridge = HookServer("127.0.0.1", 0, "", value._handle_hook,
                                    message_token="mcp-test", message_handler=value._handle_agent_message,
                                    agent_handler=value._handle_agent_action)
                await bridge.start()
                names = ("RIOLU_ENV_FILE", "RIOLU_MESSAGE_URL", "RIOLU_MCP_TOKEN", "RIOLU_AGENT_URL")
                saved = {name: os.environ.get(name) for name in names}
                os.environ["RIOLU_ENV_FILE"] = str(Path(root) / "no-env")
                os.environ["RIOLU_MESSAGE_URL"] = f"http://127.0.0.1:{bridge.port}/message"
                os.environ["RIOLU_MCP_TOKEN"] = "mcp-test"
                if worker_override:
                    os.environ["RIOLU_AGENT_URL"] = services.url
                else:
                    os.environ.pop("RIOLU_AGENT_URL", None)
                try:
                    if worker_override:
                        question = await delivery.ask_user("Worker URL must not capture MCP actions.", ["Yes"], dedupe_key="routing")
                        assert question["delivered"] == 2
                        return
                    notification = await delivery.send_message("MCP notification", dedupe_key="mcp-message")
                    assert notification["delivered"] == 2
                    question = await delivery.ask_user("MCP question", ["Yes"], dedupe_key="mcp-question")
                    assert question["delivered"] == 2
                    pending = await delivery.check_reply(question["request_id"])
                    assert pending["status"] == "pending"
                    markdown = await delivery.send_markdown("# Result\n\nText", filename="report", source="e2e", dedupe_key="mcp-md")
                    assert markdown["filename"] == "report.md" and markdown["delivered"] == 2
                    artifact = Path(root) / "attachment.txt"
                    artifact.write_text("Result")
                    outcome = await delivery.send_artifact(str(artifact), dedupe_key="mcp-artifact")
                    assert outcome["deleted"] and not artifact.exists()
                    artifact.write_text("Keep on failure")
                    services.fail_methods.add("sendDocument")
                    try:
                        await delivery.send_artifact(str(artifact), dedupe_key="mcp-artifact-failed")
                    except RuntimeError:
                        pass
                    assert artifact.exists(), "failed delivery deleted its source artifact"
                finally:
                    for name, original in saved.items():
                        if original is None:
                            os.environ.pop(name, None)
                        else:
                            os.environ[name] = original
                    await bridge.close()

            await check("mcp_notification_question_markdown_and_artifact_lifecycle", lambda: integration(mcp_bridge))
            await check("mcp_actions_do_not_use_opencode_worker_url", lambda: integration(lambda value, post: mcp_bridge(value, post, worker_override=True)))

            async def scheduler_case(now, *, last_date="", fail_first=False):
                value = bot()
                value.settings = replace(value.settings, timezone=str(now.tzinfo), daily_hour=8)
                await value.store.remember_chat(1)
                if last_date:
                    await value.store.save_digest_date(last_date)
                services.feeds["first"] = stories(1, prefix="scheduled")
                if fail_first:
                    services.fail_text.add("New Updates - First")

                class Clock:
                    @classmethod
                    def now(cls, timezone):
                        return cls.instant.astimezone(timezone)
                Clock.instant = now

                class Runtime:
                    CancelledError = asyncio.CancelledError
                    def __init__(self):
                        self.delays = []
                        self.checkpoints = []
                    async def sleep(self, seconds):
                        self.delays.append(seconds)
                        self.checkpoints.append(await value.store.digest_last_date())
                        if len(self.delays) > (3 if fail_first else 1):
                            raise asyncio.CancelledError
                        if fail_first and len(self.delays) == 2:
                            services.fail_text.clear()
                        Clock.instant = datetime.fromtimestamp(Clock.instant.timestamp() + seconds, now.tzinfo)
                        await asyncio.sleep(0)

                runtime = Runtime()
                original_datetime, original_asyncio = bot_module.datetime, bot_module.asyncio
                # Replace only clock/sleep boundaries; run the real scheduler,
                # source HTTP fetches, Telegram delivery, and durable checkpoints.
                bot_module.datetime, bot_module.asyncio = Clock, runtime
                try:
                    try:
                        await value._digest_loop()
                    except asyncio.CancelledError:
                        pass
                finally:
                    bot_module.datetime, bot_module.asyncio = original_datetime, original_asyncio
                assert len(services.sent) == 1 and "scheduled-000" in services.sent[0][1]["text"]
                assert await value.store.digest_last_date() == Clock.instant.date().isoformat()
                return runtime

            async def schedule_exact_slot():
                runtime = await scheduler_case(datetime(2026, 10, 3, 7, 59, 59, tzinfo=UTC))
                assert runtime.delays == [1, 86400], f"completed slot scheduled again: {runtime.delays}"

            async def schedule_spring():
                runtime = await scheduler_case(datetime(2026, 3, 7, 8, 0, 1, tzinfo=ZoneInfo("America/New_York")), last_date="2026-03-07")
                assert runtime.delays[0] == 23 * 3600 - 1, f"spring DST delay: {runtime.delays[0]}"

            async def schedule_fall():
                runtime = await scheduler_case(datetime(2026, 10, 31, 8, 0, 1, tzinfo=ZoneInfo("America/New_York")), last_date="2026-10-31")
                assert runtime.delays[0] == 25 * 3600 - 1, f"fall DST delay: {runtime.delays[0]}"

            async def schedule_retry():
                runtime = await scheduler_case(datetime(2026, 10, 3, 9, tzinfo=UTC), fail_first=True)
                assert runtime.delays[:3] == [60, 60, 60]
                assert runtime.checkpoints[:3] == ["", "", ""] and runtime.checkpoints[3] == "2026-10-03"

            await check("scheduler_completed_slot_moves_to_next_day", schedule_exact_slot)
            await check("scheduler_keeps_local_hour_across_spring_dst", schedule_spring)
            await check("scheduler_keeps_local_hour_across_fall_dst", schedule_fall)
            await check("scheduler_failed_delivery_retries_before_marking_day_complete", schedule_retry)

            async def long_output(mode):
                process = await asyncio.create_subprocess_exec(sys.executable, __file__, "--render-probe", mode,
                                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    output, errors = await asyncio.wait_for(process.communicate(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise AssertionError("long-line rendering stalled the event loop")
                assert process.returncode == 0, errors.decode()[-500:]

            await check("long_fenced_code_output_finishes_without_losing_content", lambda: long_output("fenced"))
            await check("long_plain_output_stays_within_message_limits", lambda: long_output("plain"))

            async def disabled_plugin():
                disabled = plugins / "disabled.py"
                disabled.write_text("raise RuntimeError('disabled source has a missing dependency')\n")
                try:
                    selected = SourceRegistry.load(str(plugins), ("first",))
                    value = bot(sources=selected)
                    services.feeds["first"] = stories(1)
                    await update(value, "/first")
                    assert len(selected.all()) == 1 and "story-000" in services.sent[0][1]["text"]
                finally:
                    disabled.unlink()

            await check("disabled_broken_source_does_not_prevent_enabled_source", disabled_plugin)

            async def rss_package():
                package = plugins / "bundle"
                package.mkdir()
                (package / "feeds.py").write_text(f"URL = {(services.url + '/rss/feed')!r}\n")
                (package / "__init__.py").write_text(
                    "from .feeds import URL\nfrom riolu.source import RssBundleSource, FeedSpec\n"
                    "SOURCE = RssBundleSource('bundle', 'Bundle', 'news', 'RSS fixture', "
                    "(FeedSpec('first', 'First feed', URL), FeedSpec('mirror', 'Mirror feed', URL)))\n"
                )
                services.rss = (
                    '<rss><channel><item><title>Upper path</title><link>/Case</link>'
                    '<description>&lt;b&gt;RSS summary&lt;/b&gt;</description>'
                    '<pubDate>Fri, 02 Oct 2026 12:00:00 GMT</pubDate></item>'
                    '<item><title>Lower path</title><link>/case</link></item></channel></rss>'
                )
                value = bot(sources=SourceRegistry.load(str(plugins), ("bundle",)))
                await update(value, "/bundle")
                output = services.sent[-1][1]["text"]
                assert output.count("Upper path") == 1 and output.count("Lower path") == 1, "case-sensitive RSS paths were merged or mirrors duplicated"
                assert "RSS summary" in output and "2026-10-02" in output

            await check("rss_package_relative_imports_and_case_sensitive_item_identity", rss_package)

            async def large_metadata():
                value = bot()
                services.feeds["first"] = [{**stories(1, prefix="large-metadata", summary="<&>" * 200)[0],
                                            "facts": ["<&>" * 4000]}]
                await value._post_new_items(1, (registry.get("first"),))
                output = services.sent[0][1]["text"]
                assert "large-metadata-000" in output, "oversized metadata hid an item that was marked seen"
                assert len(output) <= 3900

            await check("digest_fits_large_metadata_before_checkpointing_item", large_metadata)

            async def migration_flow(*, legacy=False):
                value = bot()
                await value.store.remember_chat(1)
                original = await value.store.add_note(1, "Before upgrade", "Keep this body")
                other = await value.store.add_note(10, "Destination note", "Keep this too")
                await value.store.mark_seen(1, "first", [stories(1)[0]["url"]])
                await value.store.mark_hook_sent(1, "mcp:before-upgrade")
                await value.store.remember_message(1, 77)
                await value.store.create_agent_request("old-prompt", "Choose", ["Yes"], "fixture")
                await value.store.remember_agent_prompt("old-prompt", 1, 77)
                if legacy:
                    raw = json.loads(Path(value.settings.state_path).read_text())
                    raw["chat_migrations"] = {"1": 10, "10": 20}
                    raw["chats"] = [20]
                    Path(value.settings.state_path).write_text(json.dumps(raw))
                else:
                    await update(value, "/note")
                    await update(value, "Unfinished body")
                    await value._handle_update({"message": {"chat": {"id": 1}, "migrate_to_chat_id": 10}})
                    await value._handle_update({"message": {"chat": {"id": 20}, "migrate_from_chat_id": 10}})
                    await update(value, "Draft after upgrade", chat=20)
                    assert any(n["body"] == "Unfinished body" for n in await value.store.notes_for_chat(20))
                reopened = bot(settings=value.settings)
                await update(reopened, "/notes", chat=20)
                panel = json.dumps(services.sent[-1][1])
                assert "Before upgrade" in panel, "migrated chat lost authorization or notes"
                assert "Destination note" in panel
                assert (await reopened.store.note_for_chat(20, original["id"]))["body"] == original["body"]
                assert await reopened.store.pending_agent_request_for_message(20, 77) is None, "old prompt retargeted"
                assert await reopened.store.take_message_history(20) != [77], "old message IDs retargeted"
                services.feeds["first"] = stories(1)
                services.sent.clear()
                await reopened._post_periodic_updates()
                assert not services.sent, "digest repeated a pre-migration checkpoint"
                result = await reopened._handle_agent_message({"message": "Already sent", "dedupe_key": "before-upgrade"})
                assert result[0] == 200 and not services.sent, "migration lost notification dedupe"
                await callback(reopened, f"note:delete:{original['id']}", chat=20)
                again = bot(settings=value.settings)
                assert await again.store.note_for_chat(20, original["id"]) is None, "deleted migrated note resurrected"
                assert await again.store.note_for_chat(20, other["id"])
                assert 20 not in again.settings.harness_chat_ids
                before = await again.store.chat_migrations()
                await again._handle_update({"message": {"chat": {"id": 999}, "migrate_to_chat_id": 20}})
                assert await again.store.chat_migrations() == before, "unauthorized chat changed migrations"
                try:
                    await again.store.save_chat_migration(20, 1)
                except ValueError:
                    pass
                else:
                    raise AssertionError("cyclic migration accepted")

            await check("telegram_migration_preserves_chat_data_and_authorization", migration_flow)
            await check("legacy_migration_chain_recovers_data_after_restart", lambda: migration_flow(legacy=True))

            async def migration_rejected_send():
                value = bot()
                value.settings = replace(value.settings, target_chat_id=1)
                services.migrations[1] = 10
                first = await value._handle_agent_message({"message": "Move delivery", "dedupe_key": "migration-send"})
                assert first[0] == 200, "definitively rejected send did not reach new chat"
                assert [p["chat_id"] for _, p in services.sent] == [10]
                reopened = bot(settings=value.settings)
                assert (await reopened._handle_agent_message({"message": "Move delivery", "dedupe_key": "migration-send"}))[0] == 200
                assert len(services.sent) == 1
                services.sent.clear()
                await reopened._handle_agent_artifact({"filename": "fixture.txt", "content_base64": base64.b64encode(b"body").decode()})
                assert [p["chat_id"] for _, p in services.sent] == [10], "artifact target did not resolve migration"
                services.calls.clear()
                try:
                    await reopened._show(1, bot_module.Screen("Old panel"), message_id=77)
                except Exception:
                    pass
                assert [p["chat_id"] for a, p in services.calls if a == "editMessageText"] == [1], "old edit retargeted"

            await check("migration_rejected_sends_retry_only_at_proven_destination", migration_rejected_send)

            async def limited_delivery():
                value = bot()
                services.rate_limits["sendMessage"] = 1
                await update(value, "/notes")
                assert len(services.sent) == 1 and sum(a == "sendMessage" for a, _ in services.calls) == 2
                services.unchanged_edits = True
                await callback(value, "nav:notes", message_id=services.sent[-1][1]["message_id"])
                assert not any("Something went wrong" in p.get("text", "") for a, p in services.calls if a == "editMessageText"), "no-op edit became a command failure"
                await value.telegram.request("editMessageReplyMarkup", {"chat_id": 1, "message_id": 1, "reply_markup": {"inline_keyboard": []}})

            await check("telegram_rate_limit_retries_and_unchanged_edits_succeed", limited_delivery)

            async def artifact_boundaries():
                from riolu.agent.harness import HarnessChat
                value = bot()
                value.harness = HarnessChat(value.settings, value.store, lambda: value.telegram, lambda: http)
                thread = {"chat_id": 1, "topic_id": 7, "cwd": root, "model": "default", "effort": "default", "harness": "opencode", "status": "ready"}
                files = {"output.txt": b"useful", ".ENV.production": b"secret", "private.PEM": b"secret", "empty.txt": b""}
                for name, content in files.items():
                    (Path(root) / name).write_bytes(content)
                (Path(root) / "alias.txt").symlink_to(Path(root) / ".ENV.production")
                (Path(root) / "oversized.txt").write_bytes(b"x" * 10_000_001)
                body = "Result\n" + "\n".join(f"@riolu-artifact {Path(root) / name}" for name in [*files, "alias.txt", "oversized.txt", "missing.txt"])
                await value.harness.say(thread, body)
                assert sum(a == "sendDocument" for a, _ in services.sent) == 1, "credential, empty, missing, or oversized artifact was delivered"
                assert any("artifact issue" in p.get("text", "") for _, p in services.sent)
                await value.harness.close()

            await check("harness_artifacts_reject_sensitive_and_invalid_files", artifact_boundaries)

            async def special_sources():
                special = Path(root) / "special-sources"
                special.mkdir()
                base = (
                    "import httpx\nfrom riolu.models import IntelItem\n"
                    "class FixtureSource:\n    name = 'Special source'\n    description = 'fixture'\n"
                    "    async def fetch(self, limit, client):\n"
                    f"        r = await client.get({(services.url + '/news/first')!r}); r.raise_for_status()\n"
                    "        return [IntelItem(source_id=self.id, source_name=self.name, **x) for x in r.json()[:limit]]\n"
                )
                (special / "modes.py").write_text(base +
                    "    id = 'modes'\n    category = 'ctf'\n    aliases = ('events',)\n"
                    "    modes = (('ongoing', 'Ongoing', ''), ('upcoming', 'Upcoming', ''))\n"
                    "    async def fetch_mode(self, mode, client):\n        return await self.fetch(3, client)\nSOURCE = FixtureSource()\n")
                (special / "categories.py").write_text(base +
                    "    id = 'categories'\n    category = 'news'\n    aliases = ('catalog',)\n"
                    "    categories = (('security', 'Security', ''),)\n"
                    "    async def fetch_category(self, category, limit):\n"
                    "        async with httpx.AsyncClient() as client:\n            return await self.fetch(limit, client)\nSOURCE = FixtureSource()\n")
                selected = SourceRegistry.load(str(special))
                value = bot(sources=selected)
                services.feeds["first"] = stories(20)
                await update(value, "/events")
                assert "Ongoing" in json.dumps(services.sent[-1][1])
                await callback(value, "run:modes:ongoing")
                assert "story-002" in services.calls[-1][1]["text"] and "story-003" not in services.calls[-1][1]["text"]
                refresh = next(button["callback_data"] for row in services.calls[-1][1]["reply_markup"]["inline_keyboard"] for button in row if button.get("callback_data", "").startswith("run:"))
                await callback(value, refresh)
                assert "Ongoing" in services.calls[-1][1]["text"]
                await update(value, "/latest catalog security 100")
                assert "story-009" in services.sent[-1][1]["text"] and "story-010" not in services.sent[-1][1]["text"]
                await update(value, "/latest")
                assert "modes" not in json.dumps(services.sent[-1][1]["reply_markup"]), "CTF mode leaked into news selector"
                services.fail_sources.add("first")
                await update(value, "/events upcoming")
                assert "503" in services.sent[-1][1]["text"]

            await check("special_source_modes_categories_aliases_and_refresh", special_sources)

            async def source_selection():
                value = bot()
                value.news.settings = replace(value.settings, default_source_ids=("second", "first", "second"))
                services.feeds = {"first": stories(1, prefix="first-selection"), "second": stories(1, prefix="second-selection")}
                await update(value, "/latest all")
                text = services.sent[-1][1]["text"]
                assert text.count("second-selection-000") == 1, "duplicate default source fetched/rendered twice"
                assert text.index("second-selection-000") < text.index("first-selection-000")
                for field in ("default_source_ids", "automated_source_ids"):
                    try:
                        bot(settings=replace(value.settings, **{field: ("missing",)}))
                    except RuntimeError as exc:
                        assert "missing" in str(exc)
                    else:
                        raise AssertionError(f"unknown {field} accepted silently")

            await check("configured_source_selection_deduplicates_and_rejects_missing", source_selection)

            async def config_overrides():
                config = Path(root) / "config"
                config.mkdir()
                (config / "sources.json").write_text(json.dumps({"enabled": ["First-Source"], "default": ["FIRST_SOURCE"], "automated": ["first source"]}))
                env = {k: v for k, v in os.environ.items() if not k.startswith("RIOLU_") and k not in {"SOURCE_IDS", "DEFAULT_SOURCE_IDS", "AUTOMATED_SOURCE_IDS", "DAILY_HOUR", "TIMEZONE", "MAX_LIMIT", "DEFAULT_LIMIT"}}
                env.update(TELEGRAM_TOKEN="fixture", RIOLU_CONFIG_DIR=str(config), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
                code = "from riolu.config import load_settings; import json; s=load_settings(); print(json.dumps([s.source_ids,s.default_source_ids,s.automated_source_ids,s.daily_hour]))"
                async def run(extra):
                    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, cwd=root, env={**env, **extra}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
                    return process.returncode, stdout.decode(), stderr.decode()
                status, output, failure = await run({})
                assert status == 0, failure
                assert json.loads(output)[:3] == [["first_source"]] * 3
                status, output, failure = await run({"SOURCE_IDS": "second", "DEFAULT_SOURCE_IDS": "SECOND", "AUTOMATED_SOURCE_IDS": "second", "DAILY_HOUR": "11"})
                assert status == 0 and json.loads(output) == [["second"], ["second"], ["second"], 11], failure
                status, _, failure = await run({"DAILY_HOUR": "24"})
                assert status != 0 and "between 0 and 23" in failure
                status, _, failure = await run({"TIMEZONE": "Invalid/Zone"})
                assert status != 0 and "Unknown timezone" in failure
                status, _, failure = await run({"DEFAULT_SOURCE_IDS": "missing"})
                assert status != 0 and "enabled" in failure

            await check("configuration_normalizes_source_ids_and_validates_overrides", config_overrides)

            async def coding_controls():
                from riolu.agent.harness import HarnessChat
                from riolu.agent.worker import AgentWorker
                value = bot()
                worker = AgentWorker(replace(value.settings, agent_port=0, agent_token="worker-test",
                                             opencode_url=services.url, opencode_password="", opencode_password_file=""))
                await worker.server.start()
                settings = replace(value.settings, harness_chat_ids=frozenset({1}), agent_token="worker-test",
                                   agent_url=f"http://127.0.0.1:{worker.server.port}")
                harness = HarnessChat(settings, value.store, lambda: value.telegram, lambda: http)
                thread = {"chat_id": 1, "topic_id": 7, "cwd": root, "model": "default", "effort": "default",
                          "session_id": "ses_fixture", "harness": "opencode", "status": "ready", "ownership": "telegram-worker"}
                native = {
                    "permission": [{"id": "permission-fixture", "sessionID": "ses_fixture", "action": "write", "resources": ["/fixture/report"]}],
                    "question": [{"id": "form-fixture", "sessionID": "ses_fixture", "fields": [
                        {"key": "choices", "type": "multiselect", "label": "Choose items", "options": [{"label": "One", "value": "one"}, {"label": "Comma, label", "value": "comma-value"}]},
                    ]}],
                }
                replies = []
                faults = {"reply": False}
                async def handle_native(method, path, body):
                    if method == "GET" and path.endswith("/message"):
                        return 200, {"data": [], "cursor": {}}
                    if method == "GET" and path.endswith("/permission"):
                        return 200, {"data": native["permission"]}
                    if method == "GET" and path.endswith("/form"):
                        return 200, {"data": native["question"]}
                    if method == "GET" and path.endswith("/form/form-fixture"):
                        return 200, {"data": native["question"][0]}
                    if method == "POST" and path.endswith("/reply"):
                        if faults["reply"]:
                            return 503, {"message": "fixture temporarily unavailable"}
                        replies.append((path, body))
                        native["permission" if "/permission/" in path else "question"].clear()
                        return 200, {"data": {}}
                    return 404, {"message": "fixture unexpected path"}
                services.native_handler = handle_native
                async def press(data, message_id, *, chat=1):
                    await harness.ui.callback({"id": "coding-callback", "data": data, "message": {
                        "chat": {"id": chat}, "message_id": message_id, "message_thread_id": 7}})
                    if harness.actions:
                        await asyncio.gather(*list(harness.actions))
                try:
                    await harness._load()
                    harness.threads[harness._key(thread)] = thread
                    await harness._save(thread)
                    await harness._check_requests(thread)
                    assert len(harness.requests) == 2 and len(services.sent) == 2
                    await harness._check_requests(thread)
                    assert len(services.sent) == 2, "request panels duplicated"
                    permission_token, (_, permission) = next((token, pending) for token, pending in harness.requests.items() if pending[1]["kind"] == "permission")
                    panel = next(p for _, p in services.sent if p["message_id"] == permission["_message_id"])
                    button = panel["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
                    await press(button, 999)
                    await press(button, permission["_message_id"], chat=999)
                    assert not replies, "stale or untrusted control replied natively"
                    try:
                        await harness._reply(thread, "answer", permission_token + " yes")
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("question command approved a permission")
                    faults["reply"] = True
                    await press(button, permission["_message_id"])
                    assert permission_token in harness.requests and button[7:] in harness.ui.choices
                    faults["reply"] = False
                    await asyncio.gather(press(button, permission["_message_id"]), harness._reply(thread, "approve", permission_token), return_exceptions=True)
                    assert len(replies) == 1 and replies[0][1] == {"decision": "once"}, "duplicate permission reply"
                    await press(button, permission["_message_id"])
                    assert len(replies) == 1
                    question_token, (_, question) = next(iter(harness.requests.items()))
                    panel = next(p for _, p in services.sent if p["message_id"] == question["_message_id"])
                    buttons = panel["reply_markup"]["inline_keyboard"]
                    await press(buttons[1][0]["callback_data"], question["_message_id"])
                    assert harness.ui.selections[question_token] == {1}
                    submit = next("h:pick:" + token for token, choice in harness.ui.choices.items() if choice["command"] == "submit_answer")
                    await press(submit, question["_message_id"])
                    assert len(replies) == 2 and replies[-1][1] == {"answer": {"choices": ["comma-value"]}}, "selected comma label was split"
                    await harness._check_requests(thread)
                    assert not harness.requests
                finally:
                    services.native_handler = None
                    await harness.close()
                    await worker.server.close()
                    await worker.http.aclose()

            await check("coding_permissions_questions_stale_buttons_and_duplicate_replies", coding_controls)
    await services.close()
    target = Path(os.getenv("RIOLU_E2E_REPORT", "riolu-e2e-report.json"))
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if not all(report["checks"].values()):
        raise SystemExit(1)


async def render_probe(mode):
    services = Services()
    await services.start()
    try:
        with tempfile.TemporaryDirectory(prefix="riolu-render-") as root:
            settings = Settings("unused", frozenset({1}), None, 5, 100, str(Path(root) / "state.json"), 20, ())
            value = RioluBot(settings, registry=SourceRegistry(()))
            async with httpx.AsyncClient() as http:
                value.http = http
                value.telegram = LocalTelegram(services.url, http)
                thread = {"chat_id": 1, "topic_id": 2, "cwd": root}
                content = "0123456789" * 550
                text = f"```python\n{content}\n```\nTail" if mode == "fenced" else content + "\nTail"
                await value.harness.say(thread, text, status="opencode: completed")
                visible = [BeautifulSoup(payload["text"], "html.parser").get_text()
                           for action, payload in services.sent if action == "sendMessage"]
                assert visible and max(map(len, visible)) <= 4096, "message limit exceeded"
                assert "".join(re.findall(r"\d+", "\n".join(visible))) == content, "long output lost or duplicated content"
                assert sum("opencode: completed" in text for text in visible) == 1
    finally:
        await services.close()


if __name__ == "__main__":
    asyncio.run(render_probe(sys.argv[2]) if len(sys.argv) > 1 and sys.argv[1] == "--render-probe" else main())
