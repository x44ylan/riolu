"""Small authenticated client for the isolated Riolu agent worker."""

from __future__ import annotations

from typing import Any

import httpx

from riolu.agent.errors import OpenCodeError


class AgentWorkerClient:
    def __init__(
        self,
        http,
        url: str,
        token: str,
    ) -> None:
        self.http = http
        self.url = url.rstrip("/")
        self.token = token

    async def connect(self) -> None:
        if not self.token:
            raise RuntimeError("RIOLU_AGENT_TOKEN is required for coding sessions")
        await self._post("/health", {})

    async def opencode(
        self, thread: dict, method: str, path: str, body: dict | None = None
    ) -> Any:
        if not self.token:
            raise RuntimeError("RIOLU_AGENT_TOKEN is required for coding sessions")
        payload = await self._post(
            "/opencode",
            {"thread": thread, "method": method, "path": path, "body": body},
            timeout=None if (method == "POST" and path.endswith(("/message", "/command", "/wait"))) else 120,
        )
        return payload.get("result")

    async def _post(
        self, path: str, payload: dict, *, timeout: float | None = 45
    ) -> dict:
        try:
            response = await self.http().post(
                self.url + path,
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=timeout,
            )
        except httpx.TransportError as exc:
            raise OpenCodeError(
                "Riolu's OpenCode connection is reconnecting. Check /queue before resending a prompt.",
                code="unavailable", status=503,
            ) from exc
        if response.is_error:
            detail = response.text[:1000]
            code = "request_failed"
            try:
                error = response.json()
                detail = str(error.get("error") or detail)
                code = error.get("code") or code
            except Exception:
                pass
            raise OpenCodeError(detail, code=code, status=response.status_code)
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError("Agent worker returned an invalid response")
        return value

    async def close(self) -> None:
        return None
