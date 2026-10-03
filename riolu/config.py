from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from riolu.source import normalize_source_token

@dataclass(frozen=True)
class DojoToolConfig:
    name: str
    kind: str
    target: str
    port: int = 0
    url: str = ""


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_chat_ids: frozenset[int]
    target_chat_id: int | None
    default_limit: int
    max_limit: int
    state_path: str
    http_timeout_seconds: float
    default_source_ids: tuple[str, ...]
    source_directory: str = ""
    source_ids: tuple[str, ...] = ()
    automated_source_ids: tuple[str, ...] = ()
    timezone: str = "UTC"
    daily_hour: int = 8
    dojo_services: tuple[str, ...] = ()
    dojo_tools: tuple[DojoToolConfig, ...] = ()
    hook_host: str = "127.0.0.1"
    hook_port: int = 8301
    hook_token: str = ""
    mcp_token: str = ""
    opencode_url: str = "http://127.0.0.1:4096"
    opencode_password: str = ""
    opencode_password_file: str = ""
    harness_cwd: str = ""
    agent_url: str = "http://127.0.0.1:8302"
    agent_host: str = "127.0.0.1"
    agent_port: int = 8302
    agent_token: str = ""
    harness_chat_ids: frozenset[int] = frozenset()
    daily_news_chat_id: int | None = None
    daily_news_thread_id: int | None = None


def load_settings() -> Settings:
    load_dotenv()
    config_dir = Path(
        os.getenv("RIOLU_CONFIG_DIR", str(Path.home() / ".config" / "riolu"))
    ).expanduser()
    app = _load_json(config_dir / "riolu.json")
    source_config = _load_json(config_dir / "sources.json")
    dojo_config = _load_json(config_dir / "dojo.json")

    token = _string_env("TELEGRAM_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is required")

    limits = _object(app, "limits")
    telegram = _object(app, "telegram")
    schedule = _object(app, "schedule")
    agent = _object(app, "agent")
    max_limit = max(1, _int_env("MAX_LIMIT", _integer(limits, "maximum", 100)))
    default_limit = max(
        1,
        min(_int_env("DEFAULT_LIMIT", _integer(limits, "default", 5)), max_limit),
    )
    timezone = _string_env("TIMEZONE", _string(schedule, "timezone", "UTC"))
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unknown timezone: {timezone}") from exc
    daily_hour = _int_env("DAILY_HOUR", _integer(schedule, "hour", 8))
    if not 0 <= daily_hour <= 23:
        raise RuntimeError("DAILY_HOUR must be between 0 and 23")

    source_directory = _string(
        source_config, "directory", str(config_dir / "sources")
    )
    source_ids = _source_ids_env(
        "SOURCE_IDS", _string_list(source_config, "enabled", ())
    )
    default_source_ids = _source_ids_env(
        "DEFAULT_SOURCE_IDS", _string_list(source_config, "default", ())
    )
    automated_source_ids = _source_ids_env(
        "AUTOMATED_SOURCE_IDS",
        _string_list(source_config, "automated", ()),
    )
    if source_ids and not set(default_source_ids).issubset(source_ids):
        raise RuntimeError("Default sources must also be enabled")
    if source_ids and not set(automated_source_ids).issubset(source_ids):
        raise RuntimeError("Automated sources must also be enabled")

    return Settings(
        telegram_token=token,
        allowed_chat_ids=frozenset(
            _int_list_env("ALLOWED_CHAT_IDS", _integer_list(telegram, "allowed_chat_ids"))
        ),
        target_chat_id=_optional_int_env(
            "TARGET_CHAT_ID", _optional_integer(telegram, "target_chat_id")
        ),
        default_limit=default_limit,
        max_limit=max_limit,
        state_path=_string_env(
            "STATE_PATH", _string(app, "state_path", ".riolu-state.json")
        ) or ".riolu-state.json",
        http_timeout_seconds=max(
            1.0,
            _float_env(
                "HTTP_TIMEOUT_SECONDS", _number(app, "http_timeout_seconds", 20.0)
            ),
        ),
        default_source_ids=default_source_ids,
        source_directory=source_directory,
        source_ids=source_ids,
        automated_source_ids=automated_source_ids,
        timezone=timezone,
        daily_hour=daily_hour,
        dojo_services=_string_list(dojo_config, "services", ()),
        dojo_tools=_dojo_tools(dojo_config),
        hook_host=_string_env("HOOK_HOST", "127.0.0.1") or "127.0.0.1",
        hook_port=max(1, _int_env("HOOK_PORT", 8301)),
        hook_token=_string_env("HOOK_TOKEN", ""),
        mcp_token=_string_env("RIOLU_MCP_TOKEN", ""),
        opencode_url=_string_env("RIOLU_OPENCODE_URL", "http://127.0.0.1:4096"),
        opencode_password=_string_env("RIOLU_OPENCODE_PASSWORD", ""),
        opencode_password_file=_string_env("RIOLU_OPENCODE_PASSWORD_FILE", ""),
        harness_cwd=_string_env(
            "RIOLU_HARNESS_CWD", _string(agent, "workspace", str(Path.home()))
        ),
        agent_url=_string_env("RIOLU_AGENT_URL", "http://127.0.0.1:8302"),
        agent_host=_string_env("RIOLU_AGENT_HOST", "127.0.0.1"),
        agent_port=max(1, _int_env("RIOLU_AGENT_PORT", 8302)),
        agent_token=_string_env(
            "RIOLU_AGENT_TOKEN", _string_env("RIOLU_MCP_TOKEN", "")
        ),
        harness_chat_ids=frozenset(
            _int_list_env("RIOLU_HARNESS_CHAT_IDS", _integer_list(agent, "chat_ids"))
        ),
        daily_news_chat_id=_optional_int_env(
            "DAILY_NEWS_CHAT_ID", _optional_integer(telegram, "daily_news_chat_id")
        ),
        daily_news_thread_id=_optional_int_env(
            "DAILY_NEWS_THREAD_ID", _optional_integer(telegram, "daily_news_thread_id")
        ),
    )


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read configuration: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Configuration must be a JSON object: {path}")
    return value


def _object(value: dict[str, Any], key: str) -> dict[str, Any]:
    child = value.get(key, {})
    if not isinstance(child, dict):
        raise RuntimeError(f"{key} must be a JSON object")
    return child


def _string(value: dict[str, Any], key: str, default: str) -> str:
    raw = value.get(key, default)
    if not isinstance(raw, str):
        raise RuntimeError(f"{key} must be a string")
    return raw.strip()


def _integer(value: dict[str, Any], key: str, default: int) -> int:
    raw = value.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise RuntimeError(f"{key} must be an integer")
    return raw


def _optional_integer(value: dict[str, Any], key: str) -> int | None:
    raw = value.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise RuntimeError(f"{key} must be an integer or null")
    return raw


def _number(value: dict[str, Any], key: str, default: float) -> float:
    raw = value.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise RuntimeError(f"{key} must be a number")
    return float(raw)


def _string_list(
    value: dict[str, Any], key: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    raw = value.get(key)
    if raw is None:
        return default
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"{key} must be a list of strings")
    return tuple(item.strip() for item in raw if item.strip())


def _integer_list(value: dict[str, Any], key: str) -> tuple[int, ...]:
    raw = value.get(key, [])
    if not isinstance(raw, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in raw
    ):
        raise RuntimeError(f"{key} must be a list of integers")
    return tuple(raw)


def _dojo_tools(value: dict[str, Any]) -> tuple[DojoToolConfig, ...]:
    raw = value.get("tools", [])
    if not isinstance(raw, list):
        raise RuntimeError("tools must be a list")
    tools: list[DojoToolConfig] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("Each Dojo tool must be an object")
        name = _string(item, "name", "")
        kind = _string(item, "kind", "")
        target = _string(item, "target", "")
        port = _integer(item, "port", 0)
        url = _string(item, "url", "")
        if not name or kind not in {"docker", "systemd"} or not target:
            raise RuntimeError("Dojo tools require name, docker/systemd kind, and target")
        if not url and not 1 <= port <= 65535:
            raise RuntimeError(f"Dojo tool {name} requires a valid port or URL")
        tools.append(DojoToolConfig(name, kind, target, port, url))
    return tuple(tools)


def _string_env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _source_ids_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or not value.strip():
        values = default
    else:
        values = tuple(part.strip() for part in value.split(",") if part.strip())
    return tuple(dict.fromkeys(normalize_source_token(part) for part in values))


def _int_list_env(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    result: list[int] = []
    for part in value.split(","):
        text = part.strip()
        if not text:
            continue
        try:
            result.append(int(text))
        except ValueError as exc:
            raise RuntimeError(f"{name} contains a non-integer chat id: {text}") from exc
    return tuple(result)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _optional_int_env(name: str, default: int | None) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
