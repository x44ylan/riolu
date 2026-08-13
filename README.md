# riolu

Riolu is a private Telegram information PA. It starts as a clean source aggregator and adds personal assistant primitives around it:

- modular information sources
- scheduled Telegram digests
- custom RSS subscriptions per chat
- one-off reminders
- compact Telegram HTML cards

## Built-In Sources

Each built-in source lives in its own folder under `riolu/sources/<source_id>/source.py`.

| Source | Command | Coverage |
| --- | --- | --- |
| HelloGitHub | `/hellogithub` | Curated GitHub projects via `https://hellogithub.com/rss` |
| Talkback | `/talkback` | Security research and vulnerability links |
| LoL Esports | `/lol` | LCK, LCS, MSI, Worlds, and international League of Legends esports |
| Opencode Releases | `/opencode` | GitHub release notes for `anomalyco/opencode` |
| Cyber News | `/cyber` | The Hacker News, Krebs on Security, and BleepingComputer |
| Daily Tech Learning | `/tech` | Hacker News, Lobsters, and freeCodeCamp learning feeds |

## Setup

macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

Set `TELEGRAM_TOKEN` in `.env` using the token Telegram gives you, then send `/start` to the bot in Telegram. Riolu stores that chat automatically and can send scheduled digests there without a manual chat ID.

To restrict access, set `ALLOWED_CHAT_IDS` to a comma-separated list of Telegram chat IDs.

## Run

```bash
riolu
```

You can also run `python -m riolu`. Stop the bot with `Ctrl+C`; in-flight background loops are cancelled cleanly.

Riolu registers its command menu with Telegram on startup. Send `/start` for the interactive home screen; its buttons browse sources, fetch digests, and open feed or reminder views without adding extra navigation messages to the chat.

## Automation

Set these in `.env`:

```bash
TARGET_CHAT_ID=
POST_INTERVAL_MINUTES=60
STATE_PATH=.riolu-state.json
TIMEZONE=Asia/Singapore
DEFAULT_SOURCE_IDS=hello_github,talkback
```

Riolu posts new built-in source items on startup and then every `POST_INTERVAL_MINUTES` to chats that have sent the bot a command. Set `TARGET_CHAT_ID` only if you want to force automated built-in digests to a specific chat instead. Items are marked seen only after Telegram accepts the digest.

Custom RSS subscriptions are stored per chat and are also checked by the periodic feed loop.

## Telegram Commands

Every action remains available as a command, even when an inline button offers the same shortcut.

Read sources:

```text
/start
/help
/sources
/latest [source] [limit]
/hellogithub [limit]
/talkback [limit]
/lol [limit]
/opencode [limit]
/cyber [limit]
/tech [limit]
```

Subscribe to custom RSS/Atom feeds:

```text
/subscribe https://example.com/feed.xml Example Feed
/subscriptions
/unsubscribe rss_abc123def0
```

Set reminders:

```text
/remind in 30m Stand up
/remind 09:00 Daily review
/remind tomorrow 09:00 Check LCK schedule
/remind 2026-06-25 21:30 Ship riolu
/reminders
/forget rem_abc123def0
```

`limit` defaults to `DEFAULT_LIMIT` and is capped by `MAX_LIMIT`.

## Test

The regression suite uses Python's standard library test runner:

```bash
python -m unittest discover -v
```

## Architecture

See `docs/ARCHITECTURE.md` for the project plan, source folder contract, and QA checklist.
