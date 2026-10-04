# Riolu

A self-hosted Telegram assistant for news, notes, system stats, and OpenCode sessions.

- Fetch news from your own source plugins and send daily digests.
- Save and manage named notes.
- View system stats on the home screen and open tools from Links.
- Run OpenCode sessions in Telegram forum topics.
- Connect local agents through optional webhooks and MCP.

News adapters are kept locally; none are bundled with this repository.

## Setup

Requires Python 3.11+ and a Telegram bot token.

```bash
git clone https://github.com/x44ylan/riolu.git
cd riolu
python3 -m venv .venv
.venv/bin/pip install -e .

mkdir -p ~/.config/riolu/sources
cp config/riolu.example.json ~/.config/riolu/riolu.json
cp config/sources.example.json ~/.config/riolu/sources.json
cp config/dojo.example.json ~/.config/riolu/dojo.json
cp .env.example .env
```

Set `TELEGRAM_TOKEN` in `.env` and your permitted Telegram chat IDs in `riolu.json`. Then start the bot:

```bash
.venv/bin/riolu
```

## Configuration

Configuration lives in `~/.config/riolu`. Use `RIOLU_CONFIG_DIR` to change it.

| File | Controls |
| --- | --- |
| `riolu.json` | Chat access, digest schedule and destination, limits, and agent workspace |
| `sources.json` | Source directory and enabled, default, and automated sources |
| `dojo.json` | Tool links and service settings |
| `.env` | Tokens, passwords, and connection URLs |

Environment variables override JSON settings. Keep credentials, local state, and source adapters out of Git.

### News sources

Add trusted Python files or packages to `~/.config/riolu/sources`. Each exports a `SOURCE` object following the [source contract](riolu/source.py): metadata and an async `fetch(limit, client)` method returning `IntelItem` objects. RSS helpers are available in the same module.

In `sources.json`, `enabled` chooses which plugins load, `default` selects sources for `/latest all`, and `automated` selects the daily digest. An empty `enabled` list loads every local plugin.

Set the digest timezone, hour, and Telegram destination in `riolu.json`. Missed digests catch up after startup; failed runs retry with increasing delays while retaining successful delivery checkpoints.

### OpenCode

Run an OpenCode server, set its URL and credentials in `.env`, and set `RIOLU_AGENT_TOKEN` for the local bridge. Add your Telegram forum group to `agent.chat_ids` in `riolu.json`.

Run the bridge alongside the bot in a separate terminal:

```bash
.venv/bin/riolu-agent
```

Use `/opencode` in the allowed group to start a session. See the [Telegram agent guide](docs/telegram-agents.md) for session controls.

## Commands

| Command | Action |
| --- | --- |
| `/start` | Home screen with system stats and refresh |
| `/latest` | Browse news sources |
| `/latest all [limit]` | Fetch default sources |
| `/note`, `/notes` | Create and manage notes |
| `/dojo` | Open Links |
| `/opencode` | Start an OpenCode topic |
| `/clear` | Clear recent bot messages |
| `/help` | Usage help |

## Running and debugging

[Systemd templates](deploy/) are provided for the bot and agent bridge. Follow their logs with:

```bash
journalctl -u riolu.service -u riolu-agent.service -f
```

Digest logs include the source, failure phase, error type, timing, item counts, and retry delay.

Run the local end-to-end checks:

```bash
.venv/bin/python scripts/riolu-e2e.py
```

These use temporary state and local HTTP fixtures without sending Telegram messages. Results are saved to `riolu-e2e-report.json`.

See [Architecture](docs/ARCHITECTURE.md) for the code layout and integration details.
