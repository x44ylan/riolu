from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_chat_ids: frozenset[int]
    default_limit: int
    http_timeout_seconds: float


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is required")

    allowed_chat_ids = frozenset(
        int(chat_id.strip())
        for chat_id in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
        if chat_id.strip()
    )

    return Settings(
        telegram_token=token,
        allowed_chat_ids=allowed_chat_ids,
        default_limit=max(1, min(_int_env("DEFAULT_LIMIT", 5), 10)),
        http_timeout_seconds=max(1.0, _float_env("HTTP_TIMEOUT_SECONDS", 20.0)),
    )


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return float(value)
