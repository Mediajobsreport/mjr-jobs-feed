import importlib.util
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "mjr_ats", Path(__file__).resolve().parents[1] / "mjr-ats-crawler.py"
)
crawler = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = crawler
spec.loader.exec_module(crawler)


class Response:
    def __init__(self, payload=None, text=""):
        self.payload = payload
        self.text = text

    def json(self):
        return self.payload


class ATSRegressionTests(unittest.TestCase):
    def test_location_inference_ignores_global_description_boilerplate(self):
        self.assertEqual(crawler.infer_country("Celebration, Florida, United States", description="Offices in Toronto, Vancouver and Germany"), "US")
        self.assertEqual(crawler.infer_country("Los Angeles, CA", description="Our Toronto office"), "US")
        self.assertEqual(crawler.infer_country("Toronto, ON, Canada"), "CA")
        self.assertEqual(crawler.infer_country("Germany"), "DE")

    def test_disney_metadata_uses_job_header_and_preserves_scope(self):
        job = crawler.Job("10163635", "Tax Graduate Intern, Spring 2027", "ESPN",
            "Job ID 10163635 Location Celebration, Florida, United States Business The Walt Disney Company (Corporate) Date posted Oct. 22, 2026 "
            + "Tax and accounting internship responsibilities. " * 20
            + "Related Jobs Toronto, Ontario, Canada Business ESPN Date posted October 22, 2026",
            crawler.TODAY, "Internship", "Internships",
            "https://www.disneycareers.com/en/job/celebration/tax-intern/391/102302180480", "")
        jobs, rejected = crawler.apply_company_scope_filters([job])
        self.assertEqual(rejected, [])
        self.assertEqual((job.city, job.state, job.country), ("Celebration", "Florida", "US"))
        self.assertEqual(job.company, "The Walt Disney Company (Corporate)")
        self.assertEqual(crawler._canonical_requisition_key(job), "disney:102302180480")
        job.title = "Hotel Housekeeper"
        self.assertTrue(crawler.company_scope_rejection_reason(job))
        job.title = "Senior Effects Technical Director"
        job.description = "Job ID 10163658 Location London, United Kingdom Business Industrial Light & Magic Date posted Oct. 22, 2026"
        jobs, rejected = crawler.apply_company_scope_filters([job])
        self.assertEqual(jobs, [])
        self.assertEqual((job.city, job.country), ("London", "GB"))
        self.assertEqual(len(rejected), 1)
        job.description = "Job ID 10163635 Location Glendale, California, United States / Santa Monica, California, United States Business Disney Entertainment and ESPN Product & Technology Date posted Oct. 22, 2026"
        crawler._normalize_disney_metadata(job)
        self.assertEqual((job.city, job.state, job.country), ("Glendale", "California", "US"))

    def test_sony_foreign_location_is_excluded_even_with_old_us_country(self):
        row = {"company": "Sony Music Entertainment", "title": "Legal Internship", "city": "Germany", "country": "US"}
        self.assertTrue(crawler.company_scope_rejection_reason(row))
        row["city"] = "New York, NY"
        self.assertFalse(crawler.company_scope_rejection_reason(row))

    def test_hubbard_adp_job_code_uses_rendered_public_code(self):
        detail = """Multiplatform News Producer
#26-1275
Albany, New York, United States
Job Overview
Produce digital and broadcast news content."""
        self.assertEqual(crawler._hubbard_adp_job_code(detail), "26-1275")
        self.assertEqual(
            crawler._hubbard_adp_job_code("Multiplatform News Producer\nAlbany, New York"),
            "",
        )

    def test_relative_dates_include_workday_lower_bounds(self):
        for label, days in [
            ("Posted Today", 0), ("Posted Yesterday", 1),
            ("Posted 3 Days Ago", 3), ("Posted 30+ Days Ago", 30),
            ("Posted 45+ Days Ago", 45),
        ]:
            with self.subTest(label=label):
                self.assertEqual(crawler.pdate(label), crawler.TODAY - timedelta(days=days))

    def test_workday_skips_old_details_but_reads_fresh_postings(self):
        calls = []

        def request(method, url, **kwargs):
            calls.append(url)
            if method == "POST":
                return Response({"total": 2, "jobPostings": [
                    {"externalPath": "/job/old", "postedOn": "Posted 30+ Days Ago"},
                    {"externalPath": "/job/new", "postedOn": "Posted Today"},
                ]})
            return Response({"jobPostingInfo": {
                "title": "Business Reporting Internship", "postedOn": "Posted Today",
                "jobDescription": "Editorial duties " * 30,
                "location": "New York, NY", "jobReqId": "new",
            }})

        with patch.object(crawler, "req", request), patch.object(
            crawler, "_discover_workday_endpoint", return_value=("example.test", "example", "Careers")
        ), patch.object(crawler, "_workday_hosts", return_value=["example.test"]):
            jobs = crawler.workday({"Company": "Test Media", "Industry": "Journalism", "URL": "https://example.test/Careers"})
        self.assertEqual([job.id for job in jobs], ["new"])
        self.assertEqual(jobs[0].category, "Internships")
        self.assertFalse(any("/job/old" in url for url in calls))

    def test_ap_reads_microdata_and_rejects_foreign_stale_and_missing_dates(self):
        links = ''.join(f'<a href="/job/Editor/{n}/">Editor</a>' for n in range(1, 5))

        def request(method, url, **kwargs):
            if "/go/" in url:
                return Response(text=links + links)
            n = int(url.rstrip("/").split("/")[-1])
            posted = crawler.TODAY - timedelta(days=30 if n == 3 else 2)
            date_field = f'<meta itemprop="datePosted" content="{posted.isoformat()}">' if n != 4 else ""
            return Response(text=f'''
                <h1 itemprop="title">News Editor</h1>{date_field}
                <meta itemprop="addressCountry" content="{'GB' if n == 2 else 'US'}">
                <meta itemprop="addressLocality" content="Los Angeles">
                <meta itemprop="addressRegion" content="CA">
                <div itemprop="description">{'Edit breaking news and journalism. ' * 20}</div>
            ''')

        with patch.object(crawler, "req", request):
            jobs = crawler.associated_press({"Company": "Associated Press (AP)", "Industry": "Journalism"})
        self.assertEqual([job.id for job in jobs], ["1"])
        self.assertEqual((jobs[0].city, jobs[0].state, jobs[0].country), ("Los Angeles", "CA", "US"))
        self.assertEqual(jobs[0].date, crawler.TODAY - timedelta(days=2))


    def test_paycom_portal_jobs_route_is_enumerated(self):
        clientkey = "41E82EC60AAD792EC7018E3AB22DE8D2"
        src = {
            "Company": "Woodward Communications",
            "Industry": "Journalism",
            "URL": (
                "https://www.paycomonline.net/v4/ats/web.php/jobs"
                "?clientkey=" + clientkey
            ),
        }
        detail_url = (
            "https://www.paycomonline.net/v4/ats/web.php/portal/"
            + clientkey + "/jobs/351753"
        )
        description = "<p>" + "Write and report local news. " * 40 + "</p>"
        payload = {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "News & Sports Reporter/Photographer FT",
            "datePosted": crawler.TODAY.isoformat(),
            "description": description,
            "identifier": {"value": "J0NA62351753"},
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Cascade",
                    "addressRegion": "IA",
                    "addressCountry": "US",
                },
            },
        }
        detail_html = (
            '<script type="application/ld+json">'
            + json.dumps(payload)
            + "</script>"
        )

        def request(method, url, **kwargs):
            if url == detail_url:
                return Response(text=detail_html)
            if url == src["URL"]:
                return Response(
                    text=(
                        '<a href="/v4/ats/web.php/portal/'
                        + clientkey
                        + '/jobs/351753">Current opening</a>'
                    )
                )
            raise AssertionError("Unexpected Paycom request: {}".format(url))

        with patch.object(crawler, "req", request):
            jobs = crawler.paycom(src)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].id, "J0NA62351753")
        self.assertEqual(jobs[0].title, "News & Sports Reporter/Photographer FT")
        self.assertIn("Cascade", jobs[0].city)
        self.assertEqual(jobs[0].country, "US")

if __name__ == "__main__":
    unittest.main()

