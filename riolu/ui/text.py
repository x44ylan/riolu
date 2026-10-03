from __future__ import annotations

import re
from html import escape, unescape

from bs4 import BeautifulSoup


def clean_text(value: str) -> str:
    return " ".join(unescape(value).split())


def html_to_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return clean_text(soup.get_text(" ", strip=True))


def clamp_text(value: str, max_length: int) -> str:
    text = clean_text(value)
    if len(text) <= max_length:
        return text
    return text[: max(0, max_length - 3)].rstrip() + "..."


def clamp_multiline_text(value: str, max_length: int) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= max_length:
        return text
    return text[: max(0, max_length - 3)].rstrip() + "..."


_INLINE = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|\*\*(?P<bold>[^*\n]+)\*\*"
    r"|__(?P<bold2>[^_\n]+)__"
    r"|\*(?P<italic>[^*\n]+)\*"
    r"|_(?P<italic2>[^_\n]+)_"
    r"|~~(?P<strike>[^~\n]+)~~"
    r"|\[(?P<link_text>[^\]]+)\]\((?P<link_url>https?://[^)\s]+)\)"
)
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^[-*+]\s+(.+)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_FENCE_BLOCK = re.compile(r"(?ms)^```[ \t]*([\w+-]*)[ \t]*\n(.*?)^```[ \t]*$")


def markdown_to_telegram_html(value: str) -> str:
    """Convert common model Markdown to Telegram HTML entities."""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    segments = _FENCE_BLOCK.split(text)
    pieces = [_convert_block(segments[0])]
    for index in range(1, len(segments), 3):
        lang, code = segments[index], segments[index + 1]
        if lang:
            pieces.append(
                f'<pre><code class="language-{escape(lang)}">'
                f"{escape(code.rstrip(chr(10)))}</code></pre>"
            )
        else:
            pieces.append(f"<pre>{escape(code.rstrip(chr(10)))}</pre>")
        pieces.append(_convert_block(segments[index + 2]))
    return "".join(pieces)


def markdown_chunks(value: str, limit: int = 1800) -> list[str]:
    """Split Markdown into sendable chunks without breaking fenced code."""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    fence = False
    fence_lang = ""

    def flush() -> None:
        nonlocal current, size, fence
        if not current:
            return
        piece = "\n".join(current)
        if fence:
            piece += "\n```"
        chunks.append(piece)
        if fence:
            current = [f"```{fence_lang}"]
            size = len(current[0]) + 1
        else:
            current = []
            size = 0

    for line in text.split("\n"):
        stripped = line.strip()
        is_delimiter = stripped.startswith("```")
        if current and size + len(line) + 1 > limit:
            flush()
        while size + len(line) + 1 > limit:
            length = max(1, limit - size - 1)
            current.append(line[:length])
            size += min(length, len(line)) + 1
            line = line[length:]
            flush()
        if is_delimiter:
            if not fence:
                fence = True
                fence_lang = stripped[3:].strip()[:64]
            else:
                fence = False
                fence_lang = ""
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _convert_block(block: str) -> str:
    if not block:
        return ""
    lines: list[str] = []
    for line in block.split("\n"):
        if _RULE.match(line.strip()):
            lines.append("<i>————————</i>")
            continue
        quote = _QUOTE.match(line)
        if quote:
            lines.append(f"<blockquote>{_convert_inline(quote.group(1))}</blockquote>")
            continue
        heading = _HEADING.match(line)
        if heading:
            lines.append(f"<b>{_convert_inline(heading.group(1))}</b>")
            continue
        bullet = _BULLET.match(line)
        if bullet:
            lines.append(f"• {_convert_inline(bullet.group(1))}")
            continue
        lines.append(_convert_inline(line))
    return "\n".join(lines)


def _convert_inline(line: str) -> str:
    out: list[str] = []
    pos = 0
    for match in _INLINE.finditer(line):
        out.append(escape(line[pos : match.start()]))
        if match.group("code") is not None:
            out.append(f"<code>{escape(match.group('code'))}</code>")
        elif match.group("bold") is not None:
            out.append(f"<b>{escape(match.group('bold'))}</b>")
        elif match.group("bold2") is not None:
            out.append(f"<b>{escape(match.group('bold2'))}</b>")
        elif match.group("italic") is not None:
            out.append(f"<i>{escape(match.group('italic'))}</i>")
        elif match.group("italic2") is not None:
            out.append(f"<i>{escape(match.group('italic2'))}</i>")
        elif match.group("strike") is not None:
            out.append(f"<s>{escape(match.group('strike'))}</s>")
        else:
            url = match.group("link_url")
            out.append(
                f'<a href="{escape(url, quote=True)}">{escape(match.group("link_text"))}</a>'
            )
        pos = match.end()
    out.append(escape(line[pos:]))
    return "".join(out)
