import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "mjr_ats_blackburn", Path(__file__).resolve().parents[1] / "mjr-ats-crawler.py"
)
crawler = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = crawler
spec.loader.exec_module(crawler)


class BlackburnWordPressTests(unittest.TestCase):
    def test_finds_wordpress_career_post_permalinks(self):
        base = "https://blackburnmedia.ca/careers"
        html = '''
            <a href="/careers/2026/10/digital-content-coordinator">Digital Content Coordinator</a>
            <a href="/uncategorized/2026/09/broadcast-engineer-chatham-ontario">Broadcast Engineer</a>
        '''
        links = crawler._direct_board_candidate_links(base, html)
        self.assertIn(
            "https://blackburnmedia.ca/careers/2026/10/digital-content-coordinator",
            links,
        )
        self.assertIn(
            "https://blackburnmedia.ca/uncategorized/2026/09/broadcast-engineer-chatham-ontario",
            links,
        )

    def test_uses_wordpress_published_date_metadata(self):
        html = '''
            <meta property="article:published_time" content="2026-10-02T09:00:00-04:00">
        '''
        self.assertEqual(crawler._direct_board_date(html), date(2026, 10, 2))


if __name__ == "__main__":
    unittest.main()
