from __future__ import annotations

from riolu.host import HostInspector
from riolu.screens import Screen, dojo_overview, dojo_tailscale, error


class DojoFeature:
    COMMANDS = frozenset({"dojo", "dojo_refresh", "dojo_tailscale"})

    def __init__(self, inspector: HostInspector) -> None:
        self.inspector = inspector

    async def execute(self, command: str, args: list[str]) -> Screen:
        try:
            if command in {"dojo", "dojo_refresh"}:
                return dojo_overview(await self.inspector.overview())
            if command == "dojo_tailscale":
                return dojo_tailscale(await self.inspector.tailscale())
        except Exception as exc:
            return error("Dojo unavailable", str(exc))
        return error("Unknown Dojo command", command)
