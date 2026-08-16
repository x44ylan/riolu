from __future__ import annotations

import json
import unittest

from riolu.sources.hello_github.source import _projects_from_page


class HelloGitHubTests(unittest.TestCase):
    def test_extracts_project_quick_view_from_embedded_data(self) -> None:
        data = {
            "props": {
                "pageProps": {
                    "volume": {
                        "publish_at": "2026-07-28T08:10:05",
                        "data": [
                            {
                                "category_name": "人工智能",
                                "items": [
                                    {
                                        "name": "harbor",
                                        "full_name": "harbor-framework/harbor",
                                        "description_en": "AI agent evaluation framework.",
                                        "github_url": "https://github.com/harbor-framework/harbor",
                                        "stars": 3567,
                                    }
                                ],
                            }
                        ],
                    }
                }
            }
        }
        html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'

        items = _projects_from_page(html)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "harbor-framework/harbor")
        self.assertEqual(items[0].summary, "AI agent evaluation framework.")
        self.assertEqual(items[0].facts, ("AI", "★ 3.6k"))
        self.assertEqual(items[0].url, "https://github.com/harbor-framework/harbor")


if __name__ == "__main__":
    unittest.main()
