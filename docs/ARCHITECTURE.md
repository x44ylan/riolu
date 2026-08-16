# Riolu Architecture

## Product Direction

Riolu is a private Telegram information PA. The first milestone is source intelligence: collect useful information, render it cleanly, dedupe it, and deliver it on demand or on a schedule. The second milestone is assistant behavior: reminders, subscriptions, preference tracking, summaries, and eventually topic-aware routing.

## Current Modules

```text
riolu/
  app.py              # CLI entrypoint only
  bot.py              # Telegram transport coordinator and background loops
  config.py           # .env parsing and runtime settings
  feeds.py            # RSS/Atom parser
  models.py           # shared domain models
  reminders.py        # reminder time parser
  host.py             # Linux and Tailscale inspection
  rendering.py        # Telegram HTML cards
  screens.py          # atomic text + keyboard UI values
  state.py            # atomic JSON state store
  telegram.py         # Telegram Bot API gateway
  text.py             # text cleanup helpers
  features/
    cart.py           # shopping-list lifecycle
    dojo.py           # host control-center workflow
    news.py           # source selection and digest workflow
    subscriptions.py  # feed lifecycle and manual fetching
    reminders.py      # one-off and persistent daily task lifecycle
  sources/
    <source_id>/
      source.py       # exports SOURCE
```

## Feature Boundary

`RioluBot` owns Telegram polling, authorization, callback parsing, scheduled loops,
and delivery. It delegates user workflows to feature modules. Each feature accepts
a command and returns a complete `Screen`, which keeps message text and its inline
keyboard together as one value.

This boundary is intentionally deeper than a folder split: news owns source lookup
and fetching, subscriptions own feed validation and lifecycle controls, and reminders
own reminder creation and deletion. Feature tests exercise those interfaces without
starting Telegram polling or constructing update payloads.

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
- `onetracker`: validate and summarize the public OneTracker cybersecurity hub without relying on Bubble's private data APIs.
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
  "cart": {
    "123456789": [{"id": "cart_...", "text": "<item>"}]
  },
  "reminders": [
    {"id": "rem_...", "chat_id": 123456789, "text": "Ship", "due_at": "...", "repeat": "daily"}
  ]
}
```

`repeat` is absent for one-off reminders. Daily reminders advance to their next local-time occurrence only after Telegram accepts the delivery, and remain stored until the user presses Done.

Riolu keeps a rolling list of the latest 100 incoming and outgoing message IDs per chat. `/clear` submits those IDs to Telegram's bulk deletion method; Telegram limits deletion to eligible messages less than 48 hours old.

Each custom subscription also stores `paused` and `keywords`. Existing state files are normalized with safe defaults during loading.

## Comparable Project Research

Riolu borrows focused interaction and reliability patterns from mature open-source projects without adopting their heavier deployment stacks:

| Project | Advantage observed | Riolu integration |
| --- | --- | --- |
| [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot) | Reading-oriented formatting, per-feed customization, media handling, HTTP caching, and OPML portability | Source-aware quick-view facts and per-feed keyword customization; media and OPML remain future options |
| [telegram-robot-rss](https://github.com/cbrgm/telegram-robot-rss) | Small, memorable subscription command set and manual fetch-by-name | `/get <id> [limit]` plus direct pause/resume controls |
| [rss-chan](https://github.com/hyPnOtICDo0g/rss-chan) | Manual item-count fetching and per-feed templates | Manual count-limited fetching; source-specific renderers provide safer structured formatting than user templates |
| [rss-telegram](https://github.com/daquino94/rss-telegram) | Persistent deduplication and grouping by feed source | Existing durable seen-state and grouped Telegram sections retained |
| [feedforbot](https://github.com/shpaker/feedforbot) | Silent first run, flood-wait retry, health checks, and graceful shutdown | Silent subscription baseline and bounded Telegram retry; systemd supplies process health and restart behavior |

The selection rule is deliberate: features must improve a private assistant without adding a database, Redis, Docker, or a separate web control plane.

Chats are remembered after an allowed command, which lets scheduled default-source digests work without manually configuring a target chat ID. Seen items are marked only after Telegram accepts the digest. Writes are atomic through a temporary file and replace.

## Message Design

Telegram messages use simple HTML:

- compact icon and bold title header
- one `<blockquote>` per card
- expanded summary, trimmed only to protect Telegram's message limit
- source/date/tags metadata
- article URL buttons plus 10/20/50/100 result-size controls
- ten-story pagination for larger result sets
- inline keyboards for home, source, and digest navigation
- button-driven feed and reminder management with explicit delete confirmation

The renderer adds cards until it approaches Telegram's message limit, then stops without cutting through HTML tags.
Article buttons retain access to additional fetched stories even when their previews no longer fit in the message.
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
