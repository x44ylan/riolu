from __future__ import annotations

from riolu.sources.base import FeedSpec, RssBundleSource


SOURCE = RssBundleSource(
    id="cyber",
    name="Cyber News",
    category="cybersecurity",
    description="Security headlines from trusted cyber news feeds.",
    aliases=("cyber_news", "security", "infosec"),
    feeds=(
        FeedSpec(
            id="the_hacker_news",
            name="The Hacker News",
            url="https://feeds.feedburner.com/TheHackersNews",
            tags=("cyber", "security", "threats"),
        ),
        FeedSpec(
            id="krebs",
            name="Krebs on Security",
            url="https://krebsonsecurity.com/feed/",
            tags=("cyber", "security", "analysis"),
        ),
        FeedSpec(
            id="bleeping_computer",
            name="BleepingComputer",
            url="https://www.bleepingcomputer.com/feed/",
            tags=("cyber", "security", "malware"),
        ),
    ),
)
