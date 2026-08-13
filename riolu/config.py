from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from riolu.sources.registry import DEFAULT_SOURCE_IDS


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_chat_ids: frozenset[int]
    target_chat_id: int | None
    default_limit: int
    max_limit: int
    post_interval_minutes: int
    state_path: str
    http_timeout_seconds: float
    timezone: ZoneInfo
    default_source_ids: tuple[str, ...]


def load_settings() -> Settings:
    load_dotenv()

    token = _string_env("TELEGRAM_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is required")

    max_limit = max(1, _int_env("MAX_LIMIT", 10))
    return Settings(
        telegram_token=token,
        allowed_chat_ids=frozenset(_int_list_env("ALLOWED_CHAT_IDS")),
        target_chat_id=_optional_int_env("TARGET_CHAT_ID"),
        default_limit=max(1, min(_int_env("DEFAULT_LIMIT", 5), max_limit)),
        max_limit=max_limit,
        post_interval_minutes=max(1, _int_env("POST_INTERVAL_MINUTES", 60)),
        state_path=_string_env("STATE_PATH", ".riolu-state.json") or ".riolu-state.json",
        http_timeout_seconds=max(1.0, _float_env("HTTP_TIMEOUT_SECONDS", 20.0)),
        timezone=_timezone_env("TIMEZONE", "Asia/Singapore"),
        default_source_ids=_source_ids_env("DEFAULT_SOURCE_IDS", DEFAULT_SOURCE_IDS),
    )


def _string_env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _source_ids_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _int_list_env(name: str) -> tuple[int, ...]:
    value = os.getenv(name, "")
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


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
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


def _timezone_env(name: str, default: str) -> ZoneInfo:
    value = _string_env(name, default) or default
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"{name} is not a valid IANA timezone: {value}") from exc
