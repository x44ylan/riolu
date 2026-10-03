#!/usr/bin/env python3
"""Exercise Riolu's worker and harness against disposable live OpenCode sessions.

Failure cases: wrong workspace, empty/invalid model selection, catalog reloads,
slow prompt preparation, vanished sessions, transient reads, ambiguous prompt
writes, failed turns followed by queued turns,
restart recovery, duplicate result delivery, and stale recovery anchors.
Topic failures: duplicate attachment, a literal new prefix forwarded as a prompt,
failed replacement creation erasing history, restart ownership, failed topic
deletion, stale controls, concurrent replies, and permission/question mixups.
Telegram output is recorded locally; this never sends Telegram messages.
Run from the deployment environment with its .env loaded, using the Riolu venv.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from importlib import import_module
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from dotenv import load_dotenv

from riolu.agent.harness import HarnessChat
from riolu.agent.opencode import OpenCodeV2
from riolu.agent.worker import AgentWorker
from riolu.config import load_settings
from riolu.state import StateStore


fixtures = import_module("riolu-e2e")


class TelegramRecorder(fixtures.LocalTelegram):
    def __init__(self, services, http):
        super().__init__(services.url, http)
        self.services = services

    @property
    def messages(self):
        return [payload.get("text", "") for _, payload in self.services.sent]


async def drain(harness, thread):
    deadline = time.monotonic() + 180
    while harness.jobs or thread.get("turn_anchor"):
        assert time.monotonic() < deadline, "turn did not settle within three minutes"
        await harness._check_requests(thread)
        assert not harness.requests, "unexpected permission or question in smoke prompt"
        await asyncio.sleep(1)


async def main():
    load_dotenv(Path.cwd() / ".env")
    settings = load_settings()
    worker = AgentWorker(replace(settings, agent_port=0))
    await worker.server.start()
    settings = replace(settings, agent_url=f"http://127.0.0.1:{worker.server.port}")
    report = {"checks": {}, "sessions_cleaned": False}
    sessions = []
    harness = None
    proxy = None
    services = fixtures.Services()
    await services.start()
    with tempfile.TemporaryDirectory(prefix="riolu-e2e-") as tmp:
        async with httpx.AsyncClient() as http:
            native = OpenCodeV2(http, settings)
            telegram = TelegramRecorder(services, http)
            harness = HarnessChat(settings, StateStore(Path(tmp) / "state.json"), lambda: telegram, lambda: http)
            probe = {"cwd": settings.harness_cwd or str(Path.home())}
            try:
                catalog = await harness._oc(probe, "GET", "/provider")
                located = await harness._oc(probe, "GET", "/api/model")
                assert located["location"]["directory"] == probe["cwd"], "workspace query was ignored"
                choices = [f"{p['id']}/{mid}" for p in catalog["all"] for mid in p["models"]]
                assert choices, "workspace model catalog is empty"
                model = os.getenv("RIOLU_E2E_MODEL", "proteus/gpt-5.6-sol")
                assert model in choices, f"smoke model unavailable: {model}"
                report["checks"]["workspace_catalog"] = True

                created = await harness._oc(probe, "POST", "/session", {"title": "riolu-integration-e2e"})
                sid = created["id"]
                sessions.append(sid)
                await native.configure(probe, sid, {})
                selected_default = (await native.raw(probe, "GET", f"/api/session/{sid}"))["model"]
                assert f"{selected_default['providerID']}/{selected_default['id']}" in choices
                report["checks"]["new_session_default_model_resolves"] = True
                thread = {**probe, "chat_id": 0, "topic_id": 1, "harness": "opencode", "name": "riolu-integration-e2e",
                          "session_id": sid, "ownership": "telegram-worker", "status": "ready",
                          "model": model, "effort": "low", "agent": "build"}
                harness.threads = {harness._key(thread): thread}
                await harness._prompt(thread, "Respond with exactly RIOLU-FIRST-OK. Do not use tools.")
                selected = await harness._oc_session(thread)
                assert selected.get("model", {}).get("providerID") == model.split("/", 1)[0]
                assert selected.get("model", {}).get("id") == model.split("/", 1)[1]
                assert selected.get("model", {}).get("variant") == "low"
                report["checks"]["settings_applied_before_prompt"] = True
                await harness._prompt(thread, "Respond with exactly RIOLU-SECOND-OK. Do not use tools.")
                await harness.close()
                # Reopen the harness against the same durable state while work runs.
                harness = HarnessChat(settings, StateStore(Path(tmp) / "state.json"), lambda: telegram, lambda: http)
                await harness._load()
                thread = harness.threads[harness._key(thread)]
                await harness._ensure_recovery(thread)
                await drain(harness, thread)
                output = "\n".join(telegram.messages)
                for marker in ["RIOLU-FIRST-OK", "RIOLU-SECOND-OK"]:
                    assert output.count(marker) == 1, f"missing or duplicated {marker}"
                assert thread["status"] == "ready" and not thread.get("turn_anchor")
                report["checks"]["queue_and_restart_recovery"] = True

                await harness._settings(thread, "effort", "high")
                selected = await harness._oc_session(thread)
                saved = await harness.store.harness_threads()
                assert selected["model"]["variant"] == "high"
                assert saved[harness._key(thread)]["effort"] == "high"
                await native.raw(probe, "POST", f"/api/session/{sid}/model", {
                    "model": {"providerID": model.split("/", 1)[0], "id": model.split("/", 1)[1], "variant": "low"},
                })
                await harness._sync_runtime(thread)
                assert thread["effort"] == "low", "native setting change did not reach the panel"
                report["checks"]["settings_persist_and_native_ui_syncs"] = True

                original = thread["session_id"]
                thread["session_id"] = "ses_missing_riolu_e2e"
                await harness._sync_name(thread)
                assert thread.get("session_missing") and thread["session_id"] != original
                assert not thread.get("turn_anchor")
                try:
                    await harness._prompt(thread, "Must never be sent.")
                except ValueError as exc:
                    assert "/resume" in str(exc) and "/new" in str(exc)
                else:
                    raise AssertionError("missing session accepted a prompt")
                report["checks"]["missing_session_quarantined"] = True
                thread["session_id"] = original
                thread.pop("session_missing", None)
                thread["model"] = "proteus/riolu-invalid-model"
                try:
                    await harness._prompt(thread, "Must never be admitted.")
                except RuntimeError as exc:
                    assert "model" in str(exc).lower()
                else:
                    raise AssertionError("unavailable model accepted a prompt")
                assert not thread.get("turn_anchor"), "rejected prompt left a recovery anchor"
                report["checks"]["unavailable_model_rejected_before_admission"] = True

                # Real TCP proxy faults: one transient read; one ambiguous write.
                counts = {"GET": 0, "POST": 0}
                faults = {"mode": "transient", "catalog_reads": 0, "prompt_writes": 0}
                upstream_url, password = native._service_connection()

                async def fault(reader, writer):
                    try:
                        line = (await reader.readline()).decode().strip()
                        method, target, _ = line.split()
                        length = 0
                        while True:
                            header = await reader.readline()
                            if header == b"\r\n":
                                break
                            if header.lower().startswith(b"content-length:"):
                                length = int(header.split(b":", 1)[1])
                        payload = await reader.readexactly(length) if length else b""
                        counts[method] += 1
                        if faults["mode"] != "transient":
                            if method == "POST" and target.split("?", 1)[0].endswith("/prompt"):
                                faults["prompt_writes"] += 1
                                await asyncio.sleep(31)
                                if reader.at_eof():
                                    return
                            result = await http.request(
                                method, upstream_url + target, content=payload,
                                headers={"Content-Type": "application/json"},
                                auth=("opencode", password), timeout=90,
                            )
                            status, content = result.status_code, result.content
                            if faults["mode"] == "catalog" and target.split("?", 1)[0] == "/api/model":
                                faults["catalog_reads"] += 1
                                if faults["catalog_reads"] == 1:
                                    catalog = result.json()
                                    catalog["data"] = [item for item in catalog["data"]
                                                       if f"{item['providerID']}/{item['id']}" != model]
                                    content = json.dumps(catalog).encode()
                        elif method == "GET" and counts[method] > 1:
                            result = await http.get(upstream_url + target, auth=("opencode", password))
                            status, content = result.status_code, result.content
                        else:
                            status, content = 503, b'{"message":"temporary outage"}'
                        writer.write(f"HTTP/1.1 {status} Response\r\nContent-Type: application/json\r\nContent-Length: {len(content)}\r\nConnection: close\r\n\r\n".encode() + content)
                        await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()

                proxy = await asyncio.start_server(fault, "127.0.0.1", 0)
                transport = OpenCodeV2(http, replace(settings, opencode_url=f"http://127.0.0.1:{proxy.sockets[0].getsockname()[1]}"))
                await transport.raw(probe, "GET", f"/api/session/{sid}")
                assert counts["GET"] == 2
                try:
                    await transport.raw(probe, "POST", f"/api/session/{sid}/prompt", {"text": "not forwarded"})
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("write failure was hidden")
                assert counts["POST"] == 1, "ambiguous prompt was replayed"
                report["checks"]["transient_reads_retry_writes_do_not"] = True

                faults["mode"] = "catalog"
                try:
                    await transport.configure(thread, sid, {"model": model})
                except RuntimeError:
                    report["checks"]["catalog_reload_gap_recovers"] = False
                else:
                    report["checks"]["catalog_reload_gap_recovers"] = faults["catalog_reads"] == 2
                faults["mode"] = "slow"
                before = {message["id"] for message in await native.messages(probe, sid)}
                try:
                    await transport.request(thread, "POST", f"/session/{sid}/admit", {
                        "model": model,
                        "parts": [{"type": "text", "text": "Respond with exactly RIOLU-SLOW-OK. Do not use tools."}],
                        "delivery": "queue",
                    })
                except RuntimeError:
                    report["checks"]["slow_admission_sent_once"] = False
                    await asyncio.sleep(2)
                else:
                    await native.wait_for_completion(probe, sid, before)
                    messages = [message for message in await native.messages(probe, sid) if message["id"] not in before]
                    output = "\n".join(part.get("text", "") for message in messages
                                       if message["type"] == "assistant" for part in message.get("content", []))
                    report["checks"]["slow_admission_sent_once"] = output.count("RIOLU-SLOW-OK") == 1 and faults["prompt_writes"] == 1
                assert report["checks"]["catalog_reload_gap_recovers"], "transient catalog gap rejected the selected model"
                assert report["checks"]["slow_admission_sent_once"], "slow preparation failed or replayed a prompt"

                invalid = await native.raw(probe, "POST", "/api/session", {
                    "title": "riolu-failure-e2e", "location": {"directory": probe["cwd"]},
                    "model": {"providerID": "proteus", "id": "riolu-invalid-model"},
                })
                failed_sid = invalid["id"]
                sessions.append(failed_sid)
                await native.raw(probe, "POST", f"/api/session/{failed_sid}/prompt", {"text": "Fail before producing a reply."})
                deadline = time.monotonic() + 30
                while True:
                    state = await native.raw(probe, "GET", f"/api/session/{failed_sid}")
                    if state.get("outcome") == "failed":
                        break
                    assert time.monotonic() < deadline, "invalid native model did not terminate"
                    await asyncio.sleep(1)
                recovered = {**thread, "session_id": failed_sid, "topic_id": 2,
                             "model": model, "effort": "default", "turn_anchor": "start"}
                harness.threads[harness._key(recovered)] = recovered
                output_start = len(telegram.messages)
                await harness._prompt(recovered, "Respond with exactly RIOLU-RECOVERED-OK. Do not use tools.")
                await drain(harness, recovered)
                text = "\n".join(telegram.messages[output_start:])
                assert text.count("opencode: failed") == 1, "prior failed turn was lost or mislabeled"
                assert text.count("opencode: completed") == 1, "successful turn was mislabeled"
                assert text.count("RIOLU-RECOVERED-OK") == 1 and recovered["status"] == "ready"
                report["checks"]["failure_then_success_reported_separately"] = True

                unauthorized = OpenCodeV2(http, replace(settings, opencode_password="riolu-e2e-invalid"))
                try:
                    await unauthorized.raw(probe, "GET", f"/api/session/{sid}")
                except RuntimeError as exc:
                    assert getattr(exc, "code", "") == "auth"
                    assert "credentials" in str(exc)
                else:
                    raise AssertionError("invalid credentials were accepted")
                report["checks"]["authentication_error_is_actionable"] = True

                async def topic_check(name, run):
                    try:
                        await run()
                    except Exception as exc:
                        report["checks"][name] = False
                        report.setdefault("failures", {})[name] = f"{type(exc).__name__}: {exc}"
                    else:
                        report["checks"][name] = True
                    finally:
                        services.fail_methods.clear()
                        # Include every topic-owned session in guaranteed cleanup.
                        for item in harness.threads.values():
                            owned = item.get("session_id")
                            if owned and not item.get("deleted") and owned not in sessions:
                                sessions.append(owned)

                async def duplicate_attach():
                    original = await native.raw(probe, "POST", "/api/session", {"location": {"directory": probe["cwd"]}, "title": "riolu-attach-e2e"})
                    sessions.append(original["id"])
                    await native.configure(probe, original["id"], {"model": model})
                    await native.raw(probe, "POST", f"/api/session/{original['id']}/prompt", {"text": "Respond with exactly RIOLU-ATTACH-OK. Do not use tools."})
                    await native.wait_for_completion(probe, original["id"], set())
                    results = await asyncio.gather(*(harness._dispatch(0, 0, None, "/opencode resume " + original["id"], "opencode", "resume " + original["id"]) for _ in range(2)), return_exceptions=True)
                    assert sum(isinstance(result, ValueError) for result in results) == 1, "concurrent attachment did not reject one owner"
                    count = len(harness.threads)
                    try:
                        await harness._dispatch(0, 0, None, "/opencode resume " + original["id"], "opencode", "resume " + original["id"])
                    except ValueError as exc:
                        assert "already" in str(exc)
                    assert len(harness.threads) == count, "root resume created duplicate session owners"
                    await harness._dispatch(0, 0, None, "/opencode import " + original["id"], "opencode", "import " + original["id"])
                    assert len(harness.threads) == count + 1
                    imported = list(harness.threads.values())[-1]
                    assert imported["session_id"] != original["id"], "import did not fork native session"
                    copied = await native.messages(probe, imported["session_id"])
                    assert any("RIOLU-ATTACH-OK" in str(m.get("text") or m.get("content")) for m in copied), "import lost native history"
                    await harness._command(imported, "fork", "")
                    forked = list(harness.threads.values())[-1]
                    assert forked["session_id"] != imported["session_id"]
                    copied = await native.messages(probe, forked["session_id"])
                    assert any("RIOLU-ATTACH-OK" in str(m.get("text") or m.get("content")) for m in copied), "fork lost native history"

                await topic_check("topic_resume_prevents_duplicate_ownership_and_import_forks", duplicate_attach)

                async def new_prompt():
                    await harness._dispatch(0, 0, None, "/opencode new Respond with exactly RIOLU-TOPIC-OK. Do not use tools.", "opencode", "new Respond with exactly RIOLU-TOPIC-OK. Do not use tools.")
                    created = list(harness.threads.values())[-1]
                    sessions.append(created["session_id"])
                    await drain(harness, created)
                    messages = await native.messages(probe, created["session_id"])
                    prompt = next(m for m in messages if m["type"] == "user")
                    text = prompt.get("text") or "\n".join(p.get("text", "") for p in prompt.get("content", []))
                    assert text.endswith("[/Riolu Telegram harness]\n\nRespond with exactly RIOLU-TOPIC-OK. Do not use tools."), "new prefix was forwarded"
                    assert sum("RIOLU-TOPIC-OK" in x for x in telegram.messages) >= 1

                await topic_check("opencode_new_forwards_only_the_optional_prompt", new_prompt)

                async def clear_recovery():
                    created = await harness.create(0)
                    sessions.append(created["session_id"])
                    key = harness._key(created)
                    old_topic = created["topic_id"]
                    services.fail_methods.add("createForumTopic")
                    try:
                        await harness._clear_topic_history(created)
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError("fixture did not reject replacement")
                    assert (0, old_topic) in services.topics, "failed clear erased original topic"
                    assert key in await harness.store.harness_threads()
                    services.fail_methods.clear()
                    await harness._clear_topic_history(created)
                    assert created["topic_id"] != old_topic and (0, old_topic) not in services.topics
                    records = await harness.store.harness_threads()
                    assert key not in records and records[harness._key(created)]["session_id"] == created["session_id"]
                    reopened = HarnessChat(settings, harness.store, lambda: telegram, lambda: http)
                    try:
                        await reopened._load()
                        assert reopened.threads[harness._key(created)]["session_id"] == created["session_id"]
                    finally:
                        await reopened.close()
                    services.fail_methods.add("deleteForumTopic")
                    try:
                        await harness._command(created, "delete", "")
                    except RuntimeError:
                        pass
                    assert harness._key(created) in await harness.store.harness_threads()
                    services.fail_methods.clear()
                    await harness._command(created, "delete", "")
                    assert harness._key(created) not in await harness.store.harness_threads()
                    sessions.remove(created["session_id"])

                await topic_check("topic_clear_failure_preserves_history_and_delete_recovers", clear_recovery)
            finally:
                if harness:
                    await harness.close()
                for sid in sessions:
                    await native.raw(probe, "DELETE", f"/api/session/{sid}")
                report["sessions_cleaned"] = True
                if proxy:
                    proxy.close()
                    await proxy.wait_closed()
                await worker.server.close()
                await worker.http.aclose()
                await services.close()
                target = Path(os.getenv("RIOLU_E2E_REPORT", "opencode-e2e-report.json"))
                target.write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps(report, indent=2))
    if not all(report["checks"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
