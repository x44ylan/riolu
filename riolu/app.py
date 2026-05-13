from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

import httpx

from riolu.config import Settings, load_settings
from riolu.scrapers import IntelItem, scrape_hellogithub, scrape_talkback


Scraper = Callable[[int, float], Awaitable[list[IntelItem]]]

LOGGER = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    asyncio.run(run(load_settings()))


async def run(settings: Settings) -> None:
    timeout = httpx.Timeout(settings.http_timeout_seconds, read=50.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        offset = 0
        LOGGER.info("Riolu is polling Telegram")
        while True:
            try:
                updates = await _request(
                    client,
                    settings,
                    "getUpdates",
                    {"offset": offset, "timeout": 45, "allowed_updates": ["message"]},
                )
            except Exception:
                LOGGER.exception("Telegram polling failed")
                await asyncio.sleep(5)
                continue

            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                await _handle_update(client, settings, update)


async def _handle_update(client: httpx.AsyncClient, settings: Settings, update: dict[str, Any]) -> None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()

    if chat_id is None or not text.startswith("/"):
        return

    if settings.allowed_chat_ids and chat_id not in settings.allowed_chat_ids:
        LOGGER.warning("Rejected chat_id=%s", chat_id)
        await _send(client, settings, chat_id, "Riolu is private.")
        return

    command, args = _parse_command(text)
    if command in {"start", "help"}:
        await _send(client, settings, chat_id, _help_text())
    elif command == "latest":
        await _latest(client, settings, chat_id, args)
    elif command == "hellogithub":
        await _scrape_command(client, settings, chat_id, args, "HelloGitHub", scrape_hellogithub)
    elif command == "talkback":
        await _scrape_command(client, settings, chat_id, args, "Talkback", scrape_talkback)


async def _latest(
    client: httpx.AsyncClient,
    settings: Settings,
    chat_id: int,
    args: list[str],
) -> None:
    limit = _limit(args, settings)
    await _send(client, settings, chat_id, "Scraping both sources...")

    results = await asyncio.gather(
        scrape_hellogithub(limit, settings.http_timeout_seconds),
        scrape_talkback(limit, settings.http_timeout_seconds),
        return_exceptions=True,
    )
    await _send(client, settings, chat_id, _format_combined(results))


async def _scrape_command(
    client: httpx.AsyncClient,
    settings: Settings,
    chat_id: int,
    args: list[str],
    source: str,
    scraper: Scraper,
) -> None:
    limit = _limit(args, settings)
    await _send(client, settings, chat_id, f"Scraping {escape(source)}...")

    try:
        items = await scraper(limit, settings.http_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - user-facing Telegram error boundary
        LOGGER.exception("Failed to scrape %s", source)
        await _send(client, settings, chat_id, f"Failed to scrape {escape(source)}: <code>{escape(str(exc))}</code>")
        return

    await _send(client, settings, chat_id, _format_items(source, items))


def _parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.split()
    command = parts[0].split("@", 1)[0].removeprefix("/").lower()
    return command, parts[1:]


def _limit(args: list[str], settings: Settings) -> int:
    if not args:
        return settings.default_limit
    try:
        return max(1, min(int(args[0]), 10))
    except ValueError:
        return settings.default_limit


def _help_text() -> str:
    return (
        "<b>riolu</b> intel\n\n"
        "Commands:\n"
        "/latest [limit] - scrape both sources\n"
        "/hellogithub [limit] - scrape HelloGitHub\n"
        "/talkback [limit] - scrape Talkback"
    )


def _format_combined(results: list[list[IntelItem] | BaseException]) -> str:
    sections: list[str] = []
    for source, result in zip(("HelloGitHub", "Talkback"), results, strict=True):
        if isinstance(result, BaseException):
            sections.append(f"<b>{escape(source)}</b>\nFailed: <code>{escape(str(result))}</code>")
        else:
            sections.append(_format_items(source, result))
    return "\n\n".join(sections)


def _format_items(source: str, items: list[IntelItem]) -> str:
    if not items:
        return f"<b>{escape(source)}</b>\nNo items found."

    lines = [f"<b>{escape(source)}</b>"]
    for index, item in enumerate(items, start=1):
        lines.append(f'{index}. <a href="{escape(item.url)}">{escape(item.title)}</a>')
        if item.summary:
            lines.append(escape(item.summary))
    return _truncate("\n".join(lines))


async def _send(client: httpx.AsyncClient, settings: Settings, chat_id: int, text: str) -> None:
    await _request(
        client,
        settings,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": _truncate(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


async def _request(
    client: httpx.AsyncClient,
    settings: Settings,
    method: str,
    payload: dict[str, Any],
) -> Any:
    response = await client.post(_telegram_url(settings.telegram_token, method), json=payload)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram request failed"))
    return data.get("result")


def _telegram_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _truncate(text: str) -> str:
    return text if len(text) <= 3900 else f"{text[:3890]}\n..."
