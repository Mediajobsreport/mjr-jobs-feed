import copy
import csv
import json
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_ats_regressions import crawler, Response


BOARD = "ae441110-89bd-444d-8ad2-b76c7b9db7a9"
OID = "f937b6e7-46d4-4529-b042-b218c92368c2"
ROOT = f"https://recruiting.ultipro.com/GRA1017GRYT/JobBoard/{BOARD}"
URL = f"{ROOT}/OpportunityDetail?opportunityId={OID}"
SOURCE = {"Company": "Gray Media", "Industry": "Television", "URL": ROOT}


def opportunity():
    return {
        "Id": OID, "Title": "Digital News Content Producer - WFSB",
        "RequisitionNumber": "DIGIT016964", "FullTime": True,
        "PostedDate": (crawler.TODAY - timedelta(days=2)).isoformat(),
        "UpdatedDate": crawler.TODAY.isoformat(), "OpportunityIsClosed": False,
        "JobLocationType": 2,
        "Description": "<p>Produce local news and digital journalism.</p>" * 12,
        "JobBoardMemberships": [{
            "JobBoardId": BOARD, "PublishedExternal": True,
            "ExternalPostedDate": (crawler.TODAY - timedelta(days=2)).isoformat(),
        }],
        "Locations": [{"Address": {
            "City": "Rocky Hill", "State": {"Code": "CT"},
            "Country": {"Code": "USA"},
        }}],
    }


class GrayUKGTests(unittest.TestCase):
    def test_detail_request_accepts_timeout_and_retry_controls(self):
        from unittest.mock import Mock
        response = Mock(status_code=200)
        with patch.object(crawler.SESSION, "request", return_value=response) as request:
            self.assertIs(crawler._req_raw("GET", URL, timeout=12, tries=2), response)
        request.assert_called_once_with("GET", URL, timeout=12)
        with patch.object(crawler.SESSION, "request", side_effect=crawler.requests.Timeout("timeout")) as request, patch.object(crawler.time, "sleep"):
            with self.assertRaises(crawler.requests.Timeout):
                crawler._req_raw("GET", URL, timeout=12, tries=2)
        self.assertEqual(request.call_count, 2)

    def test_reads_json_from_unrendered_public_detail(self):
        raw = '<h1><span data-bind="text: formattedTitle"></span></h1><script>'
        raw += 'var opportunity = new US.Opportunity.CandidateOpportunityDetail('
        raw += json.dumps(opportunity()) + ');</script>'
        job = crawler._ukg_detail(SOURCE, URL, raw)
        self.assertEqual(job.id, "DIGIT016964")
        self.assertEqual((job.city, job.state, job.country), ("Rocky Hill", "CT", "US"))
        self.assertEqual(job.date, crawler.TODAY - timedelta(days=2))
        self.assertIn("local news", job.description)

    def test_rejects_closed_foreign_stale_wrong_board_and_wrong_id(self):
        mutations = [
            lambda p: p.update(OpportunityIsClosed=True),
            lambda p: p["Locations"][0]["Address"]["Country"].update(Code="GBR"),
            lambda p: p["JobBoardMemberships"][0].update(ExternalPostedDate=(crawler.TODAY - timedelta(days=14)).isoformat()),
            lambda p: p["JobBoardMemberships"][0].update(PublishedExternal=False),
            lambda p: p["JobBoardMemberships"][0].update(JobBoardId="wrong-board"),
            lambda p: p.update(Id="wrong-id"),
            lambda p: p.update(Description="Apply now"),
        ]
        for mutate in mutations:
            payload = opportunity()
            mutate(payload)
            with self.subTest(payload=payload):
                self.assertIsNone(crawler._ukg_embedded_job(SOURCE, URL, payload))

    def test_preserves_employer_nationwide_location(self):
        payload = opportunity()
        payload["Locations"][0]["Address"]["City"] = None
        payload["Locations"][0]["Address"]["State"] = None
        payload["Locations"][0]["LocalizedName"] = "Nationwide"
        job = crawler._ukg_embedded_job(SOURCE, URL, payload)
        self.assertEqual((job.city, job.state, job.country), ("Nationwide", "", "US"))

    def test_prefilters_stale_inventory_and_deduplicates_detail_requests(self):
        row = {
            "external_id": OID, "title": "News Producer", "status": "Published",
            "external_posted_date": crawler.TODAY.isoformat(),
            "job_location_type": "remote",
            "job_boards": [{"is_published_external": True, "recruiting_apply_url": URL}],
        }
        stale = copy.deepcopy(row)
        stale["external_posted_date"] = (crawler.TODAY - timedelta(days=14)).isoformat()
        raw = 'new US.Opportunity.CandidateOpportunityDetail(' + json.dumps(opportunity()) + ');'
        with patch.object(crawler, "req", return_value=Response([row, row, stale])), patch.object(
            crawler, "_req_raw", return_value=Response(text=raw)
        ) as detail:
            jobs = crawler.gray_direct(SOURCE)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].work_arrangement, "Remote")
        detail.assert_called_once()

    def test_gray_has_one_active_source(self):
        path = Path(__file__).resolve().parents[1] / "mjr-ats-sources.csv"
        with path.open() as handle:
            rows = [row for row in csv.DictReader(handle) if row["Company"] in {"Gray Media", "Gray Television"}]
        self.assertEqual([row["Company"] for row in rows], ["Gray Media"])


if __name__ == "__main__":
    unittest.main()
