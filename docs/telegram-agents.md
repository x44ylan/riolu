# Telegram and OpenCode

Riolu treats Telegram forum topics as a focused remote UI for OpenCode.

## Processes

- `riolu.service` owns Telegram polling, topic routing, and rendering.
- `riolu-agent.service` is the only Riolu process that talks to OpenCode.
- Their authenticated loopback API is configured with `RIOLU_AGENT_URL` and `RIOLU_AGENT_TOKEN`.

This separation lets the Telegram bot continue serving news and notes if OpenCode is unavailable, and lets the agent bridge restart without replacing the polling process.

## Starting and attaching

Run `/opencode` in an allowlisted forum group. Riolu creates a topic and a native OpenCode session. The optional forms are:

```text
/opencode new [prompt]
/opencode resume <session-id-or-title>
/opencode import <session-id-or-title>
```

Inside a topic:

```text
/new [prompt]
/resume [session-id-or-title]
/import <session-id-or-title>
/fork [session-id]
```

Bare `/resume` opens a paginated picker of unattached sessions. It also works from the group outside an OpenCode topic; choosing a session creates its topic. Supplying an ID or title still works directly. `import` and `fork` create a native fork. Riolu refuses to attach a session already owned by another live Telegram topic.

## Prompt behavior

Plain text in the topic is a prompt. Riolu admits every prompt directly to OpenCode’s native durable queue with `delivery: "queue"`; OpenCode owns ordering, persistence, and sequential execution. `/queue` shows the active prompt plus OpenCode’s native inbox, and `/queue clear` cancels undelivered items. OpenCode drains the queue automatically, so there is no `/queue run`. `/steer <prompt>` sends guidance directly into the active run. `/interrupt` cancels queued items, aborts the active run, and preserves the session.

Every accepted prompt ends with one terminal notice: `opencode: completed`, `opencode: failed`, `opencode: interrupted`, or `opencode: timed out`. Queued prompts receive `/queue: position N` when accepted and their own terminal notice after they run.

Before admission, Riolu checks the workspace's model catalog and applies the topic's model, effort, and agent to the native session. An unavailable model or effort is rejected before the prompt enters the inbox. Settings selected in the native OpenCode UI are reflected in the Telegram panel.

Transient reads retry briefly. Prompt writes are never automatically resent because a disconnected response can still mean OpenCode accepted the prompt. Riolu retains its recovery anchor and checks the native inbox and messages after reconnecting; use `/queue` before manually resending.

If a native session has been deleted, Riolu preserves its topic and session ID, stops polling it, and marks it unavailable. Use `/resume` to attach an existing saved session in a new topic or `/new` to start one. The missing session's history is not recreated automatically.

OpenCode permission requests and questions appear as Telegram controls. Replies are scoped to the originating topic and expire when the run ends.

### Returning files

OpenCode prompts include lightweight Riolu harness context. To return a local file directly in the same Telegram topic, include an artifact marker in the final response:

```text
@riolu-artifact /absolute/path/to/file.png optional caption
```

Riolu removes the marker, validates the file, and sends common images inline or other files as documents. Paths may be quoted to preserve spaces. The current limits are 1 byte to 10 MB, and credential-looking filenames are rejected.

## Settings and tools

```text
/model       choose a connected provider model
/effort      choose reasoning effort
/agent       choose an OpenCode agent
/clear       delete and recreate this topic; keep its session
/steer       guide the active run
/status      show native session details
/delete      delete the topic and its OpenCode session
/rename      rename the session and topic
/compact     summarize the session
/diff        show changes
/undo        revert the latest user turn
/redo        undo a revert
/export      export messages
/sessions    list sessions
/usage       show the full all-providers usage view
```

The allowlisted commands above are registered as the group's native Telegram slash-command menu. Riolu's normal command menu remains the default for other chats. Commands outside the explicit OpenCode allowlist—including custom, raw API, RPC, and shell commands—are rejected in Telegram. `/usage` is a built-in wrapper for the `opencode-usage-tracker` plugin and always shows the full all-providers view.

## Security model

Agent execution is more privileged than reading news. `agent.chat_ids` is therefore a dedicated allowlist and does not inherit general Telegram destinations. Use a private forum group or another tightly controlled supergroup. The news broadcast topic can be in a different chat or topic.

## Integration verification

Run `scripts/opencode-e2e.py` with the deployment's Python environment from the directory containing its `.env`. It starts an isolated Riolu worker, exercises disposable sessions on the configured OpenCode server, records Telegram output locally, and removes those sessions afterward. Set `RIOLU_E2E_MODEL` to a connected model with a `low` effort variant and `RIOLU_E2E_REPORT` to the desired JSON report path. It checks settings, queued delivery across harness restarts, missing sessions, unavailable models, transient HTTP failures, authentication, and failed turns followed by successful turns. No messages are sent to Telegram.
