from __future__ import annotations

from riolu.sources.base import FeedSpec, RssBundleSource


SOURCE = RssBundleSource(
    id="tech",
    name="Daily Tech Learning",
    category="learning",
    description="A compact stream for discovering one useful technical idea each day.",
    aliases=("learn", "learning", "daily_tech", "tech_learning"),
    keywords=(
        "ai",
        "api",
        "cloud",
        "code",
        "database",
        "developer",
        "engineering",
        "github",
        "javascript",
        "kubernetes",
        "linux",
        "llm",
        "open source",
        "postgres",
        "programming",
        "python",
        "rust",
        "security",
        "software",
        "systems",
        "tutorial",
        "typescript",
        "web",
    ),
    feeds=(
        FeedSpec(
            id="hacker_news_frontpage",
            name="Hacker News Front Page",
            url="https://hnrss.org/frontpage",
            tags=("technology", "startups", "engineering"),
        ),
        FeedSpec(
            id="lobsters",
            name="Lobsters",
            url="https://lobste.rs/rss",
            tags=("programming", "systems", "engineering"),
        ),
        FeedSpec(
            id="freecodecamp",
            name="freeCodeCamp",
            url="https://www.freecodecamp.org/news/rss/",
            tags=("learning", "programming", "tutorials"),
        ),
    ),
)
