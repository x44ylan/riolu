from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, Awaitable, Callable

LOGGER = logging.getLogger(__name__)

MAX_BODY_BYTES = 65_536
MAX_AGENT_BODY_BYTES = 15_000_000
ALLOWED_TYPES = frozenset({"challenge.blocked", "challenge.solved"})
ALLOWED_STATUS = frozenset({"blocked", "solved"})
ALLOWED_URGENCY = frozenset({"info", "success", "warning", "error"})
MAX_MESSAGE_LENGTH = 3_500
MAX_LABEL_LENGTH = 100

HandlerResult = int | tuple[int, object]
HookHandler = Callable[[dict[str, Any]], Awaitable[HandlerResult]]

_PHRASES = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    413: "Payload Too Large",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


def validate_event(payload: object) -> str:
    """Return an error message, or "" when the event can be announced."""
    if not isinstance(payload, dict):
        return "payload must be a JSON object"
    for key in ("id", "type", "ctf", "challenge", "status"):
        if not str(payload.get(key) or "").strip():
            return f"missing or empty {key!r}"
    if not isinstance(payload["type"], str) or payload["type"] not in ALLOWED_TYPES:
        return f"unsupported type {payload['type']!r}"
    if not isinstance(payload["status"], str) or payload["status"] not in ALLOWED_STATUS:
        return f"unsupported status {payload['status']!r}"
    if payload["type"] != f"challenge.{payload['status']}":
        return "type and status do not match"
    return ""


def validate_message(payload: object) -> str:
    """Return an error message, or "" when an agent notification is valid."""
    if not isinstance(payload, dict):
        return "payload must be a JSON object"
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return "missing or empty 'message'"
    if len(message.strip()) > MAX_MESSAGE_LENGTH:
        return f"message exceeds {MAX_MESSAGE_LENGTH} characters"
    for key in ("title", "source", "dedupe_key"):
        value = payload.get(key, "")
        if not isinstance(value, str):
            return f"{key!r} must be a string"
        if len(value.strip()) > MAX_LABEL_LENGTH:
            return f"{key!r} exceeds {MAX_LABEL_LENGTH} characters"
    urgency = payload.get("urgency", "info")
    if not isinstance(urgency, str) or urgency not in ALLOWED_URGENCY:
        return f"unsupported urgency {urgency!r}"
    return ""


class HookServer:
    """Minimal authenticated HTTP endpoints for local Riolu integrations."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        handler: HookHandler,
        *,
        message_token: str = "",
        message_handler: HookHandler | None = None,
        agent_handler: HookHandler | None = None,
        request_timeout: float = 75,
    ) -> None:
        self.host = host
        self.port = port
        self.routes: dict[str, tuple[str, HookHandler, int]] = {}
        if token:
            self.routes["/hook"] = (token, handler, MAX_BODY_BYTES)
        if message_token and message_handler is not None:
            self.routes["/message"] = (message_token, message_handler, MAX_BODY_BYTES)
        if message_token and agent_handler is not None:
            self.routes["/agent"] = (message_token, agent_handler, MAX_AGENT_BODY_BYTES)
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task] = set()
        self.request_timeout = request_timeout

    async def start(self) -> asyncio.AbstractServer:
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]  # real port when bound to 0
        return self._server

    async def run(self) -> None:
        server = self._server if self._server is not None else await self.start()
        async with server:
            await server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        clients = list(self._clients)
        for task in clients:
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self._clients.add(task)
        try:
            await asyncio.wait_for(self._chat(reader, writer), self.request_timeout)
        except Exception:
            LOGGER.debug("Hook connection failed", exc_info=True)
        finally:
            if task:
                self._clients.discard(task)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (ConnectionError, OSError, TimeoutError):
                pass  # client already gone

    async def _chat(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, path, _ = (await reader.readline()).decode("latin-1").split()
        except ValueError:
            return await self._respond(writer, 400)
        headers: dict[str, str] = {}
        for _ in range(100):
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        route = self.routes.get(path.rstrip("/"))
        if method != "POST" or route is None:
            return await self._respond(writer, 404)
        token, handler, max_body_bytes = route
        if not hmac.compare_digest(headers.get("authorization", ""), f"Bearer {token}"):
            return await self._respond(writer, 401)
        try:
            size = int(headers.get("content-length", "0"))
        except ValueError:
            return await self._respond(writer, 400)
        if size < 0 or size > max_body_bytes:
            return await self._respond(writer, 413)
        body = await reader.readexactly(size) if size else b""
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return await self._respond(writer, 400)
        if not isinstance(payload, dict):
            return await self._respond(writer, 400)
        try:
            result = await handler(payload)
        except Exception:
            LOGGER.exception("Local integration handler failed")
            return await self._respond(writer, 500)
        if isinstance(result, tuple):
            status, response = result
        else:
            status, response = int(result), None
        await self._respond(writer, status, response)

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        response: object | None = None,
    ) -> None:
        payload = response if response is not None else {"ok": status == 200}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {_PHRASES.get(status, 'Error')}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + body)
        await writer.drain()
