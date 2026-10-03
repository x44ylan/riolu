from __future__ import annotations

import asyncio
import logging

from riolu.bot import RioluBot
from riolu.config import load_settings


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    # httpx logs complete request URLs, which include the Telegram bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = load_settings()
    except RuntimeError as exc:
        logging.getLogger(__name__).error("Configuration error: %s", exc)
        raise SystemExit(2) from None

    try:
        asyncio.run(RioluBot(settings).run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Riolu stopped")
