"""OpenCode V2 transport for the Telegram harness's session operations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from urllib.parse import quote

import httpx

from riolu.agent.errors import OpenCodeError, failure_reason


RUN_POLL_SECONDS = 2
RUN_TIMEOUT_SECONDS = 60 * 60


class OpenCodeV2:
    def __init__(self, http, settings):
        self.http = http
        self.settings = settings

    async def raw(self, thread, method, path, body=None, *, params=None, unwrap=True):
        url, password = self._service_connection()
        # Location is a nested V2 query. Dot-separated keys are silently ignored.
        params = {"location[directory]": thread["cwd"], **(params or {})}
        attempts = 3 if method == "GET" else 1
        read_timeout = None if path.endswith("/wait") else (
            90 if method == "POST" and path.endswith(("/prompt", "/command")) else 30
        )
        for attempt in range(attempts):
            try:
                response = await self.http.request(
                    method, url.rstrip("/") + path,
                    params=params, json=body,
                    auth=httpx.BasicAuth("opencode", password) if password else None,
                    timeout=httpx.Timeout(30, read=read_timeout),
                )
            except httpx.TransportError as exc:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                detail = "OpenCode is unreachable. Try again when the server reconnects."
                if method != "GET":
                    detail += " Check /queue before resending; your request may have been accepted."
                raise OpenCodeError(detail, code="unavailable", status=503) from exc
            if response.status_code in {502, 503, 504} and attempt + 1 < attempts:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            break
        if response.is_error:
            try:
                error = response.json()
            except ValueError:
                error = {}
            if not isinstance(error, dict):
                error = {}
            detail = str(error.get("message") or f"OpenCode returned HTTP {response.status_code}.")
            code = "request_failed"
            if error.get("_tag") == "SessionNotFoundError":
                code = "session_missing"
                detail = "This OpenCode session no longer exists. Use /resume to attach a saved session or /new to start one."
            elif response.status_code in {401, 403}:
                code, detail = "auth", "OpenCode rejected Riolu's credentials. The server connection needs reauthorization."
            elif response.status_code in {502, 503, 504}:
                code, detail = "unavailable", "OpenCode is temporarily unavailable. Check /queue before resending a prompt."
            raise OpenCodeError(detail[:1000], code=code, status=response.status_code)
        result = response.json() if response.content else {}
        if unwrap and isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    async def configure(self, thread, sid, body):
        """Check selections before a prompt enters OpenCode's durable inbox."""
        base = f"/api/session/{sid}"
        native = await self.raw(thread, "GET", base)
        selected = body.get("model") or thread.get("model")
        if selected == "default" or not selected:
            selected = native.get("model")
        if isinstance(selected, str):
            provider, _, name = selected.partition("/")
            selected = {"providerID": provider, "id": name}
        elif selected:
            selected = {**selected, "id": selected.get("id", selected.get("modelID"))}
        if not selected:
            selected = await self.raw(thread, "GET", "/api/model/default")
        if not selected:
            raise OpenCodeError("No model is available. Use /model to choose a connected model.", code="model_unavailable", status=409)
        for attempt in range(3):
            models = await self.raw(thread, "GET", "/api/model")
            available = next((item for item in models
                              if item["providerID"] == selected["providerID"] and item["id"] == selected["id"]), None)
            if available is not None or attempt == 2:
                break
            await asyncio.sleep(0.5 * (attempt + 1))
        if available is None:
            raise OpenCodeError(f"Model {selected['providerID']}/{selected['id']} is unavailable. Choose a connected model with /model.", code="model_unavailable", status=409)
        model = {"providerID": selected["providerID"], "id": selected["id"]}
        variant = body.get("variant", thread.get("effort") or selected.get("variant"))
        if variant and variant != "default":
            if variant not in {item["id"] for item in available.get("variants", [])}:
                raise OpenCodeError(f"Effort {variant} is unavailable for this model. Choose one with /effort.", code="model_unavailable", status=409)
            model["variant"] = variant
        if model != native.get("model"):
            await self.raw(thread, "POST", base + "/model", {"model": model})
        agent = body.get("agent") or thread.get("agent")
        if agent and agent != native.get("agent"):
            await self.raw(thread, "POST", base + "/agent", {"agent": agent})

    def _service_connection(self):
        url = self.settings.opencode_url
        password = self.settings.opencode_password
        filename = self.settings.opencode_password_file
        if not filename or password:
            return url, password
        text = Path(filename).read_text()
        try:
            service = json.loads(text)
        except json.JSONDecodeError:
            service = None
        if isinstance(service, dict):
            password = str(service.get("password") or "")
            if service.get("url") and url == "http://127.0.0.1:4096":
                url = str(service["url"])
            return url, password
        for line in text.splitlines():
            if line.startswith("OPENCODE_SERVER_PASSWORD="):
                password = line.partition("=")[2].strip().strip("\"'")
        return url, password

    async def messages(self, thread, sid):
        result, cursor = [], None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            else:
                params["order"] = "asc"
            page = await self.raw(thread, "GET", f"/api/session/{sid}/message", params=params, unwrap=False)
            result.extend(page.get("data", []))
            next_cursor = (page.get("cursor") or {}).get("next")
            if not next_cursor or next_cursor == cursor:
                return result
            cursor = next_cursor

    async def run(self, thread, sid, action, body):
        base = f"/api/session/{sid}"
        before = {message["id"] for message in await self.messages(thread, sid)}
        await self.configure(thread, sid, body)
        delivery = body.get("delivery", "queue")
        if action == "command":
            await self.raw(thread, "POST", base + "/command", {"name": body["command"], "text": body.get("arguments", ""), "delivery": delivery})
        else:
            text = "\n".join(part.get("text", "") for part in body.get("parts", []) if part.get("type") == "text")
            await self.raw(thread, "POST", base + "/prompt", {"text": text, "delivery": delivery})
        await self.wait_for_completion(thread, sid, before)
        messages = [message for message in await self.messages(thread, sid) if message["id"] not in before and message["type"] == "assistant"]
        for message in messages:
            if message.get("error"):
                raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
        native = await self.raw(thread, "GET", base)
        if native.get("outcome") == "failed":
            raise RuntimeError(failure_reason(messages, native))
        model = native.get("model") or {}
        final_parts = next((
            [part for part in message.get("content", [])
             if part.get("type") == "text" and part.get("text")]
            for message in reversed(messages)
            if any(part.get("type") == "text" and part.get("text")
                   for part in message.get("content", []))
        ), [])
        return {
            "info": {"providerID": model.get("providerID"), "modelID": model.get("id"), "variant": model.get("variant")},
            "parts": final_parts,
        }

    async def wait_for_completion(self, thread, sid, before):
        """Wait for this turn's idle message, including pauses for user input."""
        base = f"/api/session/{sid}"
        waiter = asyncio.create_task(
            self.raw(thread, "POST", f"/api/experimental/session/{sid}/wait")
        )
        started = time.monotonic()
        wait_error = None
        try:
            while True:
                if waiter is not None:
                    done, _ = await asyncio.wait({waiter}, timeout=RUN_POLL_SECONDS)
                    if waiter in done:
                        try:
                            await waiter
                        except Exception as exc:
                            wait_error = exc
                            waiter = None
                        else:
                            waiter = None
                else:
                    await asyncio.sleep(RUN_POLL_SECONDS)

                permission = await self.raw(thread, "GET", base + "/permission")
                forms = await self.raw(thread, "GET", base + "/form")
                if permission or forms:
                    # A native wait can finish when execution pauses for input.
                    # Do not report completion or time out while the user decides.
                    started = time.monotonic()
                    continue
                native = await self.raw(thread, "GET", base)
                recent = [
                    message
                    for message in await self.messages(thread, sid)
                    if message["id"] not in before
                ]
                last_user = max((index for index, message in enumerate(recent)
                                 if message["type"] == "user"), default=-1)
                has_idle = any(message["type"] == "idle"
                               for message in recent[last_user + 1:])
                now = time.monotonic()
                if has_idle and native.get("outcome") == "failed":
                    raise RuntimeError(failure_reason(recent, native))
                if has_idle and native.get("outcome") == "interrupted":
                    raise RuntimeError("OpenCode was interrupted; send a new prompt to continue.")
                if has_idle and native.get("outcome") == "succeeded":
                    return
                if now - started >= RUN_TIMEOUT_SECONDS:
                    detail = f": {wait_error}" if wait_error else ""
                    raise TimeoutError(f"OpenCode did not finish within one hour{detail}")
        finally:
            if waiter is not None and not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    async def request(self, thread, method, path, body=None):
        # Keep the harness's operation interface stable while using only V2 HTTP.
        if path.startswith("/api/"):
            return await self.raw(thread, method, path, body, unwrap=False)
        location = {"directory": thread["cwd"]}
        if path == "/session":
            if method == "POST":
                result = await self.raw(thread, method, "/api/session", {**(body or {}), "location": location})
                return session_view(result)
            sessions, cursor = [], None
            while True:
                params = {"directory": thread["cwd"], "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                page = await self.raw(thread, method, "/api/session", params=params, unwrap=False)
                sessions.extend(session_view(item) for item in page.get("data", []))
                following = (page.get("cursor") or {}).get("next")
                if not following or following == cursor:
                    return sessions
                cursor = following
        if path in {"/provider", "/agent", "/command"}:
            params = {"location[directory]": thread["cwd"]}
            if path == "/provider":
                models = await self.raw(thread, "GET", "/api/model", params=params)
                providers = {}
                for model in models:
                    entry = {**model, "variants": {variant["id"]: variant for variant in model.get("variants", [])}}
                    providers.setdefault(model["providerID"], {"id": model["providerID"], "models": {}})["models"][model["id"]] = entry
                return {"all": list(providers.values()), "connected": list(providers)}
            items = await self.raw(thread, "GET", "/api" + path, params=params)
            return [{**item, "name": item.get("id", item.get("name"))} for item in items]
        sid = quote(thread.get("session_id", ""), safe="")
        base = f"/api/session/{sid}"
        if path == "/permission":
            requests = await self.raw(thread, "GET", base + "/permission")
            return [{**item, "permission": item["action"], "patterns": item["resources"]} for item in requests]
        if path == "/question":
            forms = await self.raw(thread, "GET", base + "/form")
            return [{**form, "questions": [field_question(field) for field in form["fields"] if not field.get("hidden")]} for form in forms]
        if path.startswith("/permission/"):
            request_id = path.split("/")[2]
            return await self.raw(thread, "POST", base + f"/permission/{request_id}/reply", {"decision": body["reply"]})
        if path.startswith("/question/"):
            form_id = path.split("/")[2]
            form = await self.raw(thread, "GET", base + f"/form/{form_id}")
            fields = [field for field in form["fields"] if not field.get("hidden")]
            answers = body["answers"]
            if len(fields) != len(answers):
                raise ValueError("The form changed. Refresh the question before answering.")
            answer = {field["key"]: field_answer(field, value, exact=bool(body.get("exact")))
                      for field, value in zip(fields, answers)}
            return await self.raw(thread, "POST", base + f"/form/{form_id}/reply", {"answer": answer})
        if path.startswith("/session/"):
            parts = path.split("/")
            sid = parts[2]
            base = f"/api/session/{sid}"
            action = parts[3] if len(parts) > 3 else ""
            if action == "runtime" and method == "GET":
                # Session records omit the effective model. The latest assistant
                # message is the authoritative record of what actually ran.
                page = await self.raw(
                    thread, "GET", base + "/message",
                    params={"limit": 20, "order": "desc"}, unwrap=False,
                )
                for message in page.get("data", []):
                    if message.get("type") != "assistant":
                        continue
                    model = message.get("model") or {}
                    provider = model.get("providerID")
                    name = model.get("id")
                    return {
                        "model": f"{provider}/{name}" if provider and name else "",
                        "variant": message.get("variant") or model.get("variant") or "default",
                        "agent": message.get("agent") or "",
                    }
                return {"model": "", "variant": "default", "agent": ""}
            if action == "admit" and method == "POST":
                await self.configure(thread, sid, body)
                # Native queue admission returns immediately. OpenCode owns
                # ordering, durability, retries, and delivery of inbox items.
                delivery = body.get("delivery", "queue")
                if body.get("command"):
                    result = await self.raw(
                        thread, "POST", base + "/command",
                        {"name": body["command"], "text": body.get("arguments", ""),
                         "delivery": delivery}, unwrap=False,
                    )
                else:
                    text = "\n".join(
                        part.get("text", "") for part in body.get("parts", [])
                        if part.get("type") == "text"
                    )
                    result = await self.raw(
                        thread, "POST", base + "/prompt",
                        {"text": text, "delivery": delivery}, unwrap=False,
                    )
                return message_view(result["data"])
            if action in {"message", "command"} and method == "POST":
                return await self.run(thread, sid, action, body or {})
            if action == "message":
                return [message_view(message) for message in await self.messages(thread, sid)]
            if action == "abort":
                return await self.raw(thread, "POST", base + "/interrupt")
            if action == "summarize":
                return await self.raw(thread, "POST", base + "/compact", {"delivery": "queue"})
            if action == "revert":
                return await self.raw(thread, "POST", base + "/revert/stage", body)
            if action == "unrevert":
                return await self.raw(thread, "DELETE", base + "/revert")
            if action == "share":
                raise ValueError("OpenCode V2 has no public sharing endpoint. Use /export instead.")
            if action == "shell":
                return await self.raw(thread, "POST", base + "/shell", {"command": body["command"]})
            result = await self.raw(thread, method, "/api" + path, body)
            if method == "GET" and not action or action == "fork":
                return session_view(result)
            return result
        raise ValueError("Use a V2 /api/ path for native OpenCode API operations.")


def session_view(value):
    return {**value, "directory": (value.get("location") or {}).get("directory", "")}


def message_view(value):
    model = value.get("model") or {}
    return {
        "info": {**value, "role": value["type"], "providerID": model.get("providerID"), "modelID": model.get("id")},
        "parts": value.get("content", [{"type": "text", "text": value.get("text", "")}]),
    }


def field_question(field):
    def option_label(option):
        if not isinstance(option, dict):
            return str(option)
        return str(option.get("label") or option.get("title") or option.get("value") or "")

    return {
        "id": field["key"], "question": field.get("title") or field.get("description") or field["key"],
        "multiple": field["type"] == "multiselect", "isSecret": field.get("secret", False),
        "options": [{"label": option_label(option), "description": option.get("description", "") if isinstance(option, dict) else ""} for option in field.get("options", [])],
    }


def field_answer(field, answers, *, exact=False):
    value = answers[0] if answers else ""
    options = {
        str(option.get("label") or option.get("title") or option.get("value")): option.get("value", option.get("label", option.get("title")))
        for option in field.get("options", []) if isinstance(option, dict)
    }
    if field["type"] == "multiselect":
        items = answers if exact else str(value).split(",")
        return [options.get(item.strip(), item.strip()) for item in items if item.strip()]
    if field["type"] == "number":
        return float(value)
    if field["type"] == "boolean":
        if value.lower() not in {"true", "false", "yes", "no"}:
            raise ValueError("Answer yes or no for this field.")
        return value.lower() in {"true", "yes"}
    return options.get(value, value)
