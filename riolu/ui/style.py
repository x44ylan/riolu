"""Small, shared Telegram HTML components for Riolu-authored messages."""

import re
from html import escape


_COMMAND = re.compile(r"(?<![\w/:<])/[a-z][a-z0-9_-]*(?![\w/-])")


def code(value: object) -> str:
    return f"<code>{escape(str(value))}</code>"


def commands(text: str) -> str:
    """Escape plain text and highlight commands, without interpreting user HTML."""
    parts, offset = [], 0
    for match in _COMMAND.finditer(text):
        parts.extend((escape(text[offset:match.start()]), code(match.group())))
        offset = match.end()
    parts.append(escape(text[offset:]))
    return "".join(parts)


def card(title: str, body_html: str, *, hint: str = "") -> str:
    """body_html must be escaped text or trusted component output."""
    text = f"<b>{escape(title)}</b>\n<blockquote>{body_html}</blockquote>"
    if hint:
        text += f"\n<i>{escape(hint)}</i>"
    return text


def notice(title: str, text: str) -> str:
    """Render a system notice as one fully quoted title-and-message line."""
    title = title[:100].lower()
    body = text[:1800] + ("…" if len(text) > 1800 else "")
    message = f"{title}: {body}" if body else title
    rendered = f"<blockquote>{commands(message)}</blockquote>"
    while len(rendered) > 3900:
        body = body[:len(body) * 3 // 4] + "…"
        message = f"{title}: {body}" if body else title
        rendered = f"<blockquote>{commands(message)}</blockquote>"
    return rendered
