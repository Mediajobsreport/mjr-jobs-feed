import unittest
from datetime import timedelta
from unittest.mock import patch

from test_ats_regressions import crawler, Response


SOURCE = {"Company": "Lee Enterprises", "Industry": "Journalism",
    "URL": "https://jobs.dayforcehcm.com/en-US/leeenterprises/CANDIDATEPORTAL"}


def row(identifier, days=2, country="US", title="News Reporter"):
    return {
        "clientNamespace": "leeenterprises", "jobPostingId": identifier,
        "jobTitle": title, "jobDescription": "Report local news and journalism. " * 20,
        "postingStartTimestampUTC": (crawler.TODAY - timedelta(days=days)).isoformat(),
        "postingExpiryTimestampUTC": None,
        "postingLocations": [
            {"isoCountryCode": country, "cityName": None, "stateCode": "MT"},
            {"isoCountryCode": country, "cityName": "Great Falls", "stateCode": "MT"},
        ],
    }


class DayforceSearchTests(unittest.TestCase):
    def test_paginates_and_applies_age_country_and_internship_filters(self):
        offsets = []

        def request(method, url, **kwargs):
            if method == "GET":
                return Response({"csrfToken": "anonymous-test-token"})
            offset = kwargs["json"]["paginationStart"]
            offsets.append(offset)
            self.assertEqual(kwargs["headers"]["X-CSRF-TOKEN"], "anonymous-test-token")
            rows = [row(1), row(2, days=14)] if offset == 0 else [
                row(3, country="GB"), row(4, days=20, country="CA", title="News Internship"),
            ]
            return Response({"jobPostings": rows, "maxCount": 4})

        with patch.object(crawler, "req", request):
            jobs = crawler.dayforce(SOURCE)
        self.assertEqual(offsets, [0, 2])
        self.assertEqual([job.id for job in jobs], ["1", "4"])
        self.assertEqual(jobs[0].city, "Great Falls")
        self.assertEqual(jobs[1].category, "Internships")
        self.assertTrue(jobs[0].url.endswith("/CANDIDATEPORTAL/jobs/1"))

    def test_rejects_expired_posting(self):
        expired = row(1)
        expired["postingExpiryTimestampUTC"] = "2000-01-01T00:00:00+00:00"
        with patch.object(crawler, "req", side_effect=[
            Response({"csrfToken": "token"}),
            Response({"jobPostings": [expired], "maxCount": 1}),
        ]):
            self.assertEqual(crawler.dayforce_public_search(SOURCE), [])

    def test_incomplete_search_is_an_error(self):
        with patch.object(crawler, "req", side_effect=[
            Response({"csrfToken": "token"}),
            Response({"jobPostings": [], "maxCount": 25}),
        ]):
            with self.assertRaisesRegex(RuntimeError, "reported total"):
                crawler.dayforce_public_search(SOURCE)

    def test_repeated_search_page_is_an_error(self):
        with patch.object(crawler, "req", side_effect=[
            Response({"csrfToken": "token"}),
            Response({"jobPostings": [row(1)], "maxCount": 2}),
            Response({"jobPostings": [row(1)], "maxCount": 2}),
        ]):
            with self.assertRaisesRegex(RuntimeError, "repeated a page"):
                crawler.dayforce_public_search(SOURCE)


if __name__ == "__main__":
    unittest.main()
