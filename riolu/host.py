from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path


MONITORED_SERVICES = ("riolu.service", "tailscaled.service", "ssh.service")


@dataclass(frozen=True)
class ServiceStatus:
    name: str
    state: str


@dataclass(frozen=True)
class HostSnapshot:
    hostname: str
    uptime_seconds: int
    load_1m: float
    cpu_count: int
    memory_used: int
    memory_total: int
    disk_used: int
    disk_total: int
    services: tuple[ServiceStatus, ...]


@dataclass(frozen=True)
class TailscalePeer:
    name: str
    address: str
    online: bool
    os: str = ""


class HostInspector:
    async def overview(self) -> HostSnapshot:
        services = await asyncio.gather(*(_service_status(name) for name in MONITORED_SERVICES))
        memory_used, memory_total = _memory_usage()
        disk = shutil.disk_usage("/")
        return HostSnapshot(
            hostname=socket.gethostname(),
            uptime_seconds=_uptime_seconds(),
            load_1m=os.getloadavg()[0],
            cpu_count=os.cpu_count() or 1,
            memory_used=memory_used,
            memory_total=memory_total,
            disk_used=disk.used,
            disk_total=disk.total,
            services=tuple(services),
        )

    async def tailscale(self) -> tuple[TailscalePeer, ...]:
        output = await _run("tailscale", "status", "--json")
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Tailscale returned invalid status data") from exc
        peers: list[TailscalePeer] = []
        for value in data.get("Peer", {}).values():
            if not isinstance(value, dict):
                continue
            addresses = value.get("TailscaleIPs", [])
            address = str(addresses[0]) if isinstance(addresses, list) and addresses else ""
            name = str(value.get("HostName") or value.get("DNSName") or address or "Unknown")
            peers.append(
                TailscalePeer(
                    name=name.rstrip("."),
                    address=address,
                    online=bool(value.get("Online", False)),
                    os=str(value.get("OS") or ""),
                )
            )
        return tuple(sorted(peers, key=lambda peer: (not peer.online, peer.name.casefold())))


async def _service_status(name: str) -> ServiceStatus:
    try:
        state = (await _run("systemctl", "is-active", name, allow_failure=True)).strip() or "unknown"
    except OSError:
        state = "unavailable"
    return ServiceStatus(name=name.removesuffix(".service"), state=state)


async def _run(*command: str, allow_failure: bool = False) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=8)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"{command[0]} timed out")
    if process.returncode and not allow_failure:
        message = stderr.decode(errors="replace").strip()
        raise RuntimeError(message or f"{command[0]} failed")
    return stdout.decode(errors="replace")


def _uptime_seconds() -> int:
    try:
        return int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
    except (OSError, ValueError, IndexError):
        return 0


def _memory_usage() -> tuple[int, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return 0, 0
    total = values.get("MemTotal", 0)
    return max(0, total - values.get("MemAvailable", total)), total
