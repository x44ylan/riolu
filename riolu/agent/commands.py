"""The explicit Telegram command surface for OpenCode topics."""

from __future__ import annotations


TELEGRAM_COMMANDS: tuple[dict[str, str], ...] = (
    {"command": "agent", "description": "Choose an OpenCode agent"},
    {"command": "clear", "description": "Clear chat history"},
    {"command": "compact", "description": "Compact the current session"},
    {"command": "delete", "description": "Delete this topic and its session"},
    {"command": "diff", "description": "Show session changes"},
    {"command": "effort", "description": "Choose reasoning effort"},
    {"command": "export", "description": "Export session messages"},
    {"command": "fork", "description": "Fork the current session"},
    {"command": "help", "description": "Show OpenCode help"},
    {"command": "import", "description": "Fork and attach another session"},
    {"command": "interrupt", "description": "Stop the active run"},
    {"command": "model", "description": "Choose a model"},
    {"command": "new", "description": "Start a new OpenCode topic"},
    {"command": "opencode", "description": "Start a new OpenCode topic"},
    {"command": "queue", "description": "Inspect OpenCode’s native queue"},
    {"command": "redo", "description": "Redo the reverted turn"},
    {"command": "rename", "description": "Rename this session and topic"},
    {"command": "resume", "description": "Pick a session to resume"},
    {"command": "sessions", "description": "List native sessions"},
    {"command": "status", "description": "Show current session details"},
    {"command": "steer", "description": "Guide the active run"},
    {"command": "undo", "description": "Undo the latest user turn"},
    {"command": "usage", "description": "Show full provider usage"},
)


# Includes aliases and contextual commands that remain valid when typed or
# selected from an inline control, without advertising them in Telegram's menu.
ALLOWED_COMMANDS = frozenset(
    {
        *(item["command"] for item in TELEGRAM_COMMANDS),
        "agents",
        "answer",
        "approve",
        "clear",
        "commands",
        "deny",
        "models",
        "stop",
        "summarize",
    }
)
