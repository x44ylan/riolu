from __future__ import annotations

from riolu.sources.base import FeedSpec, RssBundleSource


SOURCE = RssBundleSource(
    id="opencode",
    name="Opencode Releases",
    category="releases",
    description="GitHub release notes for anomalyco/opencode.",
    aliases=("opencode_releases", "release_notes", "github_releases"),
    feeds=(
        FeedSpec(
            id="opencode_releases",
            name="anomalyco/opencode",
            url="https://github.com/anomalyco/opencode/releases.atom",
            tags=("github", "release-notes", "opencode"),
        ),
    ),
)
