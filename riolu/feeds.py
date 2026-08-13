from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import urljoin
from xml.etree import ElementTree

from riolu.models import IntelItem
from riolu.text import clean_text, html_to_text


def parse_feed(
    xml_text: str,
    *,
    source_id: str,
    source_name: str,
    category: str,
    tags: Iterable[str] = (),
    feed_url: str = "",
) -> list[IntelItem]:
    root = ElementTree.fromstring(xml_text)
    root_name = _local_name(root.tag)
    if root_name == "feed":
        return _parse_atom(
            root,
            source_id=source_id,
            source_name=source_name,
            category=category,
            tags=tuple(tags),
            feed_url=feed_url,
        )
    return _parse_rss(
        root,
        source_id=source_id,
        source_name=source_name,
        category=category,
        tags=tuple(tags),
        feed_url=feed_url,
    )


def _parse_rss(
    root: ElementTree.Element,
    *,
    source_id: str,
    source_name: str,
    category: str,
    tags: tuple[str, ...],
    feed_url: str,
) -> list[IntelItem]:
    items: list[IntelItem] = []
    for node in root.findall(".//item"):
        title = clean_text(_first_text(node, "title"))
        link = clean_text(_first_text(node, "link")) or clean_text(_first_text(node, "guid"))
        summary = html_to_text(_first_text(node, "description") or _first_text(node, "encoded"))
        published_at = _parse_date(
            _first_text(node, "pubDate") or _first_text(node, "published") or _first_text(node, "date")
        )

        if not title or not link:
            continue

        items.append(
            IntelItem(
                source_id=source_id,
                source_name=source_name,
                title=title,
                url=urljoin(feed_url, link),
                summary=summary,
                category=category,
                tags=tags,
                published_at=published_at,
            )
        )
    return items


def _parse_atom(
    root: ElementTree.Element,
    *,
    source_id: str,
    source_name: str,
    category: str,
    tags: tuple[str, ...],
    feed_url: str,
) -> list[IntelItem]:
    items: list[IntelItem] = []
    for entry in _children(root, "entry"):
        title = clean_text(_first_text(entry, "title"))
        link = _atom_link(entry) or clean_text(_first_text(entry, "id"))
        summary = html_to_text(_first_text(entry, "summary") or _first_text(entry, "content"))
        published_at = _parse_date(_first_text(entry, "published") or _first_text(entry, "updated"))

        if not title or not link:
            continue

        items.append(
            IntelItem(
                source_id=source_id,
                source_name=source_name,
                title=title,
                url=urljoin(feed_url, link),
                summary=summary,
                category=category,
                tags=tags,
                published_at=published_at,
            )
        )
    return items


def _atom_link(entry: ElementTree.Element) -> str:
    fallback = ""
    for link in _children(entry, "link"):
        href = clean_text(link.attrib.get("href", ""))
        if not href:
            continue
        if link.attrib.get("rel", "alternate") == "alternate":
            return href
        fallback = fallback or href
    return fallback


def _first_text(node: ElementTree.Element, *names: str) -> str:
    for name in names:
        for child in node.iter():
            if _local_name(child.tag) == name and child.text:
                return child.text
    return ""


def _children(node: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in list(node) if _local_name(child.tag) == name]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_date(value: str) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None

    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
