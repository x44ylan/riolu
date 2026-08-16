from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from riolu.feeds import parse_feed
from riolu.models import IntelItem
from riolu.text import clean_text


HELLOGITHUB_RSS = "https://hellogithub.com/rss"

CATEGORY_LABELS = {
    "人工智能": "AI",
    "其它": "Other",
    "开源书籍": "Open-source book",
}


@dataclass(frozen=True)
class HelloGitHubSource:
    id: str = "hello_github"
    name: str = "HelloGitHub"
    category: str = "projects"
    description: str = "Curated open-source projects with repository details and quick summaries."
    aliases: tuple[str, ...] = ("hellogithub", "hg", "github_projects")

    async def fetch(self, limit: int, client: httpx.AsyncClient) -> list[IntelItem]:
        if limit < 1:
            return []

        rss_response = await client.get(HELLOGITHUB_RSS)
        rss_response.raise_for_status()
        issues = parse_feed(
            rss_response.text,
            source_id=self.id,
            source_name=self.name,
            category=self.category,
            feed_url=HELLOGITHUB_RSS,
        )
        if not issues:
            return []

        latest_issue = issues[0]
        issue_response = await client.get(latest_issue.url)
        issue_response.raise_for_status()
        return _projects_from_page(issue_response.text, fallback_date=latest_issue.published_at)[:limit]


def _projects_from_page(html: str, *, fallback_date: datetime | None = None) -> list[IntelItem]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.select_one("#__NEXT_DATA__")
    if script is None or not script.string:
        raise RuntimeError("HelloGitHub project data was not found")

    try:
        page_data = json.loads(script.string)
        volume = page_data["props"]["pageProps"]["volume"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("HelloGitHub project data has an unexpected format") from exc

    published_at = _published_at(volume.get("publish_at")) or fallback_date
    results: list[IntelItem] = []
    for category in volume.get("data", []):
        if not isinstance(category, dict):
            continue
        category_name = _category_label(str(category.get("category_name", "")))
        for project in category.get("items", []):
            item = _project_item(project, category_name=category_name, published_at=published_at)
            if item is not None:
                results.append(item)
    return results


def _project_item(
    project: object,
    *,
    category_name: str,
    published_at: datetime | None,
) -> IntelItem | None:
    if not isinstance(project, dict):
        return None
    full_name = clean_text(str(project.get("full_name", "")))
    name = clean_text(str(project.get("name", "")))
    url = clean_text(str(project.get("github_url", "")))
    if not (full_name or name) or not url.startswith("https://github.com/"):
        return None

    summary = clean_text(str(project.get("description_en") or project.get("description") or ""))
    facts = tuple(
        value
        for value in (
            category_name,
            _stars(project.get("stars")),
        )
        if value
    )
    return IntelItem(
        source_id="hello_github",
        source_name="HelloGitHub",
        title=full_name or name,
        url=url,
        summary=summary,
        category="projects",
        tags=("github", "open-source"),
        facts=facts,
        published_at=published_at,
    )


def _category_label(value: str) -> str:
    clean = clean_text(value)
    if clean in CATEGORY_LABELS:
        return CATEGORY_LABELS[clean]
    return clean.removesuffix(" 项目").removesuffix("项目")


def _stars(value: Any) -> str:
    try:
        stars = int(value)
    except (TypeError, ValueError):
        return ""
    if stars >= 1_000_000:
        display = f"{stars / 1_000_000:.1f}m"
    elif stars >= 1_000:
        display = f"{stars / 1_000:.1f}k"
    else:
        display = str(stars)
    return f"★ {display.replace('.0', '')}"


def _published_at(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC)


SOURCE = HelloGitHubSource()
