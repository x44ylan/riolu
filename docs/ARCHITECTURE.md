# Architecture

Riolu uses one Telegram-facing process and one optional OpenCode process.

```text
Telegram ──► riolu ──► features ──► private source plugins / host / state
               │
               ├──► local webhook and MCP delivery
               │
               └──► riolu-agent ──► OpenCode
```

## Boundaries

- `bot.py` owns polling, authorization, command routing, daily scheduling, and Telegram delivery.
- `features/` owns user-facing behavior without knowing how polling works.
- `source.py` defines the source contract; `source_registry.py` loads private plugins from the local configuration directory.
- `ui/` owns Telegram-safe rendering, screens, and controls.
- `agent/` owns OpenCode sessions, topic controls, the loopback client/server, and MCP tools.
- `state.py` serializes local durable state with atomic file replacement.
- `config.py` merges publish-safe JSON behavior with environment-held secrets.

The main process never owns an OpenCode connection. It makes authenticated loopback requests to `riolu-agent`, which contains the long-running agent integration and can be restarted independently.

## Configuration flow

```text
config/*.example.json ──copy──► local config directory
                                      │
.env / service environment ───────────┤
                                      ▼
                                Settings object
```

JSON provides normal application behavior. Environment variables override it for compatibility and keep tokens, passwords, and service wiring out of version control.

Source implementations are Python plugins stored outside the repository. Each file or package exports a `SOURCE` object. Package-relative imports are supported, and no bundled adapters are required or published.

## Daily delivery semantics

The scheduler uses the configured IANA timezone and hour. A successful daily run records its local date. If the process starts later that day without the record, it schedules a catch-up run.

Each source is isolated. Riolu compares fetched item identities with the per-chat, per-source checkpoint. Messages are split into Telegram-sized batches, and identities are marked seen only after Telegram accepts that batch. A source or delivery failure therefore leaves unsent items eligible for the next run.

## Agent topic ownership

One Telegram topic owns one OpenCode session. Imported sessions are forked; resumed sessions are attached without forking only when explicitly requested. Before Riolu adopts a legacy session with unclear ownership, it forks once and records `telegram-worker` ownership. This prevents two clients from writing to the same OpenCode session.

The topic remains responsive while a turn runs: interrupts and approval replies use a separate urgent path, while additional prompts enter a bounded queue.

## Local integration endpoints

The optional HTTP listener supports generic events, concise agent messages, artifacts, questions, and answer polling. Requests are authenticated and size-limited. Delivery is deduplicated by caller-supplied keys. Files are transferred in-memory; the MCP sender deletes local artifacts after successful delivery by default.
