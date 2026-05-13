from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag


HELLOGITHUB_URL = "https://hellogithub.com/"
HELLOGITHUB_API_URL = "https://abroad.hellogithub.com/v1/?sort_by=featured&page=1&rank_by=newest&tid=all"
TALKBACK_URL = "https://talkback.sh/"


@dataclass(frozen=True)
class IntelItem:
    source: str
    title: str
    url: str
    summary: str = ""


async def scrape_hellogithub(limit: int, timeout: float) -> list[IntelItem]:
    data = await _fetch_json(HELLOGITHUB_API_URL, timeout)
    records = data.get("data", []) if isinstance(data, dict) else []
    items: list[IntelItem] = []

    for record in records:
        if not isinstance(record, dict):
            continue

        name = _clean_text(str(record.get("name") or ""))
        full_name = _clean_text(str(record.get("full_name") or ""))
        title = _clean_text(str(record.get("title_en") or record.get("title") or name or full_name))
        summary = _clean_text(str(record.get("summary_en") or record.get("summary") or ""))
        if name and name.casefold() not in title.casefold():
            title = f"{name}: {title}"

        if not _useful_title(title):
            continue

        items.append(
            IntelItem(
                source="HelloGitHub",
                title=title,
                url=f"https://github.com/{full_name}" if full_name else HELLOGITHUB_URL,
                summary=summary[:280],
            )
        )

    return _dedupe(items)[:limit]


async def scrape_talkback(limit: int, timeout: float) -> list[IntelItem]:
    html = await _fetch_html(TALKBACK_URL, timeout)
    soup = BeautifulSoup(html, "html.parser")
    return _dedupe(_talkback_items(soup))[:limit]


async def _scrape_cards(
    *,
    source: str,
    url: str,
    limit: int,
    timeout: float,
    card_selectors: tuple[str, ...],
) -> list[IntelItem]:
    html = await _fetch_html(url, timeout)
    soup = BeautifulSoup(html, "html.parser")

    items = _items_from_cards(source, url, soup, card_selectors)
    if len(items) < limit:
        items.extend(_items_from_links(source, url, soup))

    return _dedupe(items)[:limit]


async def _fetch_html(url: str, timeout: float) -> str:
    headers = {
        "User-Agent": "riolu/0.1 (+https://github.com/Dylan-Liew/riolu)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


async def _fetch_json(url: str, timeout: float) -> object:
    headers = {
        "User-Agent": "riolu/0.1 (+https://github.com/Dylan-Liew/riolu)",
        "Accept": "application/json, text/plain, */*",
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


def _talkback_items(soup: BeautifulSoup) -> list[IntelItem]:
    items: list[IntelItem] = []
    for anchor in soup.select('a[href^="http"], a[href^="/vulnerability/"]'):
        if not isinstance(anchor, Tag):
            continue

        href = urljoin(TALKBACK_URL, anchor.get("href", ""))
        if _is_talkback_chrome_link(href):
            continue

        parts = [_clean_text(part) for part in anchor.stripped_strings]
        parts = [part for part in parts if part]
        if not parts:
            continue

        title = next((part for part in parts if _useful_title(part)), "")
        if not title:
            continue
        summary = next((part for part in parts[1:] if len(part) > 50), "")
        items.append(IntelItem(source="Talkback", title=title, url=href, summary=summary[:280]))

    return items


def _is_talkback_chrome_link(url: str) -> bool:
    host = urlparse(url).netloc.removeprefix("www.")
    if host in {"elttam.com", "s3.talkback.sh"}:
        return True
    return url in {TALKBACK_URL, urljoin(TALKBACK_URL, "/resources/")}


def _items_from_cards(
    source: str,
    base_url: str,
    soup: BeautifulSoup,
    selectors: tuple[str, ...],
) -> list[IntelItem]:
    items: list[IntelItem] = []
    for selector in selectors:
        for card in soup.select(selector):
            if not isinstance(card, Tag):
                continue

            anchor = _best_anchor(card)
            if anchor is None:
                continue

            title = _clean_text(anchor.get_text(" ", strip=True))
            if not _useful_title(title):
                continue

            summary = _summary_from_card(card, title)
            items.append(
                IntelItem(
                    source=source,
                    title=title,
                    url=urljoin(base_url, anchor.get("href", "")),
                    summary=summary,
                )
            )
    return items


def _items_from_links(source: str, base_url: str, soup: BeautifulSoup) -> list[IntelItem]:
    items: list[IntelItem] = []
    for anchor in soup.select("a[href]"):
        title = _clean_text(anchor.get_text(" ", strip=True))
        if not _useful_title(title):
            continue

        items.append(
            IntelItem(
                source=source,
                title=title,
                url=urljoin(base_url, anchor.get("href", "")),
            )
        )
    return items


def _best_anchor(card: Tag) -> Tag | None:
    heading_anchor = card.select_one("h1 a[href], h2 a[href], h3 a[href], h4 a[href]")
    if isinstance(heading_anchor, Tag):
        return heading_anchor

    anchors = [anchor for anchor in card.select("a[href]") if _useful_title(anchor.get_text(" ", strip=True))]
    if not anchors:
        return None

    return max(anchors, key=lambda anchor: len(anchor.get_text(" ", strip=True)))


def _summary_from_card(card: Tag, title: str) -> str:
    candidates = [node.get_text(" ", strip=True) for node in card.select("p, [class*='desc'], [class*='summary']")]
    for candidate in candidates:
        text = _clean_text(candidate)
        if text and text != title and len(text) > 20:
            return text[:280]
    return ""


def _clean_text(text: str) -> str:
    return " ".join(unescape(text).split())


def _useful_title(title: str) -> bool:
    if len(title) < 4 or len(title) > 160:
        return False
    lowered = title.lower()
    blocked = {"home", "login", "sign in", "sign up", "rss", "github", "twitter", "x"}
    return lowered not in blocked


def _dedupe(items: list[IntelItem]) -> list[IntelItem]:
    seen: set[tuple[str, str]] = set()
    result: list[IntelItem] = []
    for item in items:
        key = (item.title.casefold(), item.url)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
