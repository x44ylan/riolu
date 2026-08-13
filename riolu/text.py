from __future__ import annotations

from html import unescape

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
