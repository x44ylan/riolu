"""Isolated process that owns coding-agent connections for Riolu."""

from __future__ import annotations

import asyncio
import logging
import secrets
import signal

import httpx

from riolu.config import load_settings
from riolu.agent.opencode import OpenCodeV2
from riolu.agent.errors import OpenCodeError
from riolu.webhook import HookServer

LOGGER = logging.getLogger(__name__)


class AgentWorker:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.instance_id = secrets.token_hex(8)
        self.http = httpx.AsyncClient(follow_redirects=True)
        self.server = HookServer(
            settings.agent_host,
            settings.agent_port,
            settings.agent_token,
            self.health,
            request_timeout=3600,
        )
        self.server.routes.update(
            {
                "/health": (settings.agent_token, self.health, 1024),
                "/opencode": (
                    settings.agent_token,
                    self.opencode,
                    2_000_000,
                ),
            }
        )

    async def health(self, payload: dict) -> tuple[int, dict[str, object]]:
        return 200, {
            "ok": True,
            "instance_id": self.instance_id,
        }

    async def opencode(self, payload: dict) -> tuple[int, dict[str, object]]:
        thread = payload.get("thread")
        method = str(payload.get("method") or "").upper()
        path = payload.get("path")
        body = payload.get("body")
        if (
            not isinstance(thread, dict)
            or method not in {"GET", "POST", "PATCH", "DELETE"}
            or not isinstance(path, str)
            or not path.startswith("/")
            or (body is not None and not isinstance(body, dict))
        ):
            return 400, {"error": "invalid OpenCode request"}
        try:
            result = await OpenCodeV2(self.http, self.settings).request(
                thread, method, path, body
            )
        except OpenCodeError as exc:
            LOGGER.warning("OpenCode %s %s: %s", method, path, exc)
            return exc.status, {"error": str(exc), "code": exc.code}
        except Exception as exc:
            LOGGER.exception("OpenCode worker request failed: %s %s", method, path)
            return 502, {"error": str(exc)}
        return 200, {"result": result}

    async def run(self) -> None:
        if not self.settings.agent_token:
            raise RuntimeError("RIOLU_AGENT_TOKEN is required")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await self.server.start()
        LOGGER.info(
            "Riolu agent listening on %s:%s",
            self.settings.agent_host,
            self.server.port,
        )
        await stop.wait()
        await self.server.close()
        await self.http.aclose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(AgentWorker(load_settings()).run())


if __name__ == "__main__":
    main()
