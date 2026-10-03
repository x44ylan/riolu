from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
from typing import Literal

import httpx
from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.types import ToolAnnotations


INSTRUCTIONS = (
    "Use Riolu only for user-requested updates, meaningful task completion or failure, questions that block work, "
    "and artifacts the user asked to receive. Use ask_user for a decision and check_reply later with its request_id. "
    "Send Markdown reports as .md artifacts so Telegram preserves the source. send_markdown sends only the file and its "
    "source label; do not add other caption text or repeat its contents, path, or link after delivery. Pass Markdown "
    "directly when possible, and delete any temporary local source file after delivery succeeds. For other artifacts, write the useful summary "
    "or requested text directly in the active conversation without repeating its local path or adding another file link. "
    "Combine related updates and avoid routine progress spam. Never send secrets, credentials, private keys, "
    "environment files, or raw sensitive data. Use a stable dedupe_key when one is available."
)

ATTACHMENT_PRESENTATION = (
    "Attachment delivered. Display the useful result text directly; do not repeat a local path or add another file link."
)
QUIET_ATTACHMENT_PRESENTATION = (
    "Attachment delivered with its source label. Delete any temporary local source file now. "
    "Do not add other text or repeat its contents, path, or link."
)

MAX_ARTIFACT_BYTES = 10_000_000
SENSITIVE_FILENAMES = frozenset({".env", "credentials", "credentials.json", "id_rsa", "id_ed25519"})
SENSITIVE_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})

mcp = MCPServer(
    "riolu",
    title="Riolu Telegram",
    description="Send concise task notifications to the configured Riolu Telegram destination.",
    instructions=INSTRUCTIONS,
    version="0.2.0",
)


@dataclass(frozen=True)
class DeliveryConfig:
    endpoint: str
    token: str
    timeout_seconds: float


def load_delivery_config() -> DeliveryConfig:
    load_dotenv(os.getenv("RIOLU_ENV_FILE", ".env"), override=False)
    host = os.getenv("HOOK_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.getenv("HOOK_PORT", "8301").strip() or "8301"
    endpoint = os.getenv("RIOLU_MESSAGE_URL", f"http://{host}:{port}/message").strip()
    token = os.getenv("RIOLU_MCP_TOKEN", "").strip()
    try:
        timeout = max(1.0, float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")))
    except ValueError as exc:
        raise RuntimeError("HTTP_TIMEOUT_SECONDS must be a number") from exc
    if not token:
        raise RuntimeError("RIOLU_MCP_TOKEN is not configured")
    return DeliveryConfig(endpoint=endpoint, token=token, timeout_seconds=timeout)


@mcp.tool(
    title="Send Telegram message",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def send_message(
    message: str,
    title: str = "Agent update",
    urgency: Literal["info", "success", "warning", "error"] = "info",
    source: str = "",
    dedupe_key: str = "",
) -> dict[str, object]:
    """Send a concise Telegram notification through Riolu.

    Use this for requested notifications, actionable blockers, failures, and
    meaningful completion updates. A stable dedupe_key prevents retry duplicates.
    """
    payload = {
        "message": message,
        "title": title,
        "urgency": urgency,
        "source": source,
        "dedupe_key": dedupe_key,
    }
    data = await _call_bridge(payload)
    return {
        "sent": True,
        "delivered": int(data.get("delivered", 0)),
        "skipped": int(data.get("skipped", 0)),
    }


@mcp.tool(
    title="Ask the user on Telegram",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def ask_user(
    question: str,
    choices: list[str] | None = None,
    source: str = "",
    dedupe_key: str = "",
) -> dict[str, object]:
    """Ask the user a blocking question through Telegram.

    Supply up to six choices for buttons, or omit choices to accept a typed reply.
    Call check_reply later with the returned request_id.
    """
    seed = dedupe_key.strip() or secrets.token_hex(16)
    digest = hashlib.sha256(f"{source.strip()}\0{seed}".encode()).hexdigest()[:20]
    request_id = f"ask_{digest}"
    data = await _call_bridge(
        {
            "action": "ask_user",
            "request_id": request_id,
            "question": question,
            "choices": choices or [],
            "source": source,
        },
        agent_action=True,
    )
    return {
        "request_id": request_id,
        "status": str(data.get("status", "pending")),
        "delivered": int(data.get("delivered", 0)),
        "skipped": int(data.get("skipped", 0)),
    }


@mcp.tool(
    title="Check a Telegram reply",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def check_reply(request_id: str) -> dict[str, object]:
    """Check whether the user answered a Riolu question."""
    data = await _call_bridge(
        {"action": "check_reply", "request_id": request_id},
        agent_action=True,
    )
    return {
        "request_id": request_id,
        "status": str(data.get("status", "pending")),
        "answer": str(data.get("answer", "")),
    }


@mcp.tool(
    title="Send an artifact",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def send_artifact(
    path: str,
    caption: str = "",
    source: str = "",
    dedupe_key: str = "",
    delete_after_send: bool = True,
) -> dict[str, object]:
    """Send a requested local file as a Telegram document.

    After successful delivery, the local file is deleted by default. Summarize
    the useful result directly without repeating its path or another file link.
    """
    artifact_path = Path(path).expanduser().resolve(strict=True)
    if not artifact_path.is_file():
        raise RuntimeError(f"Artifact is not a regular file: {artifact_path}")
    _reject_sensitive_filename(artifact_path.name)
    size = artifact_path.stat().st_size
    if size <= 0 or size > MAX_ARTIFACT_BYTES:
        raise RuntimeError(f"Artifact must be between 1 byte and {MAX_ARTIFACT_BYTES} bytes")
    result = await _send_artifact_bytes(
        artifact_path.name,
        artifact_path.read_bytes(),
        caption,
        source,
        dedupe_key,
    )
    if delete_after_send:
        artifact_path.unlink()
        result["deleted"] = True
    else:
        result["deleted"] = False
    return result


@mcp.tool(
    title="Send a Markdown document",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def send_markdown(
    markdown: str,
    filename: str = "report.md",
    source: str = "",
    dedupe_key: str = "",
) -> dict[str, object]:
    """Send a .md Telegram document with only its source label.

    The attachment and source label are the complete response. Delete any
    temporary local source file after delivery. Do not add other caption text
    or repeat its contents, local path, or link.
    """
    safe_name = Path(filename).name
    if not safe_name.casefold().endswith(".md"):
        safe_name += ".md"
    content = markdown.encode("utf-8")
    if not content or len(content) > MAX_ARTIFACT_BYTES:
        raise RuntimeError(f"Markdown must be between 1 byte and {MAX_ARTIFACT_BYTES} bytes")
    return await _send_artifact_bytes(
        safe_name,
        content,
        "",
        source,
        dedupe_key,
        presentation=QUIET_ATTACHMENT_PRESENTATION,
    )


async def _send_artifact_bytes(
    filename: str,
    content: bytes,
    caption: str,
    source: str,
    dedupe_key: str,
    *,
    presentation: str = ATTACHMENT_PRESENTATION,
) -> dict[str, object]:
    data = await _call_bridge(
        {
            "action": "send_artifact",
            "filename": filename,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "caption": caption,
            "source": source,
            "dedupe_key": dedupe_key,
        },
        agent_action=True,
        minimum_timeout_seconds=60,
    )
    return {
        "sent": True,
        "filename": filename,
        "bytes": len(content),
        "delivered": int(data.get("delivered", 0)),
        "skipped": int(data.get("skipped", 0)),
        "presentation": presentation,
    }


def _reject_sensitive_filename(filename: str) -> None:
    folded = filename.casefold()
    if folded in SENSITIVE_FILENAMES or folded.startswith(".env.") or Path(folded).suffix in SENSITIVE_SUFFIXES:
        raise RuntimeError("Riolu refuses to send files that commonly contain credentials")


async def _call_bridge(
    payload: dict[str, object],
    *,
    agent_action: bool = False,
    minimum_timeout_seconds: float = 0,
) -> dict[str, object]:
    config = load_delivery_config()
    endpoint = config.endpoint
    if agent_action:
        override = os.getenv("RIOLU_AGENT_URL", "").strip()
        # The bot's worker URL is not an MCP delivery endpoint. Preserve an
        # explicit full /agent override for independently configured senders.
        endpoint = (override if override.rstrip("/").endswith("/agent")
                    else endpoint.rsplit("/", 1)[0] + "/agent")
    try:
        async with httpx.AsyncClient(timeout=max(config.timeout_seconds, minimum_timeout_seconds)) as client:
            response = await client.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {config.token}"},
            )
            data = response.json()
            response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"Riolu could not complete the Telegram request: {exc}") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError("Riolu rejected the Telegram request")
    return data


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
