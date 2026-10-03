"""Render OpenCode usage-tracker plugin results for Telegram."""

from __future__ import annotations

import math
from typing import Any


USAGE_RPC_ID = "opencode-usage-tracker"
ALL_PROVIDERS = "all"
_BAR_WIDTH = 14


def usage_rpc_path() -> str:
    return f"/api/rpc/{USAGE_RPC_ID}/usage"


def format_usage(result: object) -> str:
    """Render the full all-providers view without truncating provider data."""
    if not isinstance(result, dict):
        return "Usage tracker returned an invalid response."

    kind = str(result.get("kind") or "")
    if kind != "ok":
        message = str(result.get("message") or "").strip()
        return message or "No usage data available."

    cards = result.get("providers")
    providers = _provider_views(cards if isinstance(cards, list) else [])
    if not providers:
        return "No usage data reported."

    lines = ["Usage Tracker · All Providers"]
    for provider in providers:
        header = str(provider["provider"])
        if provider.get("planType"):
            header += f" · {provider['planType']}"
        lines.extend(("", header))
        for section in provider["sections"]:
            lines.extend(_section_lines(section))
    return "\n".join(lines).strip()


def _provider_views(cards: list[object]) -> list[dict[str, Any]]:
    providers: dict[str, dict[str, Any]] = {}
    for value in cards:
        if not isinstance(value, dict):
            continue
        provider_id = str(
            value.get("providerId") or value.get("provider") or "provider"
        )
        section = {
            "label": str(value.get("sectionLabel") or "").strip(),
            "note": str(value.get("note") or "").strip(),
            "order": _number(value.get("sectionOrder"), 10),
            "main": value.get("sectionKind") == "main",
            "windows": [
                window
                for window in (value.get("windows") or [])
                if isinstance(window, dict)
            ],
            "extra": {
                str(key): str(item)
                for key, item in (value.get("extra") or {}).items()
                if item is not None
            }
            if isinstance(value.get("extra"), dict)
            else {},
            "error": str(value.get("error") or "").strip(),
        }
        provider = providers.get(provider_id)
        if provider is None:
            provider = {
                "provider": str(value.get("provider") or provider_id),
                "planType": str(value.get("planType") or "").strip(),
                "sections": [],
            }
            providers[provider_id] = provider
        provider["planType"] = (
            provider.get("planType") or str(value.get("planType") or "").strip()
        )
        provider["sections"].append(section)
    return [
        {
            **provider,
            "sections": sorted(
                provider["sections"],
                key=lambda section: (section["order"], 0 if section["main"] else 1),
            ),
        }
        for provider in providers.values()
    ]


def _section_lines(section: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if section["label"]:
        lines.append(section["label"])
    if section["note"]:
        lines.append(section["note"])
    if section["error"]:
        lines.append(f"Error · {section['error']}")
        return lines

    rows: list[tuple[str, str]] = []
    for window in section["windows"]:
        label = str(window.get("label") or "Usage").strip() or "Usage"
        percent = _percent(window.get("usedPercent"))
        lines.append(f"{label} · {percent}% {_bar(percent)}")
        if window.get("resetTime"):
            rows.append((f"{label} resets", str(window["resetTime"])))
    rows.extend(section["extra"].items())
    lines.extend(f"{label} · {value}" for label, value in rows)
    if not section["windows"] and not rows:
        lines.append("No usage data reported.")
    return lines


def _percent(value: object) -> int:
    try:
        percent = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        percent = 0
    return max(0, min(100, math.floor(percent + 0.5)))


def _bar(percent: int) -> str:
    filled = math.floor(percent / 100 * _BAR_WIDTH + 0.5)
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def _number(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
