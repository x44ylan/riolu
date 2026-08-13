from __future__ import annotations

import unittest

from riolu.feeds import parse_feed


class FeedParsingTests(unittest.TestCase):
    def test_parses_rss_and_resolves_relative_links(self) -> None:
        xml = """\
        <rss version="2.0"><channel><item>
          <title> Example &amp; update </title>
          <link>/posts/1</link>
          <description><![CDATA[<p>A <strong>useful</strong> summary.</p>]]></description>
          <pubDate>Fri, 10 Jul 2026 08:00:00 GMT</pubDate>
        </item></channel></rss>
        """

        items = parse_feed(
            xml,
            source_id="example",
            source_name="Example",
            category="test",
            feed_url="https://example.com/feed.xml",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Example & update")
        self.assertEqual(items[0].url, "https://example.com/posts/1")
        self.assertEqual(items[0].summary, "A useful summary.")

    def test_parses_namespaced_atom(self) -> None:
        xml = """\
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>Release 1.0</title>
            <link rel="alternate" href="https://example.com/releases/1" />
            <summary>First release</summary>
            <updated>2026-07-10T08:00:00Z</updated>
          </entry>
        </feed>
        """

        items = parse_feed(xml, source_id="releases", source_name="Releases", category="test")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://example.com/releases/1")
        self.assertIsNotNone(items[0].published_at)


if __name__ == "__main__":
    unittest.main()
