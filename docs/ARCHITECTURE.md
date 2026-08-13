# Riolu Architecture

## Product Direction

Riolu is a private Telegram information PA. The first milestone is source intelligence: collect useful information, render it cleanly, dedupe it, and deliver it on demand or on a schedule. The second milestone is assistant behavior: reminders, subscriptions, preference tracking, summaries, and eventually topic-aware routing.

## Current Modules

```text
riolu/
  app.py              # CLI entrypoint only
  bot.py              # Telegram command router and background loops
  config.py           # .env parsing and runtime settings
  feeds.py            # RSS/Atom parser
  models.py           # shared domain models
  reminders.py        # reminder time parser
  rendering.py        # Telegram HTML cards
  state.py            # atomic JSON state store
  telegram.py         # Telegram Bot API gateway
  text.py             # text cleanup helpers
  sources/
    <source_id>/
      source.py       # exports SOURCE
```

## Source Folder Contract

Every built-in information source must have this shape:

```text
riolu/sources/<source_id>/
  __init__.py
  source.py
```

`source.py` exports `SOURCE`, and `SOURCE` must expose:

- `id`: stable machine id, snake_case
- `name`: user-facing title
- `category`: broad topic bucket
- `description`: one-line purpose
- `aliases`: command/search aliases
- `fetch(limit, client)`: async method returning `list[IntelItem]`

RSS-backed sources should use `RssBundleSource`. Custom HTML/API sources can implement the same protocol directly.

## Built-In Source Strategy

- `hello_github`: use `https://hellogithub.com/rss`.
- `talkback`: custom HTML collector because the site does not expose a stable RSS feed in this project.
- `lol_esports`: combine official League of Legends news pages with `r/lolesports` Atom and filter for LCK, LCS, MSI, Worlds, and international signals.
- `opencode`: use GitHub release Atom at `https://github.com/anomalyco/opencode/releases.atom`.
- `cyber`: use The Hacker News, Krebs on Security, and BleepingComputer RSS.
- `tech`: use HN frontpage, Lobsters, and freeCodeCamp RSS as a practical "learn one technical thing today" stream.

## State Model

State lives in one JSON file, default `.riolu-state.json`.

```json
{
  "version": 2,
  "chats": [123456789],
  "seen": {
    "123456789": {
      "cyber": ["https://example.com/item"]
    }
  },
  "subscriptions": {
    "123456789": [
      {"id": "rss_...", "title": "Feed", "url": "https://example.com/rss"}
    ]
  },
  "reminders": [
    {"id": "rem_...", "chat_id": 123456789, "text": "Ship", "due_at": "..."}
  ]
}
```

Chats are remembered after an allowed command, which lets scheduled default-source digests work without manually configuring a target chat ID. Seen items are marked only after Telegram accepts the digest. Writes are atomic through a temporary file and replace.

## Message Design

Telegram messages use simple HTML:

- compact icon and bold title header
- one `<blockquote>` per card
- short summary
- source/date/tags metadata
- one `Read →` link
- inline keyboards for home, source, and digest navigation

The renderer adds cards until it approaches Telegram's message limit, then stops without cutting through HTML tags.
Button callbacks edit the current bot message, so navigation and loading states do not flood the chat. Plain-text commands use the same dispatch path and remain fully supported.

## QA Checklist

Run before shipping meaningful changes:

```bash
python -m compileall riolu
python - <<'PY'
import asyncio
import httpx
from riolu.sources.registry import SourceRegistry

async def main():
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        for source in SourceRegistry().all():
            items = await source.fetch(1, client)
            print(source.id, len(items), items[0].title if items else "-")

asyncio.run(main())
PY
```

Manual QA:

- `/sources` shows all built-in sources.
- `/latest cyber 3` renders clean cards.
- `/subscribe <feed_url> <name>` validates and stores a feed.
- `/unsubscribe <id>` removes that feed.
- `/remind in 1m Test` sends once and then disappears from `/reminders`.
