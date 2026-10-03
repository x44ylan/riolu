# Riolu

Riolu is a self-hosted Telegram assistant for private news sources, notes, host monitoring, and OpenCode sessions. Source adapters and deployment choices stay outside the repository.

> Riolu ships without news-source adapters. Add your own trusted Python plugins locally; they are ignored by Git and never need to enter the public repository.

## What it does

- Loads your own news adapters from a private local plugin directory.
- Posts a daily catch-up digest at a configurable local hour. Each successfully delivered item is checkpointed, so a failed batch resumes from the last sent item.
- Keeps prompts and OpenCode controls inside Telegram forum topics.
- Stores lightweight notes and bot state in one local JSON file.
- Shows configured systemd services, Docker tools, and Tailscale peers in the Dojo menu.
- Exposes optional local endpoints and an MCP server for agent notifications and artifacts.

News broadcasts and OpenCode prompts have separate destinations: scheduled news can target one forum topic, while prompting happens only in an allowlisted forum group.

## Layout

```text
.
├── riolu/
│   ├── agent/             OpenCode bridge, Telegram topic UI, MCP, and riolu-agent
│   ├── features/          news, notes, and Dojo behavior
│   ├── ui/                Telegram screens, rendering, styling, and text conversion
│   ├── bot.py             Telegram routing and daily scheduler
│   ├── config.py          environment and JSON configuration loader
│   ├── feed.py            optional RSS/Atom helper for private plugins
│   ├── source.py          public source contract and shared helpers
│   ├── source_registry.py private plugin discovery and loading
│   ├── state.py           durable local state
│   ├── telegram.py        Telegram Bot API client
│   └── webhook.py         authenticated local HTTP endpoints
├── config/                publish-safe configuration examples
├── deploy/                generic systemd unit examples
└── docs/                  architecture and Telegram agent documentation
```

Source implementations do not belong in the repository. They are loaded from `~/.config/riolu/sources` by default. Bot capabilities belong in `riolu/features/`, with presentation in `riolu/ui/`.

## Install

Requirements:

- Python 3.11+
- a Telegram bot token
- OpenCode, only if agent topics are enabled
- Docker, systemd, or Tailscale only for the corresponding Dojo cards

```bash
git clone https://github.com/Dylan-Liew/riolu.git
cd riolu
python -m venv .venv
.venv/bin/pip install -e .
mkdir -p ~/.config/riolu/sources
cp config/*.example.json ~/.config/riolu/
mv ~/.config/riolu/riolu.example.json ~/.config/riolu/riolu.json
mv ~/.config/riolu/sources.example.json ~/.config/riolu/sources.json
mv ~/.config/riolu/dojo.example.json ~/.config/riolu/dojo.json
cp .env.example .env
```

Set `TELEGRAM_TOKEN` and the local connection secrets in `.env`. Edit the JSON files under `~/.config/riolu` for behavior and deployment-specific values.

Run the components with:

```bash
.venv/bin/riolu-agent
.venv/bin/riolu
```

`riolu-agent` owns the OpenCode connection. The main `riolu` process owns Telegram polling, features, scheduling, and delivery. See [deploy/riolu-agent.service.example](deploy/riolu-agent.service.example) and [deploy/riolu.service.example](deploy/riolu.service.example) for systemd templates.

## Configuration

Riolu reads JSON from `~/.config/riolu` by default. Set `RIOLU_CONFIG_DIR` to use another directory.

| File | Purpose |
| --- | --- |
| `riolu.json` | Telegram destinations, schedule, limits, state path, agent workspace and chat allowlist |
| `sources.json` | private plugin directory plus enabled, default, and automated source IDs |
| `dojo.json` | systemd services and Docker/systemd tool links |
| `.env` | tokens, passwords, listener addresses, and connection URLs |

Environment variables remain available as deployment overrides. The most important are:

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_TOKEN` | Telegram bot token; required |
| `RIOLU_CONFIG_DIR` | JSON configuration directory |
| `RIOLU_AGENT_TOKEN` | shared loopback token between Riolu and `riolu-agent` |
| `RIOLU_OPENCODE_URL` | OpenCode server URL |
| `RIOLU_OPENCODE_PASSWORD` | OpenCode server password |
| `HOOK_TOKEN` | optional generic event endpoint token |
| `RIOLU_MCP_TOKEN` | optional MCP and agent-message endpoint token |

Values still present in the environment override JSON, which makes upgrades compatible with older installations.

### Daily news

Configure the schedule and destination in `riolu.json`:

```json
{
  "schedule": {
    "timezone": "UTC",
    "hour": 8
  },
  "telegram": {
    "daily_news_chat_id": -1001234567890,
    "daily_news_thread_id": 63
  }
}
```

The automated list comes from `sources.json`. When Riolu starts after the scheduled hour, it performs one catch-up run if today has not completed. It fetches up to the configured maximum and sends every unseen item; only accepted Telegram batches are marked sent.

When Telegram upgrades an authorized group, Riolu follows the reported migration for general commands and deliveries. Notes and delivery checkpoints remain available after restart. Existing message IDs stay associated with the original chat. OpenCode's separate chat allowlist still requires explicit configuration.

### OpenCode topics

Put the forum group ID in `agent.chat_ids`. The group receives a dedicated, alphabetized OpenCode slash-command menu while private chats retain Riolu's normal commands. Use `/opencode` to create a topic. Use bare `/resume` from the group or an OpenCode topic to pick an unattached native session; `/import` and `/fork` create copies. Every normal message inside an OpenCode topic is sent as a prompt.

See [docs/telegram-agents.md](docs/telegram-agents.md) for the command model and ownership rules.

### Dojo

Dojo has no built-in hostnames or service names. Define them locally in `dojo.json`:

```json
{
  "services": ["riolu.service", "riolu-agent.service"],
  "tools": [
    {
      "name": "Docs",
      "kind": "docker",
      "target": "docs",
      "url": "https://docs.example.com"
    }
  ]
}
```

A tool may use an explicit `url`, or a `port`; port-based links use the machine's Tailscale MagicDNS hostname. Dojo entries are displayed alphabetically.

## Telegram commands

- `/latest` — choose a news source
- `/latest all [limit]` — fetch the configured default sources
- `/note name` and `/notes` — manage notes
- `/dojo` — host health and configured tools
- `/opencode` — create or attach an OpenCode topic
- `/clear` — remove recent bot messages tracked in the current chat
- `/help` — usage help

Source aliases are registered by each plugin, so loaded sources can also have direct commands. Riolu builds the Telegram command menu from the loaded plugin IDs.

A loaded source with the `ctftime` ID is exposed separately as `/ctftime`, with its ongoing and upcoming views kept out of the general News chooser.

## Verification

Run the local end-to-end checks in Riolu's Python environment:

```bash
.venv/bin/python scripts/riolu-e2e.py
```

The checks use temporary state, temporary source plugins, and loopback HTTP servers. They exercise commands, callbacks, news checkpoints, scheduling, output rendering, delivery retries, questions, and artifacts without contacting Telegram or changing deployment state. Results are saved to `riolu-e2e-report.json`; set `RIOLU_E2E_REPORT` to choose another destination.

`scripts/opencode-e2e.py` separately exercises disposable sessions against a live OpenCode server. Run it with the deployment's OpenCode URL and credentials in the environment; `RIOLU_E2E_MODEL` selects the model. Telegram output is recorded locally.

## Adding a source

Create either a Python file or package under `~/.config/riolu/sources`. It must export a `SOURCE` object with `id`, `name`, `category`, `description`, `aliases`, and an asynchronous `fetch(limit, client)` method:

```python
from dataclasses import dataclass

from riolu.models import IntelItem


@dataclass(frozen=True)
class MySource:
    id: str = "my_source"
    name: str = "My Source"
    category: str = "news"
    description: str = "My private news adapter."
    aliases: tuple[str, ...] = ("mine",)

    async def fetch(self, limit, client):
        response = await client.get("https://example.com/api")
        response.raise_for_status()
        return [
            IntelItem(
                source_id=self.id,
                source_name=self.name,
                title=item["title"],
                url=item["url"],
                category=self.category,
            )
            for item in response.json()[:limit]
        ]


SOURCE = MySource()
```

Packages may use relative imports and install their own dependencies. `riolu.source` provides RSS bundle helpers, while `riolu.feed` provides lower-level RSS/Atom parsing. Plugins are executable Python code, so only install sources you trust.

Configure their roles in `sources.json`:

```json
{
  "directory": "~/.config/riolu/sources",
  "enabled": ["my_source"],
  "default": ["my_source"],
  "automated": ["my_source"]
}
```

`enabled` controls what Riolu loads, `default` controls `/latest all`, and `automated` controls the daily digest. Source IDs use the same case, space, and hyphen normalization as commands; repeated defaults are fetched once. Missing configured sources fail at startup. Leaving `enabled` empty loads every plugin found in the directory. Riolu contains no bundled adapters.

## Public-repository safety

`.env`, state files, virtual environments, caches, local source folders, and non-example JSON files under `config/` are ignored. The committed deployment units are templates and contain no user, hostname, chat ID, source adapter, or secret.

Before publishing an existing repository, also inspect its Git history. Removing a value from the current tree does not erase it from earlier commits; create a clean public history or deliberately rewrite history if private values were previously committed.
