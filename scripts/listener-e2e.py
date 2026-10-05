#!/usr/bin/env python3
"""Process E2E for required-listener lifecycle using disposable loopback peers.

Failure cases enumerated before fixes: occupied bind port, polling before
listener readiness, listener exception, unexpected normal listener return,
lost integration delivery, incomplete shutdown, and accidental live traffic.
The real bot runs in child processes; all Telegram requests use a local fixture.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def worker():
    import logging
    from riolu import bot as module
    from riolu.config import Settings
    from riolu.source_registry import SourceRegistry
    from riolu.telegram import TelegramAPI
    from riolu.webhook import HookServer

    endpoint, scenario, port, state = sys.argv[2:]

    class LocalTelegram(TelegramAPI):
        def _url(self, method):
            return endpoint + "/" + method

    class FixtureListener(HookServer):
        async def start(self):
            server = await super().start()
            print(json.dumps({"listener_ready": self.port}), flush=True)
            return server

        async def run(self):
            if scenario in {"crash", "stop"}:
                if self._server is None:
                    await self.start()
                await asyncio.sleep(0.3)
                if scenario == "crash":
                    raise RuntimeError("fixture listener failure")
                return
            await super().run()

    module.TelegramAPI = LocalTelegram
    module.HookServer = FixtureListener
    logging.basicConfig(level=logging.INFO)
    settings = Settings(
        "fixture", frozenset({1}), 1, 5, 100, state, 5, (),
        hook_port=int(port), mcp_token="fixture-integration",
    )
    await module.RioluBot(settings, registry=SourceRegistry(())).run()


async def main():
    import httpx

    checks, calls, logs = [], [], {}
    async def telegram(reader, writer):
        try:
            _, target, _ = (await reader.readline()).decode().split()
            headers = {}
            while line := await reader.readline():
                if line == b"\r\n":
                    break
                key, _, value = line.decode().partition(":")
                headers[key.lower()] = value.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            data = json.loads(body or b"{}")
            method = target.rsplit("/", 1)[-1]
            calls.append((method, data))
            result = True
            if method == "getUpdates":
                await asyncio.sleep(0.05)
                result = []
            elif method == "sendMessage":
                result = {"message_id": len(calls), "chat": {"id": data["chat_id"]}}
            payload = json.dumps({"ok": True, "result": result}).encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    fixture = await asyncio.start_server(telegram, "127.0.0.1", 0)
    endpoint = f"http://127.0.0.1:{fixture.sockets[0].getsockname()[1]}"
    result = {"passed": False, "fixture_only": True, "checks": checks}
    with tempfile.TemporaryDirectory(prefix="riolu-listener-") as directory:
        async def spawn(scenario, port=0):
            return await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).resolve()), "--worker", endpoint,
                scenario, str(port), str(Path(directory) / (scenario + ".json")),
                cwd=directory,
                env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )

        async def finish(process, scenario, fatal=False):
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            except TimeoutError:
                process.terminate()
                stdout, stderr = await process.communicate()
                raise AssertionError(f"{scenario}: bot stayed running after required listener failure")
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            logs[scenario] = (stdout + stderr).decode()
            if fatal:
                assert process.returncode != 0, f"{scenario}: required listener failure returned success"

        try:
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0)); occupied.listen()
                before = len(calls)
                process = await spawn("bind", occupied.getsockname()[1])
                await finish(process, "bind", fatal=True)
                assert not any(method == "getUpdates" for method, _ in calls[before:]), "polling started despite failed listener bind"
            checks.append("occupied listener port fails startup before polling")

            process = await spawn("healthy")
            try:
                ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
                port = ready["listener_ready"]
                async with httpx.AsyncClient() as http:
                    response = await http.post(
                        f"http://127.0.0.1:{port}/message",
                        headers={"Authorization": "Bearer fixture-integration"},
                        json={"message": "fixture lifecycle notification", "dedupe_key": "lifecycle"},
                    )
                    assert response.status_code == 200 and response.json()["delivered"] == 1
                assert any(method == "sendMessage" and "fixture lifecycle notification" in data["text"] for method, data in calls)
                checks.append("ready listener delivers through local Telegram fixture")
            finally:
                if process.returncode is None:
                    process.terminate()
                await finish(process, "healthy")
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                checks.append("process shutdown releases listener")
            else:
                writer.close(); await writer.wait_closed()
                raise AssertionError("listener remained reachable after shutdown")

            for scenario in ("crash", "stop"):
                process = await spawn(scenario)
                await finish(process, scenario, fatal=True)
                checks.append(f"listener {scenario} terminates bot instead of leaving false readiness")
            result["passed"] = True
        except Exception as error:
            result["error"] = str(error)
    fixture.close()
    await fixture.wait_closed()
    artifact = Path(os.getenv("RIOLU_LISTENER_REPORT", str(Path(tempfile.gettempdir()) / "riolu-listener-report.json")))
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({**result, "logs": logs}, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(worker() if len(sys.argv) > 1 and sys.argv[1] == "--worker" else main())
