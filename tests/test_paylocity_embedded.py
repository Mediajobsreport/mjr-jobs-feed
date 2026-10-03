import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from test_ats_regressions import crawler, Response


SOURCE = {"Company": "Test Radio", "Industry": "Radio",
    "URL": "https://recruiting.paylocity.com/recruiting/jobs/All/test/Test-Radio"}


def row(identifier, days=2, internal=False, country="USA"):
    return {"JobId": identifier, "JobTitle": "Advertising Sales Executive",
        "PublishedDate": (crawler.TODAY - timedelta(days=days)).isoformat(),
        "IsInternal": internal, "IsRemote": False,
        "JobLocation": {"City": "Green Bay", "State": "WI", "Country": country}}


class PaylocityEmbeddedTests(unittest.TestCase):
    def test_detail_uses_job_title_and_excludes_browser_notice(self):
        raw = '''<noscript><h2>In order to use this site, it is necessary to enable JavaScript.</h2></noscript>
            <div class="job-preview-details"><h1>Advertising Sales Executive</h1>
            <h2>Build Your Career</h2><p>''' + 'Sell radio advertising to local businesses. ' * 20 + '''</p></div>
            <script>window.pageData = {"jobTitle":"Advertising Sales Executive"};</script>'''
        with patch.object(crawler, "req", return_value=Response(text=raw)):
            job = crawler._paylocity_detail_job(SOURCE,
                "https://recruiting.paylocity.com/recruiting/jobs/Details/1", crawler.TODAY)
        self.assertEqual(job.title, "Advertising Sales Executive")
        self.assertEqual(job.category, "Sales & Marketing")
        self.assertNotIn("enable JavaScript", job.description)

    def tearDown(self):
        crawler.PUBLIC_BOARD_ENUMERATION.clear()

    def test_reads_inventory_without_anchors_and_prefilters_details(self):
        raw = 'window.pageData = ' + json.dumps({"Jobs": [row(1), row(2, 14), row(3, internal=True), row(4, country="GB") ]}) + ';'
        job = crawler.Job("1", "Advertising Sales Executive", "Test Radio",
            "Sales duties " * 30, crawler.TODAY - timedelta(days=2), "Full Time",
            "Sales & Marketing", "https://recruiting.paylocity.com/recruiting/jobs/Details/1", SOURCE["URL"])
        with patch.object(crawler, "req", return_value=Response(text=raw)), patch.object(
            crawler, "_paylocity_detail_job", return_value=job
        ) as detail:
            jobs = crawler._paylocity_board_jobs(SOURCE)
        detail.assert_called_once_with(SOURCE, job.url, job.date)
        self.assertEqual(len(jobs), 1)
        self.assertEqual((job.city, job.state, job.country), ("Green Bay", "WI", "US"))
        self.assertEqual(crawler.PUBLIC_BOARD_ENUMERATION["test radio"], 3)

    def test_stale_board_is_verified_empty_without_detail_requests(self):
        raw = 'window.pageData = ' + json.dumps({"Jobs": [row(1, 30)]})
        with patch.object(crawler, "req", return_value=Response(text=raw)), patch.object(crawler, "_paylocity_detail_job") as detail:
            self.assertEqual(crawler._paylocity_board_jobs(SOURCE), [])
        detail.assert_not_called()
        self.assertEqual(crawler.PUBLIC_BOARD_ENUMERATION["test radio"], 1)

    def test_failed_fresh_detail_is_an_error(self):
        raw = 'window.pageData = ' + json.dumps({"Jobs": [row(1)]})
        with patch.object(crawler, "req", return_value=Response(text=raw)), patch.object(crawler, "_paylocity_detail_job", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "could not be validated"):
                crawler._paylocity_board_jobs(SOURCE)
        self.assertNotIn("test radio", crawler.PUBLIC_BOARD_ENUMERATION)

    def test_nrg_verified_empty_board_does_not_retry_old_discovery(self):
        source = {**SOURCE, "Company": "NRG Media"}
        crawler.PUBLIC_BOARD_ENUMERATION["nrg media"] = 6
        with patch.object(crawler, "paylocity", return_value=[]), patch.object(crawler, "paylocity_v18") as fallback:
            self.assertEqual(crawler.nrg_paylocity(source), [])
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
