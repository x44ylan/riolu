from __future__ import annotations

from riolu.sources.base import FeedSpec, RssBundleSource


SOURCE = RssBundleSource(
    id="hello_github",
    name="HelloGitHub",
    category="projects",
    description="Curated open source projects from HelloGitHub RSS.",
    aliases=("hellogithub", "hg", "github_projects"),
    feeds=(
        FeedSpec(
            id="hello_github_monthly",
            name="HelloGitHub",
            url="https://hellogithub.com/rss",
            tags=("github", "open-source", "projects"),
        ),
    ),
)
