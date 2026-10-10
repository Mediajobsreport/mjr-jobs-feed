import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "mjr_ats_isolved", Path(__file__).resolve().parents[1] / "mjr-ats-crawler.py"
)
crawler = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = crawler
spec.loader.exec_module(crawler)


class Response:
    def __init__(self, text, url):
        self.text = text
        self.url = url


class IsolvedEmbeddedBoardTests(unittest.TestCase):
    def test_collects_undated_jobs_only_from_explicit_current_list(self):
        listing_url = "https://zimmercommunications.isolvedhire.com/iframe/767/"
        detail_urls = [
            listing_url + "1654532.html",
            listing_url + "1879417.html",
        ]
        listing = """
            <h1>Current Job Listings</h1>
            <p>Below is a list of the current openings with our company.</p>
            <a href="1654532.html">Account Executive</a>
            <a href="1879417.html">Broadcast Traffic Coordinator</a>
        """
        detail = """
            <h1>Account Executive</h1>
            <div>Location: Columbia, MO, USA</div>
            <main><div class="job-description">
                We are looking for a motivated account executive to build client
                relationships, develop advertising campaigns, and support local
                businesses. The successful candidate will prospect clients,
                prepare proposals, manage accounts, and coordinate with internal
                teams to deliver thoughtful marketing solutions.
            </div></main>
        """

        def request(method, url, **kwargs):
            if url.rstrip("/") == listing_url.rstrip("/"):
                return Response(listing, url)
            return Response(detail, url)

        source = {
            "Company": "Zimmer",
            "Industry": "Radio",
            "URL": listing_url,
        }
        with patch.object(crawler, "req", request):
            jobs = crawler.isolved(source)

        self.assertEqual(len(jobs), 2)
        self.assertEqual({job.id for job in jobs}, {"1654532", "1879417"})
        self.assertTrue(all(job.date == crawler.TODAY for job in jobs))

    def test_undated_detail_without_active_listing_evidence_is_rejected(self):
        url = "https://zimmercommunications.isolvedhire.com/iframe/767/1654532.html"
        raw = """
            <h1>Account Executive</h1>
            <main><div class="job-description">
                We are looking for a motivated account executive to build client
                relationships, develop advertising campaigns, and support local
                businesses. The successful candidate will prospect clients,
                prepare proposals, manage accounts, and coordinate with internal
                teams to deliver thoughtful marketing solutions.
            </div></main>
        """
        source = {"Company": "Zimmer", "Industry": "Radio", "URL": url}
        self.assertIsNone(crawler._isolved_detail(source, url, raw))


if __name__ == "__main__":
    unittest.main()
