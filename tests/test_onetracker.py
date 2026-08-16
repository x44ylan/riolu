from __future__ import annotations

import unittest

from bs4 import BeautifulSoup

from riolu.sources.onetracker.source import _meta_content


class OneTrackerTests(unittest.TestCase):
    def test_reads_public_page_metadata(self) -> None:
        soup = BeautifulSoup(
            '<meta property="og:title" content=" OneTracker  Research ">',
            "html.parser",
        )

        self.assertEqual(_meta_content(soup, "meta[property='og:title']"), "OneTracker Research")


if __name__ == "__main__":
    unittest.main()
