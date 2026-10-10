from datetime import datetime
import time
import random
import os
#!/usr/bin/env python3
import csv, hashlib, html, json, os, re, time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse, unquote, quote

import requests
from bs4 import BeautifulSoup, Comment
from dateutil import parser as dtparser

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None



TODAY = date.today()
WINDOW_DAYS = int(os.getenv("MJR_WINDOW_DAYS", "30"))
CUTOFF = TODAY - timedelta(days=WINDOW_DAYS)
REGULAR_LIFE_DAYS = 14
INTERNSHIP_LIFE_DAYS = 30

def retention_days(jobtype_value):
    return INTERNSHIP_LIFE_DAYS if jobtype_value == "Internship" else REGULAR_LIFE_DAYS

def feed_cutoff(jobtype_value):
    return TODAY - timedelta(days=retention_days(jobtype_value))

def job_is_fresh(j):
    return bool(j.date and (TODAY - j.date).days < retention_days(j.jobtype))

SOURCES_FILE = Path(os.getenv("MJR_SOURCES", "mjr-ats-sources.csv"))
OUTFILE = Path(os.getenv("MJR_OUTPUT", "mjr-jboard-master.xml"))
AUDITFILE = Path(os.getenv("MJR_AUDIT", "mjr-ats-audit.csv"))
STATE_FILE = Path(os.getenv("MJR_STATE", "mjr-job-state.json"))
QUALITY_FILE = Path(os.getenv("MJR_QUALITY_REPORT", "mjr-job-quality-report.csv"))
CAREERONESTOP_DIAGNOSTIC = Path(os.getenv("MJR_CAREERONESTOP_DIAGNOSTIC", "mjr-careeronestop-diagnostic.json"))
CAREERONESTOP_SOURCE = "https://www.careeronestop.org/"
CAREERONESTOP_LOGO = "https://raw.githubusercontent.com/mediajobsreport/mjr-jobs-feed/main/images/careeronestop-logo.jpg"

SESSION = requests.Session()
SESSION.headers.update(
    {"User-Agent": "MJR-Jobs-Feed/1.0 (+https://www.mediajobsreport.com)"}
)

APPROVED = {
    "Business Office",
    "Digital",
    "Engineering",
    "Internships",
    "Journalism",
    "Management",
    "Music Industry",
    "Public Media / Higher Ed",
    "Public Relations",
    "Radio",
    "Sales & Marketing",
    "Television",
    "Voiceover",
}


def _repair_mojibake(s):
    """Repair common UTF-8-as-Windows-1252 artifacts without rewriting prose."""
    text = str(s or "")
    replacements = {
        "\u00c2\u00a0": " ", "\u00c2 ": " ",
        "\u00e2\u0080\u0098": "‘", "\u00e2\u0080\u0099": "’",
        "\u00e2\u0080\u009c": "“", "\u00e2\u0080\u009d": "”",
        "\u00e2\u0080\u0093": "–", "\u00e2\u0080\u0094": "—",
        "\u00e2\u0080\u00a6": "…",
        "\u00e2\u0084\u00a2": "™", "\u00c2\u00a9": "©", "\u00c2\u00ae": "®",
        "\u00c3\u00a1": "á", "\u00c3\u00a9": "é", "\u00c3\u00ad": "í",
        "\u00c3\u00b3": "ó", "\u00c3\u00ba": "ú", "\u00c3\u00b1": "ñ",
        "\u00c3\u00bc": "ü", "\u00c3\u00a8": "è", "\u00c3\u00a0": "à",
        "\u00c3\u0081": "Á", "\u00c3\u0089": "É", "\u00c3\u008d": "Í",
        "\u00c3\u0093": "Ó", "\u00c3\u009a": "Ú", "\u00c3\u0091": "Ñ",
    }
    for broken, fixed in replacements.items():
        text = text.replace(broken, fixed)
    return text


def clean(s):
    return re.sub(r"\s+", " ", _repair_mojibake(html.unescape(s or ""))).strip()


def strip_html(s):
    return clean(BeautifulSoup(s or "", "html.parser").get_text(" "))


def format_description(s):
    """Preserve useful source formatting while removing unsafe/noisy markup.

    The resulting string is safe HTML intended for the XML description field.
    Paragraphs, headings, line breaks, lists and basic emphasis are retained.
    Scripts, styles, forms, buttons, images and all attributes are removed.
    """
    raw = _repair_mojibake(html.unescape(str(s or ""))).strip()
    if not raw:
        return ""

    soup = BeautifulSoup(raw, "html.parser")

    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()

    for bad in soup.find_all([
        "script", "style", "noscript", "iframe", "form", "button",
        "input", "select", "textarea", "svg", "canvas", "img",
    ]):
        bad.decompose()

    allowed = {
        "p", "br", "ul", "ol", "li",
        "strong", "b", "em", "i",
        "h2", "h3", "h4", "h5",
    }

    # Remove attributes and unwrap non-structural tags rather than deleting
    # their text. This keeps employer wording while avoiding source CSS/classes.
    for tag in list(soup.find_all(True)):
        if tag.name in allowed:
            tag.attrs = {}
        else:
            tag.unwrap()

    out = str(soup).strip()

    # If the source was plain text, preserve its line structure as HTML instead
    # of collapsing it into one block.
    if not re.search(r"<(?:p|br|ul|ol|li|h[2-5])\b", out, re.I):
        text = BeautifulSoup(out, "html.parser").get_text("\n")
        lines = [html.escape(x.strip()) for x in text.splitlines() if x.strip()]
        if len(lines) > 1:
            out = "".join(f"<p>{line}</p>" for line in lines)
        else:
            out = f"<p>{lines[0]}</p>" if lines else ""

    # Some ATS endpoints return an entire multi-section posting inside one
    # paragraph. Restore section boundaries without changing employer text.
    # This is intentionally limited to long, sparsely structured descriptions.
    structural = len(re.findall(r"<(?:p|li|ul|ol|h[2-5]|br)\b", out, re.I))
    if len(strip_html(out)) > 700 and structural < 2:
        text = BeautifulSoup(out, "html.parser").get_text(" ")
        headings = [
            "About Suno", "About the Role", "About the Job", "About Us",
            "Position Summary", "Position Overview", "Job Summary",
            "Overview", "Responsibilities", "Major Responsibilities",
            "Key Responsibilities", "Primary Responsibilities",
            "Duties and Responsibilities", "What You'll Do", "What You’ll Do",
            "Your day-to-day", "Your Day-to-Day", "Qualifications",
            "Required Qualifications", "Preferred Qualifications",
            "Required Skills", "Required Skills/Knowledge",
            "Skills and Qualifications", "What You'll Need", "What You’ll Need",
            "Education", "Experience", "Benefits", "Additional Notes",
            "Work Option", "Work Location", "Physical Requirements",
            "Equal Opportunity Employer",
        ]
        marker = re.compile(
            r"(?<![A-Za-z])(" +
            "|".join(re.escape(x) for x in sorted(headings, key=len, reverse=True)) +
            r")(?:\s*:)?(?=\s)",
            re.I,
        )
        matches = list(marker.finditer(text))
        if len(matches) >= 2:
            pieces = []
            if clean(text[:matches[0].start()]):
                pieces.append(f"<p>{html.escape(clean(text[:matches[0].start()]))}</p>")
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
                body = clean(text[match.end():end])
                pieces.append(f"<h3>{html.escape(clean(match.group(1)))}</h3>")
                if body:
                    pieces.append(f"<p>{html.escape(body)}</p>")
            out = "".join(pieces)

    return out


def pdate(v):
    if not v:
        return None

    s = clean(v)
    # Workday's "30+ Days Ago" is a lower bound, not a calendar day.
    # Fuzzy parsing can otherwise turn it into day 30 of this month and
    # clamp that future date to TODAY, making stale jobs appear new.
    m = re.search(r"(\d+)\s*\+?\s+days?\s+ago", s, re.I)
    if m:
        return TODAY - timedelta(days=int(m.group(1)))

    if "today" in s.lower():
        return TODAY

    if "yesterday" in s.lower():
        return TODAY - timedelta(days=1)

    try:
        parsed = dtparser.parse(s, fuzzy=True).date()
        # ATS feeds occasionally expose future dates due to timezone/parser quirks.
        # A job cannot have been posted after the crawl date, so clamp to TODAY.
        if parsed > TODAY:
            return TODAY
        return parsed
    except Exception:
        return None


def _req_raw(method, url, **kw):
    timeout = kw.pop("timeout", 15)
    tries = max(1, min(4, int(kw.pop("tries", 4))))
    last_retryable = None
    for n in range(tries):
        try:
            r = SESSION.request(method, url, timeout=timeout, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                last_retryable = RuntimeError(f"HTTP {r.status_code} for {url}")
                if n < tries - 1:
                    time.sleep(2**n)
                    continue
                raise last_retryable
            r.raise_for_status()
            return r
        except requests.RequestException:
            if n == tries - 1:
                raise
            time.sleep(2**n)

    # Defensive guard: never allow a failed request path to return None.
    # Callers expect a response object and may immediately access .text/.json().
    if last_retryable:
        raise last_retryable
    raise RuntimeError(f"Request failed without a response for {url}")




def req(method, url, **kw):
    _v28_before_request(url)
    last = None
    for attempt in range(3):
        try:
            r = _req_raw(method, url, **kw)
            status = getattr(r, "status_code", 200)
            if status in (429, 500, 502, 503, 504) and attempt < 2:
                last = RuntimeError(f"HTTP {status} for {url}")
                time.sleep(_v28_backoff_seconds(attempt))
                continue
            return r
        except Exception:
            continue

    return out


BATCH_DIRECT_COMPANIES = {
    "rogers sports & media",
    "cumulus media",
    "bmi",
    "associated press (ap)",
    "christian music broadcasters (cmb)",
}


def _direct_board_date(raw):
    """Find only explicit job posting dates from public job pages."""
    s = html.unescape(raw or "").replace("\\/", "/")
    patterns = [
        r"(?:Date Posted|Posted Date|Posted|Posting Date)\s*:?\s*"
        r"([A-Za-z]+\s+\d{1,2},\s+20\d{2})",
        r"(?:Date Posted|Posted Date|Posted|Posting Date)\s*:?\s*"
        r"(\d{1,2}/\d{1,2}/20\d{2})",
        r"(?:Date Posted|Posted Date|Posted|Posting Date)\s*:?\s*"
        r"(20\d{2}-\d{2}-\d{2})",
        r'["\']datePosted["\']\s*:\s*["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        for m in re.finditer(pat, s, re.I):
            d = pdate(strip_html(m.group(1)))
            if d:
                return d
    return None


def _direct_board_candidate_links(base_url, raw):
    """Extract likely individual job URLs from anchors and embedded state."""
    soup = BeautifulSoup(raw or "", "html.parser")
    base_host = urlparse(base_url).netloc.lower().replace("www.", "")
    out = set()

    job_patterns = (
        r"/job/", r"/jobs/", r"/job-detail", r"/jobdetails",
        r"/career-opportunity/", r"/positions?/", r"/opportunity/",
        r"/job-openings?/", r"/careers/jobs/",
    )

    for a in soup.find_all("a", href=True):
        h = urljoin(base_url, a["href"])
        p = urlparse(h)
        host = p.netloc.lower().replace("www.", "")
        if not host:
            continue

        # Allow same employer host and known recruiting hosts linked from it.
        same = host == base_host
        known = any(x in host for x in (
            "successfactors.com",
            "phenompeople.com",
            "eightfold.ai",
            "icims.com",
            "workdayjobs.com",
            "careerwebsite.com",
            "jobtarget.com",
        ))
        if not (same or known):
            continue

        if any(re.search(pat, h, re.I) for pat in job_patterns):
            out.add(h.split("#", 1)[0])

    # Embedded/hydrated URLs.
    raw2 = html.unescape(raw or "").replace("\\/", "/")
    for m in re.finditer(r'https?://[^"\'<>\s]+', raw2):
        h = m.group(0).rstrip(".,);")
        hp = urlparse(h)
        host = hp.netloc.lower().replace("www.", "")
        if not host:
            continue
        if (
            host == base_host
            or any(x in host for x in (
                "successfactors.com", "phenompeople.com", "eightfold.ai",
                "icims.com", "workdayjobs.com", "careerwebsite.com",
                "jobtarget.com",
            ))
        ):
            if any(re.search(pat, h, re.I) for pat in job_patterns):
                out.add(h.split("#", 1)[0])

    return out


def _direct_board_job(src, url, raw):
    # Structured JobPosting is the cleanest source.
    j = _job_from_detail(src, url, raw)
    if j:
        return j

    soup = BeautifulSoup(raw, "html.parser")
    txt = clean(soup.get_text(" "))

    pd = _direct_board_date(raw)
    if not pd or pd < CUTOFF:
        return None

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ") if h1 else "")
    if not title:
        for node in soup.find_all(["h2", "h3"]):
            cand = clean(node.get_text(" "))
            if 3 <= len(cand) <= 180:
                title = cand
                break
    if not title:
        return None

    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find(attrs={"class": re.compile(
            r"(job.?description|job.?detail|posting.?description|entry-content)",
            re.I,
        )})
        or soup
    )
    desc = clean(main.get_text(" "))
    if len(desc) < 250:
        return None

    loc = ""
    for pat in (
        r"(?:Job Location|Location)\s*:?\s*"
        r"([A-Za-z0-9 .,'/\-&]+?)(?=\s+(?:Job Type|Employment Type|Category|Department|Posted|Apply|$))",
        r"\b([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b",
    ):
        m = re.search(pat, txt)
        if m:
            loc = clean(m.group(1))
            break

    canonical = url.split("#", 1)[0]
    jid = hashlib.sha1(canonical.encode()).hexdigest()[:16]

    return Job(
        jid,
        title,
        src["Company"],
        desc,
        pd,
        jobtype(title, txt),
        category(title, desc, src["Industry"], src["Company"]),
        canonical,
        src["URL"],
        src["URL"],
        "",
        normalize_work_arrangement(desc, loc or txt),
        loc,
        "",
        infer_country(loc or txt, src["Company"], desc),
    )


def batch_direct_board(src):
    """Safe multi-employer direct-board collector for v15.

    It crawls only a bounded set of public listing/detail pages. Any bad page
    is skipped, so one employer cannot reintroduce feed-wide errors.
    """
    starts = [src["URL"]]

    # Known alternate public listing URLs for the five v15 targets.
    company = clean(src.get("Company", "")).lower()
    extras = {
        "rogers sports & media": [
            "https://jobs.rogers.com/go/Rogers-Sports-and-Media/8824500/",
        ],
        "cumulus media": [
            "https://jobs.cumulusmedia.com/jobs",
        ],
        "bmi": [
            "https://careers.bmi.com/jobs/",
        ],
        "associated press (ap)": [
            "https://careers.ap.org/go/View-All-Jobs/4304700/",
        ],
        "christian music broadcasters (cmb)": [
            "https://cmbonline.org/jobs/",
        ],
    }
    starts.extend(extras.get(company, []))
    starts = list(dict.fromkeys(starts))

    queue = list(starts)
    seen_pages = set()
    details = set()

    while queue and len(seen_pages) < 60 and len(details) < 1500:
        page = queue.pop(0)
        if page.rstrip("/") in seen_pages:
            continue
        seen_pages.add(page.rstrip("/"))

        try:
            r = req("GET", page)
        except Exception:
            continue

        final_url = str(getattr(r, "url", "") or page)
        soup = BeautifulSoup(r.text, "html.parser")

        candidates = _direct_board_candidate_links(final_url, r.text)

        for h in candidates:
            # Avoid looping on the listing root itself.
            if h.rstrip("/") == final_url.rstrip("/"):
                continue
            details.add(h)

        # Follow only obvious pagination/list navigation on the same host.
        for a in soup.find_all("a", href=True):
            label = clean(a.get_text(" ")).lower()
            href = urljoin(final_url, a["href"])
            if label in {"next", "next page", "older", "more jobs", "view more"} or re.search(
                r"(?:page|start|offset)=\d+", href, re.I
            ):
                if urlparse(href).netloc.lower() == urlparse(final_url).netloc.lower():
                    if href.rstrip("/") not in seen_pages:
                        queue.append(href)

    out = []
    seen_ids = set()

    for url in sorted(details):
        try:
            rr = req("GET", url)
            final_url = str(getattr(rr, "url", "") or url)
            j = _direct_board_job(src, final_url, rr.text)
            if j and j.id not in seen_ids:
                seen_ids.add(j.id)
                out.append(j)
        except Exception:
            continue

    return out


V16_TARGETS = {
    "cnn",
    "paramount",
    "disney / abc",
    "espn",
    "gray media",
}


def _recent_detail_job(src, url):
    """Fetch one detail page and use the shared strict JobPosting parser."""
    try:
        r = req("GET", url)
        return _job_from_detail(src, str(getattr(r, "url", "") or url), r.text)
    except Exception:
        return None


def _crawl_rendered_job_board(src, starts, allow_hosts=None, max_pages=40, max_jobs=1500):
    """Bounded server-rendered board crawler used by several v16 targets."""
    allow_hosts = {h.lower().replace("www.", "") for h in (allow_hosts or [])}
    queue = list(dict.fromkeys(starts))
    seen_pages = set()
    detail_urls = set()

    while queue and len(seen_pages) < max_pages and len(detail_urls) < max_jobs:
        page = queue.pop(0)
        key = page.rstrip("/")
        if key in seen_pages:
            continue
        seen_pages.add(key)

        try:
            r = req("GET", page)
        except Exception:
            continue

        final_url = str(getattr(r, "url", "") or page)
        soup = BeautifulSoup(r.text, "html.parser")
        final_host = urlparse(final_url).netloc.lower().replace("www.", "")

        for a in soup.find_all("a", href=True):
            h = urljoin(final_url, a["href"])
            hp = urlparse(h)
            host = hp.netloc.lower().replace("www.", "")
            if allow_hosts and host not in allow_hosts:
                continue
            if not allow_hosts and host != final_host:
                continue

            low = hp.path.lower()
            q = hp.query.lower()

            # Likely individual job detail URLs.
            if (
                re.search(r"/job/[^/]+", low)
                or re.search(r"/jobs/\d+", low)
                or "/jobdetails/" in low
                or "/job-detail/" in low
                or ("jobid=" in q and "search" not in low)
            ):
                if h.rstrip("/") != final_url.rstrip("/"):
                    detail_urls.add(h.split("#", 1)[0])
                continue

            # Follow only obvious listing pagination/navigation.
            label = clean(a.get_text(" ")).lower()
            if (
                re.search(r"\b(next|more jobs|view more|older)\b", label)
                or re.search(r"[?&](page|p|start|offset|from)=\d+", h, re.I)
                or re.search(r"/page/\d+", low)
            ):
                if h.rstrip("/") not in seen_pages:
                    queue.append(h)

        # Recover embedded job URLs from application state.
        raw = html.unescape(r.text or "").replace("\\/", "/")
        for m in re.finditer(r'https?://[^"\'<>\s]+', raw):
            h = m.group(0).rstrip(".,);")
            hp = urlparse(h)
            host = hp.netloc.lower().replace("www.", "")
            if allow_hosts and host not in allow_hosts:
                continue
            low = hp.path.lower()
            if (
                re.search(r"/job/[^/]+", low)
                or re.search(r"/jobs/\d+", low)
                or "/jobdetails/" in low
                or "/job-detail/" in low
            ):
                detail_urls.add(h.split("#", 1)[0])

    out = []
    seen_ids = set()
    for url in sorted(detail_urls):
        j = _recent_detail_job(src, url)
        if j and j.id not in seen_ids:
            seen_ids.add(j.id)
            out.append(j)
    return out



def cox_successfactors(src):
    """Cox Media Group: SAP SuccessFactors public career site.

    CMG's /viewalljobs/ page is a category landing page. The real server-
    rendered job table lives at /go/All-Jobs/9298500/ and paginates with
    SuccessFactors path offsets such as /25/... . Crawl that surface directly,
    collect canonical /job/.../<requisition-id>/ URLs, then pass each detail
    page through the project's normal recent-job validator.
    """
    base = "https://careers.cmg.com/go/All-Jobs/9298500/"
    queue = [base]
    seen_pages = set()
    detail_urls = []
    seen_detail = set()

    while queue and len(seen_pages) < 12 and len(detail_urls) < 500:
        page = queue.pop(0)
        page_key = page.split("#", 1)[0]
        if page_key in seen_pages:
            continue
        seen_pages.add(page_key)

        try:
            r = req("GET", page)
        except Exception:
            continue

        final_url = str(getattr(r, "url", "") or page)
        soup = BeautifulSoup(r.text, "html.parser")

        for a in soup.find_all("a", href=True):
            h = urljoin(final_url, a["href"]).split("#", 1)[0]
            hp = urlparse(h)
            host = hp.netloc.lower().replace("www.", "")
            if host != "careers.cmg.com":
                continue

            low = hp.path.lower()

            # CMG canonical individual job details look like:
            # /job/Orlando-Media-Consultant.../1403972500/
            if re.search(r"/job/[^/]+/\d+/?$", low):
                if h not in seen_detail:
                    seen_detail.add(h)
                    detail_urls.append(h)
                continue

            # Follow SuccessFactors "All Jobs" pagination regardless of whether
            # the anchor label is a number, chevron, or Next.
            if low.startswith("/go/all-jobs/9298500/"):
                if h.rstrip("/") != base.rstrip("/") and h not in seen_pages:
                    queue.append(h)

        # Defensive recovery of embedded canonical job URLs.
        raw = html.unescape(r.text or "").replace("\\/", "/")
        for m in re.finditer(
            r'https?://careers\.cmg\.com/job/[^"\'<>\s]+?/\d+/?',
            raw,
            re.I,
        ):
            h = m.group(0).rstrip(".,);")
            if h not in seen_detail:
                seen_detail.add(h)
                detail_urls.append(h)

    out = []
    seen_ids = set()

    for url in detail_urls:
        try:
            r = req("GET", url)
        except Exception:
            continue

        final = str(getattr(r, "url", "") or url)
        soup = BeautifulSoup(r.text, "html.parser")
        html_text = r.text or ""

        # Canonical SuccessFactors requisition id is the trailing numeric URL id.
        m_id = re.search(r"/(\d+)/?(?:\?|$)", urlparse(final).path + "?")
        if not m_id:
            m_id = re.search(r"/(\d+)/?$", urlparse(final).path)
        if not m_id:
            continue
        jid = m_id.group(1)

        # Prefer JobPosting JSON-LD title/date/location/description.
        jld = None
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                obj = json.loads(sc.string or sc.get_text() or "{}")
            except Exception:
                continue
            candidates = obj if isinstance(obj, list) else [obj]
            for cand in candidates:
                if isinstance(cand, dict) and str(cand.get("@type","")).lower() == "jobposting":
                    jld = cand
                    break
            if jld:
                break

        # Cox's JSON-LD/page heading can be polluted by OneTrust and return
        # "Cookie Consent Manager". The visible job body is authoritative:
        # "Job Title: <real title> Position Overview ..."
        visible_title = clean(soup.get_text(" ", strip=True))
        mt = re.search(
            r"\bJob Title:\s*(.+?)\s+Position Overview\b",
            visible_title,
            re.I,
        )
        title = clean(mt.group(1)) if mt else ""

        # Defensive fallback only if Cox changes the body labels.
        if not title:
            jld_title = clean((jld or {}).get("title", ""))
            if jld_title.lower() not in {
                "cookie consent manager",
                "cookie manager",
                "privacy preference center",
            }:
                title = jld_title

        if not title:
            for h in soup.find_all(["h1", "h2"]):
                cand = clean(h.get_text(" ", strip=True))
                if cand and cand.lower() not in {
                    "cookie consent manager",
                    "cookie manager",
                    "privacy preference center",
                    "cox media group careers",
                    "careers",
                }:
                    title = cand
                    break

        if not title:
            continue

        # Cox SuccessFactors exposes current posting dates either in JSON-LD or
        # visible labels such as "Date: Aug 28, 2026".
        pd = None
        raw_date = clean((jld or {}).get("datePosted", ""))
        if raw_date:
            pd = parsedate(raw_date)
        if not pd:
            visible = soup.get_text(" ", strip=True)
            for pat in [
                r"(?:Date Posted|Posted Date|Date)\s*[:\-]\s*"
                r"([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})",
                r"(?:Date Posted|Posted Date|Date)\s*[:\-]\s*"
                r"(\d{1,2}/\d{1,2}/\d{4})",
            ]:
                md = re.search(pat, visible, re.I)
                if md:
                    pd = parsedate(md.group(1))
                    if pd:
                        break
        # Cox does not reliably expose a posting date on its public
        # SuccessFactors detail pages. Per MJR feed rules, an open job with no
        # employer posting date uses the MJR discovery date.
        if not pd:
            pd = datetime.now(
                ZoneInfo("America/New_York")
            ).date()

        if pd < CUTOFF:
            continue

        desc_html = ""
        if jld and jld.get("description"):
            desc_html = str(jld.get("description"))
        if not desc_html:
            node = soup.select_one(
                ".jobdescription, .job-description, [itemprop='description'], "
                ".jobDescription, #job-description"
            )
            if node:
                desc_html = str(node)
        description_text = clean(BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True))
        if len(description_text) < 80:
            description_text = clean(soup.get_text(" ", strip=True))
        description_html = format_description(desc_html)
        if len(strip_html(description_html)) < 80:
            description_html = format_description(description_text)

        location = get_location_from_jsonld(jld) if isinstance(jld, dict) else ""

        # Cox prominently exposes the public location directly beneath the
        # title, e.g. "Orlando, FL, US, 32801".
        if not location:
            visible = soup.get_text("\n", strip=True)
            mloc_visible = re.search(
                r"(?:^|\n)Location:\s*\n?\s*"
                r"([^,\n]+),\s*([A-Z]{2}),\s*(US|USA)"
                r"(?:,\s*\d{5})?",
                visible,
                re.I,
            )
            if mloc_visible:
                location = (
                    f"{clean(mloc_visible.group(1))}, "
                    f"{mloc_visible.group(2).upper()}, US"
                )

        city, state, country = "", "", "US"
        if location:
            mloc = re.match(
                r"^([^,]+),\s*([A-Z]{2})(?:\s|,|$)",
                location,
                re.I,
            )
            if mloc:
                city = clean(mloc.group(1))
                state = mloc.group(2).upper()
            else:
                city = clean(location)

        live_apply = final.split("?",1)[0]

        # Run classification from the REAL Cox title, never cookie/privacy text.
        cat = category(title, description_text, src["Industry"], src["Company"])

        # Cox employment type must be explicit. The global helper's broad
        # "part" + "time" text search can false-positive on long descriptions.
        tlow = title.lower()
        dlow = description_text.lower()
        if re.search(r"\b(intern|internship)\b", tlow):
            jt = "Internship"
        elif re.search(r"\bpart[\s-]*time\b|\(pt\)", tlow):
            jt = "Part Time"
        elif re.search(r"\b(full[\s-]*time)\b|\(ft\)", tlow):
            jt = "Full Time"
        elif re.search(
            r"\b(this is|this role is|position is|this position is)\s+(?:an?\s+)?part[\s-]*time\b",
            dlow,
        ):
            jt = "Part Time"
        elif re.search(r"\btemporary\b|\btemp position\b", tlow):
            jt = "Temporary"
        elif re.search(r"\bcontract(?:or)?\b", tlow):
            jt = "Contract"
        else:
            jt = "Full Time"

        # Cox-specific MJR category safeguards.
        low = title.lower()
        context = (title + " " + description_text[:2200]).lower()

        # Commercial traffic/continuity/log scheduling = Business Office.
        # Covers both "Traffic Director" and Cox's "Dir, Traffic - TV" naming.
        if (
            re.search(r"\btraffic (coordinator|assistant|director|manager)\b", low)
            or re.search(r"\b(dir|director|manager|coordinator|assistant)[,\s-]+traffic\b", low)
        ) and re.search(
            r"\b(logs?|commercial|spots?|inventory|wide ?orbit|billing|master control|"
            r"sales operations|continuity|advertiser|agency profiles?)\b",
            context,
        ):
            cat = "Business Office"

        # On-air traffic reporter/anchor = platform category; Cox Radio defaults Radio.
        elif re.search(r"\btraffic (reporter|anchor)\b", low):
            cat = "Television" if re.search(r"\b(tv|television|w[a-z]{2,4}-?tv)\b", low) else "Radio"

        # Clear radio talent/programming/board roles.
        elif re.search(
            r"\b(on[- ]air|air talent|board operator|program director|"
            r"director of operations.*radio|branding.*programming)\b",
            low,
        ) and "radio" in context:
            cat = "Radio"

        # Clear TV newsroom/on-camera/production roles.
        elif re.search(
            r"\b(anchor|reporter|meteorologist|news producer|associate news producer|"
            r"investigative producer|multimedia journalist|news writer)\b",
            low,
        ) and re.search(r"\b(tv|television|w[a-z]{2,4}-?tv|telemundo)\b", context):
            cat = "Journalism"

        # Commercial creative-production roles are Television, not Sales.
        elif re.search(r"\bcommercial (editor|producer|writer|videographer)\b", low) and re.search(
            r"\b(tv|television|telemundo|w[a-z]{2,4}-?tv)\b",
            context,
        ):
            cat = "Television"

        # Sales/revenue roles must beat Digital/Business Office.
        elif re.search(
            r"\b(account executive|media consultant|sales development representative|"
            r"business development consultant|general sales manager|digital media director|"
            r"marketing solutions coordinator|client performance manager)\b",
            low,
        ) or re.search(r"\b(drive|driving|generate|growing?) (?:digital )?revenue\b", context):
            cat = "Sales & Marketing"

        wa = normalize_work_arrangement(description_text, location)

        # Explicit negative remote language must override generic remote keywords.
        # Example Cox copy: "This is not a remote position."
        if re.search(
            r"\b(?:not a remote position|not remote|no remote|remote work (?:is )?not "
            r"(?:available|offered|permitted)|must work (?:on[- ]?site|in[- ]person))\b",
            dlow,
        ):
            wa = "On-Site"

        job = Job(
            jid,
            title,
            src["Company"],
            description_html,
            pd,
            jt,
            cat,
            live_apply,
            src["URL"],
            "https://www.coxmediagroup.com/",
            "",
            wa,
            city,
            state,
            country,
            None,
        )

        if jid not in seen_ids:
            seen_ids.add(jid)
            out.append(job)

    return out


def fox_public(src):
    """Efficient FOX Careers collector.

    FOX search pages can expose hundreds of detail links. To stay safely under the
    crawler's per-domain request cap, enumerate search pages first, stop as soon as
    the retained/new-job quota is satisfied, and cap detail requests below the
    global domain ceiling.
    """
    base = "https://www.foxcareers.com"
    search_url = base + "/Search/SearchResults"
    out = []
    seen = set()

    # Leave headroom beneath the global 175-request/domain safeguard for search
    # pages, retries and any other FOX requests made during the same crawl.
    max_detail_requests = 145
    detail_requests = 0

    # FOX results are newest-first. We only need enough detail pages to populate
    # current jobs inside MJR's retention window.
    for page in range(0, 20):
        if detail_requests >= max_detail_requests:
            break

        url = search_url + f"?page={page}&language=en"
        r = req("GET", url)
        soup = BeautifulSoup(r.text, "html.parser")

        links = []
        page_seen = set()
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "/Search/JobDetail/" not in href:
                continue
            full = urljoin(base, href)
            rid = re.search(r"/JobDetail/(R\d+)", full, re.I)
            key = rid.group(1).upper() if rid else full.lower()
            if key in page_seen or key in seen:
                continue
            page_seen.add(key)
            links.append((key, full))

        if not links:
            break

        page_current = 0
        page_stale = 0

        for key, detail_url in links:
            if detail_requests >= max_detail_requests:
                break
            if key in seen:
                continue
            seen.add(key)

            detail_requests += 1
            dr = req("GET", detail_url)
            ds = BeautifulSoup(dr.text, "html.parser")
            page_text = clean(ds.get_text(" ", strip=True))

            posted_match = re.search(
                r"Job Posting Date:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
                page_text,
                re.I,
            )
            posted = pdate(posted_match.group(1)) if posted_match else None
            if not posted:
                continue

            # Because FOX search results are newest-first, stale jobs tell us when
            # we've crossed MJR's normal retention window.
            if posted < CUTOFF:
                page_stale += 1
                continue

            page_current += 1

            h1 = ds.find("h1")
            title = clean(h1.get_text(" ", strip=True) if h1 else "")
            if not title:
                continue

            rid_match = re.search(r"Job Number:\s*(R\d+)", page_text, re.I)
            rid = rid_match.group(1).upper() if rid_match else key

            brand_match = re.search(
                r"Job Number:\s*R\d+\s+Brand\s+(.+?)\s+Job Type:",
                page_text,
                re.I,
            )
            brand = clean(brand_match.group(1)) if brand_match else clean(src.get("Company", "FOX"))
            brand = re.sub(r"Error:\s*No label for:\s*SubBrand\s*", "", brand, flags=re.I).strip()
            if re.search(r"\bBig Ten Network\b", page_text, re.I):
                brand = "FOX Sports / Big Ten Network"

            loc_match = re.search(
                r"\bLocation\s+(.+?)\s+Job Posting Date:",
                page_text,
                re.I,
            )
            location = clean(loc_match.group(1)) if loc_match else ""
            location = re.sub(r"\s*;\s*", "; ", location).strip(" ;")

            city = ""
            state = ""
            country = "US"
            primary_loc = location.split(";")[0].strip() if location else ""
            if re.search(r"\bCanada\b", location, re.I):
                country = "CA"
            elif re.search(r"\bUnited Kingdom\b|\bLondon\b", location, re.I):
                country = "GB"
            elif re.search(r"\bIndia\b", location, re.I):
                country = "IN"
            elif re.search(r"\bAustralia\b", location, re.I):
                country = "AU"

            if primary_loc:
                parts = [clean(x) for x in primary_loc.split(",") if clean(x)]
                if len(parts) >= 2:
                    city, state = parts[0], parts[1]
                elif "remote" not in primary_loc.lower():
                    city = primary_loc

            desc_parts = []
            heading = None
            for tag in ds.find_all(["h2", "h3"]):
                if "JOB DESCRIPTION" in clean(tag.get_text(" ", strip=True)).upper():
                    heading = tag
                    break
            if heading:
                for sib in heading.find_all_next():
                    if sib is heading:
                        continue
                    txt = clean(sib.get_text(" ", strip=True))
                    if not txt:
                        continue
                    if sib.name in {"h1", "h2", "h3"} and (
                        "BACK TO SEARCH" in txt.upper() or "PRIVACY" in txt.upper()
                    ):
                        break
                    if sib.name in {"p", "ul", "ol", "h3", "h4"}:
                        desc_parts.append(str(sib))
                    if "equal opportunity employer" in txt.lower():
                        break

            description = format_description("".join(desc_parts))
            if len(strip_html(description)) < 150:
                description = format_description(page_text)

            work = normalize_work_arrangement(description, location, title)
            if re.search(r"\bremote\b", page_text, re.I):
                work = normalize_work_arrangement(
                    description + " Remote", location, title, work
                )

            out.append(Job(
                f"fox-{rid}",
                title,
                brand or "FOX",
                description,
                posted,
                jobtype(title, description),
                (
                    "Journalism"
                    if re.search(r"\b(multimedia reporters?|reporter/streaming host|news reporter)\b", title, re.I)
                    else "Television"
                    if re.search(r"\b(news production director|production automation technician)\b", title, re.I)
                    else category(title, description, "Television", brand or "FOX")
                ),
                detail_url,
                src.get("URL", search_url),
                base,
                "",
                work,
                city,
                state,
                country,
            ))

        # Once a FOX results page contains stale postings and no current postings,
        # we've passed the retention window; do not burn requests on older pages.
        if page_current == 0 and page_stale > 0:
            break

        # If most of the page is already stale, the next page will be older.
        if page_stale > page_current and page_stale >= 5:
            break

    print(
        f"FOX public careers: {len(out)} current jobs "
        f"({detail_requests} detail requests)"
    )
    return out


def paramount_successfactors(src):
    """Paramount SAP SuccessFactors collector — v71.

    Paramount's public board lazy-loads results beyond the first 25.  Use one
    lightweight Playwright listing session to reveal the full board, capture
    individual job URLs plus their listing dates, then request detail pages only
    for jobs inside MJR's freshness window.

    Targeted tests remain isolated by the existing MJR_TEST_COMPANIES controls.
    """
    listing = "https://careers.paramount.com/go/All-Current-Job-Opportunities/8710000/"
    diag = []
    discovered = {}
    detail_urls = []

    def d(msg):
        diag.append(str(msg))

    def extract_date(text):
        # SuccessFactors listing uses dates like "Aug 31, 2026".
        for pat in (
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\s+\d{1,2},\s+20\d{2}\b",
            r"\b\d{1,2}/\d{1,2}/20\d{2}\b",
        ):
            m = re.search(pat, text or "", re.I)
            if m:
                return pdate(m.group(0))
        return None

    d("PARAMOUNT SUCCESSFACTORS v71")
    d(f"source={src.get('URL','')}")
    d(f"listing={listing}")

    # First try the fully rendered listing.  Paramount exposes a "More Search
    # Results" control that appends additional rows.
    if sync_playwright is not None:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/152.0.0.0 Safari/537.36"
                    )
                )
                page.goto(listing, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    page.wait_for_timeout(3000)

                last_count = -1
                stable_rounds = 0

                for round_no in range(1, 30):
                    rows = page.evaluate(
                        """() => {
                          const out = [];
                          for (const a of document.querySelectorAll('a[href]')) {
                            const href = a.href || '';
                            if (!/careers\\.paramount\\.com\\/job\\//i.test(href)) continue;
                            let box = a.closest('li, tr, article, .job, .jobResultItem, .searchResultsShell');
                            if (!box) box = a.parentElement;
                            out.push({
                              href,
                              text: (box && box.innerText) ? box.innerText : (a.innerText || '')
                            });
                          }
                          return out;
                        }"""
                    )

                    for row in rows:
                        href = (row.get("href") or "").split("#", 1)[0]
                        if not href:
                            continue
                        pd = extract_date(row.get("text") or "")
                        prev = discovered.get(href)
                        # Keep a discovered listing date when available.
                        if href not in discovered or (not prev and pd):
                            discovered[href] = pd

                    count = len(discovered)
                    d(f"LISTING round={round_no} discovered={count}")

                    if count == last_count:
                        stable_rounds += 1
                    else:
                        stable_rounds = 0
                    last_count = count

                    # Stop if the full board appears loaded and the button is gone,
                    # or if multiple rounds produce no new jobs.
                    more = page.get_by_role("button", name=re.compile(r"More Search Results", re.I))
                    if more.count() == 0:
                        # Some SuccessFactors themes render a link/div instead.
                        more = page.get_by_text(re.compile(r"More Search Results", re.I), exact=False)

                    if more.count() == 0 or stable_rounds >= 2:
                        break

                    clicked = False
                    for i in range(min(more.count(), 3)):
                        try:
                            el = more.nth(i)
                            if el.is_visible():
                                el.click(timeout=8000)
                                clicked = True
                                page.wait_for_timeout(1800)
                                break
                        except Exception:
                            continue
                    if not clicked:
                        break

                browser.close()
        except Exception as e:
            d(f"PLAYWRIGHT_ERROR {type(e).__name__}:{e}")

    # Server-rendered fallback if Playwright failed to expose links.
    if not discovered:
        try:
            r = req("GET", listing)
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = urljoin(listing, a["href"]).split("#", 1)[0]
                if urlparse(href).netloc.lower().replace("www.", "") != "careers.paramount.com":
                    continue
                if "/job/" not in urlparse(href).path.lower():
                    continue
                box = a.find_parent(["li", "tr", "article", "div"])
                txt = clean(box.get_text(" ")) if box else clean(a.get_text(" "))
                discovered[href] = extract_date(txt)
            d(f"SERVER_FALLBACK discovered={len(discovered)}")
        except Exception as e:
            d(f"SERVER_FALLBACK_ERROR {type(e).__name__}:{e}")

    # Prefer listing-date prefiltering. Unknown-date links remain eligible so a
    # detail page can make the final freshness decision.
    fresh_candidates = []
    dated_old = 0
    for href, pd in discovered.items():
        if pd and pd < CUTOFF:
            dated_old += 1
            continue
        fresh_candidates.append((href, pd))

    # Newest first where the listing supplied a date.
    fresh_candidates.sort(key=lambda x: (x[1] or date.min), reverse=True)
    detail_urls = [u for u, _ in fresh_candidates]

    d(f"DISCOVERED_TOTAL={len(discovered)}")
    d(f"LISTING_OLD_SKIPPED={dated_old}")
    d(f"DETAIL_CANDIDATES={len(detail_urls)}")

    out = []
    seen_ids = set()

    for url, listing_date in fresh_candidates[:300]:
        try:
            r = req("GET", url)
            final = str(getattr(r, "url", "") or url).split("#", 1)[0]
            soup = BeautifulSoup(r.text or "", "html.parser")
            visible = clean(soup.get_text(" ", strip=True))

            # Paramount detail pages expose the real title in H1.
            h1 = soup.find("h1")
            title = clean(h1.get_text(" ", strip=True) if h1 else "")
            if not title:
                d(f"REJECT title_missing {final}")
                continue

            # Req ID is visible on the detail page and is a better stable ID than
            # the large SuccessFactors URL object ID.
            jid = ""
            m_req = re.search(r"\bReq ID:\s*([A-Z0-9_-]{3,})\b", visible, re.I)
            if m_req:
                jid = clean(m_req.group(1))
            if not jid:
                # Paramount also places the requisition number directly under H1.
                h1_parent = h1.parent if h1 else None
                near = clean(h1_parent.get_text(" ", strip=True)) if h1_parent else visible[:1000]
                m_req = re.search(r"\b(\d{4,8})\b", near)
                if m_req:
                    jid = m_req.group(1)
            if not jid:
                m_url = re.search(r"/(\d{6,12})/?$", urlparse(final).path)
                jid = m_url.group(1) if m_url else hashlib.sha1(final.encode()).hexdigest()[:16]
            if jid in seen_ids:
                continue

            # The listing date is authoritative for Paramount. Detail pages do
            # not reliably repeat the posting date in machine-readable form.
            pd = listing_date or TODAY
            if pd < CUTOFF:
                continue

            # Description: prefer the main job body and remove navigation noise.
            main = (
                soup.select_one(".jobdescription, .job-description, [itemprop='description'], "
                                ".jobDescription, #job-description")
                or soup.find("main")
                or soup
            )
            desc = format_description(str(main))
            desc_text = strip_html(desc)
            if len(desc_text) < 200:
                d(f"REJECT description_short {jid} len={len(desc_text)}")
                continue

            # Header metadata is compact and appears before the main description:
            # title / req / location / function / market / job type / work model.
            head = clean(" ".join(
                x.get_text(" ", strip=True)
                for x in soup.find_all(["h1","h2","div","span","p"])[:120]
            ))

            # Location examples:
            # New York, NY, US, 10019
            # Milan, Milan, IT, 20121
            # Budapest, HU
            city = state = ""
            country = "US"
            loc_match = re.search(
                r"\b([A-Za-zÀ-ÿ .'-]+),\s*([A-Za-z]{2,30}),\s*"
                r"(US|USA|CA|GB|UK|IT|RO|HU|DE|FR|ES|NL|PL|AU|NZ|MX|BR|AR|CL)"
                r"(?:,\s*[A-Z0-9 -]{3,10})?\b",
                visible[:1800],
                re.I,
            )
            if loc_match:
                city = clean(loc_match.group(1))
                region = clean(loc_match.group(2))
                cc = loc_match.group(3).upper()
                country = {"USA":"US","UK":"GB"}.get(cc, cc)
                if country in {"US","CA"}:
                    state = region.upper() if len(region) == 2 else region
                else:
                    state = "" if region.lower() == city.lower() else region
            else:
                loc2 = re.search(
                    r"\b([A-Za-zÀ-ÿ .'-]+),\s*(US|USA|CA|GB|UK|IT|RO|HU|DE|FR|ES|NL|PL|AU|NZ|MX|BR|AR|CL)\b",
                    visible[:1800],
                    re.I,
                )
                if loc2:
                    city = clean(loc2.group(1))
                    cc = loc2.group(2).upper()
                    country = {"USA":"US","UK":"GB"}.get(cc, cc)

            # Explicit Paramount job type.
            meta = visible[:2200]
            if re.search(r"\bInternship\b", meta, re.I) or re.search(r"\b(intern|internship)\b", title, re.I):
                jt = "Internship"
            elif re.search(r"\bPart[- ]Time\b", meta, re.I):
                jt = "Part Time"
            elif re.search(r"\bTemporary\b|\bPer Diem\b|\bFreelance\b|\bNon-Staff\b", meta, re.I):
                jt = "Temporary"
            else:
                jt = "Full Time"

            # Paramount work arrangement must come from an explicit job-level
            # statement. Do not classify from generic benefits/event boilerplate
            # that happens to contain words such as "remote" or "virtual".
            wa = "On-Site"

            # First look for compact SuccessFactors header labels.
            header_slice = visible[:2600]
            work_model = ""
            for pat in (
                r"\bWork(?:place|ing)?\s*(?:Model|Arrangement|Location|Type)\s*[:\-]?\s*"
                r"(On[- ]?Site|Onsite|Hybrid|Remote|Office First)\b",
                r"\bJob\s*Type\s*[:\-]?\s*(?:Full[- ]?Time|Part[- ]?Time|Temporary|Internship)"
                r".{0,180}?\b(On[- ]?Site|Onsite|Hybrid|Remote|Office First)\b",
            ):
                mm = re.search(pat, header_slice, re.I | re.S)
                if mm:
                    work_model = clean(mm.group(1))
                    break

            wm = work_model.lower()
            if wm:
                if "hybrid" in wm:
                    wa = "Hybrid"
                elif "remote" in wm:
                    wa = "Remote"
                else:
                    wa = "On-Site"
            else:
                # Fall back to the conservative global v64 classifier. It
                # requires language about THIS position, not incidental mentions.
                wa = normalize_work_arrangement(
                    desc,
                    ", ".join(x for x in [city, state, country] if x),
                )

            cat = category(title, desc, src["Industry"], src["Company"])
            low = title.lower()
            dlow = desc_text.lower()

            if jt == "Internship":
                cat = "Internships"

            # Definitive MJR title-first rules.
            elif re.search(r"\bsales\b", low) and "salesforce" not in low:
                cat = "Sales & Marketing"

            elif re.search(r"\b(business supervisor|business director|director,? business|supervisor,? business)\b", low):
                cat = "Business Office"

            elif re.search(r"\b(master control|master control operator|master control supervisor|master control coordinator)\b", low):
                cat = "Television"

            elif re.search(r"\b(production technician|assignment desk(?: assistant| editor| manager| coordinator)?)\b", low):
                cat = "Television"

            # Engineering / technical roles first.
            elif re.search(
                r"\b(software|data engineer|data scientist|machine learning|ml|devops|cloud|"
                r"network|systems?|security|cyber|technology|technical|engineer|engineering|"
                r"broadcast maintenance|maintenance technician|403\s*\(?g\)?\s*(?:maintenance )?technician|"
                r"media operations specialist)\b",
                low,
            ) and not re.search(r"\b(sales|marketing|account executive)\b", low):
                cat = "Engineering"

            # Sales, advertising, revenue and marketing.
            elif re.search(
                r"\b(account executive|account manager|sales|advertising|ad sales|revenue|"
                r"business development|account management|partnership marketing|"
                r"integrated marketing|performance marketing|retail marketing|"
                r"partnership sales|sponsorship|biddable platforms)\b",
                low,
            ):
                cat = "Sales & Marketing"

            # Product / UX / streaming / social are Digital unless technical
            # engineering rules above already matched.
            elif re.search(
                r"\b(product management|product manager|product designer|ux|ui|"
                r"digital|streaming|social media|social video|merchandising)\b",
                low,
            ):
                cat = "Digital"

            # CBS/local television newsroom and production functions.
            elif re.search(
                r"\b(anchor|meteorologist|weather anchor|news producer|executive producer|"
                r"assignment editor|assignment desk|photojournalist|photographer|newscast|broadcast director|master control|production technician|"
                r"tv producer|television producer|studio technician|broadcast technician|"
                r"news director|producer/editor|multi-skilled producer|line producer|"
                r"multi-skilled journalist|sports reporter|toc/editor)\b",
                low,
            ) or (
                re.search(r"\bcbs news and stations\b|\bcbs television stations\b|\bwjz\b|\bwwj-tv\b", dlow)
                and re.search(r"\b(producer|director|reporter|anchor|news|studio|photojournalist|photographer)\b", low)
            ):
                cat = "Television"

            elif re.search(r"\bproducer\b", low) and not re.search(r"\b(digital|web|social|podcast|streaming)\b", low):
                cat = "Television"

            elif re.search(r"\b(reporter|journalist|editorial|news writer|correspondent|staff writer)\b", low):
                cat = "Journalism"

            # Finance/accounting/administrative and operational analyst roles.
            elif re.search(
                r"\b(rtr|record to report|t&e|tande|travel and expense|"
                r"business analyst|financial analyst|finance analyst|accountant|"
                r"pension|internal audit|auditor|royalty compliance|fp&a|"
                r"office coordinator|operations assistant|executive assistant)\b",
                low,
            ):
                cat = "Business Office"

            # Catch vague analyst titles using functional description context.
            elif re.search(r"\banalyst\b", low):
                if re.search(
                    r"\b(accounting|general ledger|record to report|travel and expense|"
                    r"accounts payable|accounts receivable|finance|financial reporting|"
                    r"audit|pension|payroll|treasury|tax|collections)\b",
                    dlow[:3500],
                ):
                    cat = "Business Office"
                elif re.search(
                    r"\b(data pipeline|sql|python|data engineering|machine learning|"
                    r"data infrastructure|etl|analytics engineering)\b",
                    dlow[:3500],
                ):
                    cat = "Engineering"
                elif re.search(
                    r"\b(marketing analytics|audience analytics|digital analytics|"
                    r"streaming analytics|product analytics)\b",
                    dlow[:3500],
                ):
                    cat = "Digital"

            j = Job(
                jid,
                title,
                src["Company"],
                desc,
                pd,
                jt,
                cat,
                final,
                src["URL"],
                "https://www.paramount.com/",
                "",
                wa,
                city,
                state,
                country,
            )

            seen_ids.add(j.id)
            out.append(j)

            if len(out) <= 100:
                d(
                    f"ACCEPT {j.id} title={j.title} type={j.jobtype} "
                    f"cat={j.category} loc={j.city},{j.state},{j.country} "
                    f"wa={j.work_arrangement} date={j.date}"
                )
        except Exception as e:
            d(f"DETAIL_ERROR {url} {type(e).__name__}:{e}")

    d(f"FINAL={len(out)}")
    Path("mjr-paramount-diagnostic-v72.txt").write_text(
        "\n".join(diag) + "\n", encoding="utf-8"
    )
    return out


def company_scope_rejection_reason(job_or_dict):
    """Return a reason when a conglomerate job is outside MJR's media scope."""
    get = (
        (lambda key, default="": getattr(job_or_dict, key, default))
        if isinstance(job_or_dict, Job)
        else (lambda key, default="": job_or_dict.get(key, default))
    )
    company = clean(get("company", "")).lower()
    title = clean(get("title", "")).lower()
    description = strip_html(get("description", "")).lower()[:7000]
    location = clean(" ".join(
        str(get(field, "") or "") for field in ("city", "state")
    )).lower()
    text = f" {title} {description} "

    if company in {"disney / abc", "espn"}:
        # MJR carries U.S. and Canadian jobs only. Disney's keyword search can
        # silently mix in international openings even when the source is the
        # ESPN or ABC surface.
        foreign_location = re.search(
            r"\b(united kingdom|england|scotland|wales|ireland|france|germany|"
            r"spain|italy|portugal|belgium|switzerland|austria|netherlands|"
            r"sweden|norway|denmark|finland|poland|czechia|romania|greece|"
            r"israel|india|china|japan|singapore|australia|new zealand|"
            r"south africa|argentina|brazil|chile|colombia|mexico|"
            r"hong kong|taiwan|philippines|indonesia|malaysia|thailand|"
            r"united arab emirates|saudi arabia|qatar)\b|"
            r"(?:^|[, /-])(?:hk|gb|uk|fr|de|es|it|pt|be|ch|at|nl|se|no|dk|"
            r"fi|pl|cz|ro|gr|il|in|cn|jp|sg|au|nz|za|ar|br|cl|co|mx|tw|ph|"
            r"id|my|th|ae|sa|qa)(?:$|[, /-])",
            location,
        )
        if foreign_location:
            return "Disney/ESPN job outside the United States and Canada"

        # Disney/ESPN search surfaces can also return international recruiting
        # event postings with no structured city/state. In that case the
        # foreign place appears only in the title/description, so reject clear
        # recruiting-event notices before they can enter the MJR feed.
        foreign_recruiting_event = (
            re.search(
                r"\\b(recruit(?:ing|ment)|hiring|selezione del personale|career(?:s)? event|job fair|open day)\\b",
                text,
            )
            and re.search(
                r"\\b(rome|roma|bari|milan|milano|paris|london|madrid|barcelona|"
                r"berlin|munich|amsterdam|dublin|lisbon|vienna|zurich|geneva|"
                r"france|germany|spain|italy|portugal|ireland|united kingdom)\\b",
                text,
            )
        )
        if foreign_recruiting_event:
            return "Disney/ESPN international recruiting event outside media scope"

        # Clear hospitality, parks, cruise, retail and physical-trade titles.
        # These occasionally leak into Disney's ABC/ESPN search surfaces.
        hard_nonmedia_title = re.search(
            r"\b(chef(?: de partie| de rang)?|demi chef|cook|culinary|"
            r"food\s*(?:&|and)\s*beverage|food service|restaurant server|"
            r"banquet server|cocktail server|bartender|busser|dishwasher|"
            r"housekeep(?:er|ing)|custodial|custodian|lifeguard|"
            r"supply and distribution worker|warehouse worker|"
            r"hotel utility|hotel operations|front desk|bellperson|concierge|"
            r"spa attendant|massage therapist|cosmetologist|"
            r"ride operator|attractions? operator|parking attendant|"
            r"cast member|senior cast member|assistant store manager|store manager|"
            r"merchandise|retail associate|product designer.*apparel|apparel designer|"
            r"entertainment technician|garment technician|parade float driver|"
            r"quality assurance inspector.*mechanical|senior artist.*figure|"
            r"server assistant|claims examiner|"
            r"horticulturist|gardener|"
            r"plumber|carpenter|electrician|hvac|maintenance mechanic|"
            r"construction manager|construction superintendent|construction estimator|"
            r"sculptor|figure finisher|mold maker|scenic painter|artisan|"
            r"animal keeper|ecommerce specialist\s*-\s*video chat)\b",
            title,
        )
        if hard_nonmedia_title:
            return "Disney parks/resort/cruise or hospitality title outside media scope"

        nonmedia_operation_context = re.search(
            r"\b(walt disney world|disneyland resort|disney cruise line|"
            r"disney vacation club|theme park|parks and resorts|parks, resorts|"
            r"resort hotel|guest vacation|disney live entertainment|"
            r"walt disney imagineering|corporate real estate|building systems|"
            r"grounds and hardscapes|facilities maintenance|food and beverage)\b",
            description,
        )
        nonmedia_function_title = re.search(
            r"\b(manager,? operations|operations manager|infrastructure services|"
            r"facilities|building maintenance|maintenance manager|"
            r"creative & advanced development|creative and advanced development|"
            r"guest services|vacation planner|resort|cruise|"
            r"sculpt|statue|figure finishing|mold making|fabrication)\b",
            title,
        )
        if nonmedia_operation_context and nonmedia_function_title:
            return "Disney non-media operations role outside ABC/ESPN scope"

    # Employer-specific non-media exclusions. These are intentionally narrow:
    # MJR still keeps legitimate media support functions such as engineering,
    # software/IT, sales, accounting, legal, HR and newsroom administration.
    if company in {"disney / abc", "espn"}:
        newly_confirmed_disney_nonmedia = re.search(
            r"\b(recreation host(?:ess)?|pbx (?:phone )?operator|catering server|"
            r"costuming project analyst(?: intern)?|costuming.*(?:analyst|intern)|"
            r"guest services?|vacation planner|resort operations?|"
            r"food service|catering|hospitality)\b",
            title,
        )
        disney_experiences_tech = (
            re.search(r"\bdisney experiences\b", text)
            and re.search(
                r"\b(senior )?technical project manager|technology project manager|"
                r"technical program manager\b",
                title,
            )
            and not re.search(
                r"\b(abc|espn|broadcast|newsroom|news|radio|television|tv|"
                r"streaming|digital media|production|studio|ad tech|advertising)\b",
                text,
            )
        )
        if newly_confirmed_disney_nonmedia or disney_experiences_tech:
            return "Disney/ESPN non-media parks, hospitality, costuming or Experiences role"

        # Disney search surfaces can also bleed theme-park education and
        # vacation/contact-center jobs into ESPN/ABC results. Keep this narrow
        # so genuine ESPN/ABC internships, sales and technology roles survive.
        disney_guest_experience_role = (
            re.search(
                r"\b(conservation education presenter|wilderness explorer|"
                r"vacation planning|consumer direct.*specialist|"
                r"guest service.*specialist|contact center.*specialist)\b",
                text,
            )
            and re.search(
                r"\b(disney'?s animal kingdom|walt disney world|disney central|"
                r"vacation planning|guest service|guests?|theme park)\b",
                text,
            )
            and not re.search(
                r"\b(abc|espn|broadcast|newsroom|journalis|radio|television|tv|"
                r"streaming|digital media|production|studio|sports media)\b",
                text,
            )
        )
        if disney_guest_experience_role:
            return "Disney theme-park education or vacation/contact-center role outside media scope"

    if company in {"fox", "fox corporation", "fox television stations"}:
        fox_nonmedia_title = re.search(
            r"\b(catering|hospitality|mailroom|mail room|receiving|"
            r"facilities (?:assistant|coordinator|manager|specialist|technician)|"
            r"building maintenance|custodial|custodian|food service)\b",
            title,
        )
        if fox_nonmedia_title:
            return "FOX hospitality, receiving, mailroom or facilities role outside media scope"

    if company in {"qvc", "qurate retail group", "qvc group"}:
        # QVC/Qurate operates large fulfillment, distribution and retail
        # businesses alongside its television/eCommerce operation. Exclude
        # physical logistics/store roles while retaining software, IT, digital
        # commerce, marketing, finance and genuine media/broadcast positions.
        qvc_hard_nonmedia_title = re.search(
            r"\b(equipment operator|warehouse|warehouse hiring event|"
            r"fulfillment|distribution center|distribution centre|"
            r"picker|packer|pick[ /-]?pack|material handler|forklift|"
            r"inventory (?:control|specialist|associate|coordinator)|"
            r"receiving|receiver|shipping|yard driver|yard jockey|"
            r"retail associate|store associate|store manager|retail store|"
            r"wave planning|facilities|building maintenance|maintenance tech(?:nician)?|"
            r"maintenance mechanic)\b",
            title,
        )
        qvc_physical_ops_context = re.search(
            r"\b(fulfillment center|fulfillment centre|distribution center|"
            r"distribution centre|warehouse|pick(?:ing)? and pack(?:ing)?|"
            r"pick/pack|shipping and receiving|shipping & receiving|"
            r"inventory control|material handling|forklift|conveyor|"
            r"yard operations|retail store|store operations)\b",
            text,
        )
        qvc_ops_title = re.search(
            r"\b(maintenance|technician|equipment operator|inventory|"
            r"operations associate|operations coordinator|receiver|"
            r"shipping|yard driver|material handler)\b",
            title,
        )
        # Physical-product merchandising/sourcing is outside MJR's scope, but
        # don't catch digital/eCommerce/marketing roles merely because their
        # descriptions mention merchandise.
        qvc_product_merch_title = re.search(
            r"\b(merchandis(?:e|er|ing)|buyer|assistant buyer|"
            r"product sourcing|sourcing (?:specialist|manager|coordinator)|"
            r"category buyer)\b",
            title,
        )
        qvc_media_or_digital_signal = re.search(
            r"\b(broadcast|television|tv|studio|video|audio|production|"
            r"digital|ecommerce|e-commerce|software|engineer|engineering|"
            r"information technology|\bit\b|automation|artificial intelligence|"
            r"machine learning|marketing|advertising|finance|financial|"
            r"accounting|legal|human resources|\bhr\b)\b",
            title,
        )
        if qvc_hard_nonmedia_title:
            return "QVC warehouse, fulfillment, distribution, retail or facilities role outside media scope"
        if qvc_physical_ops_context and qvc_ops_title and not qvc_media_or_digital_signal:
            return "QVC fulfillment/distribution operations role outside media scope"
        if qvc_product_merch_title and not qvc_media_or_digital_signal:
            return "QVC physical-product merchandising or sourcing role outside media scope"

    if company == "paramount":
        paramount_nonmedia_title = re.search(
            r"\b(toy|toys|consumer products?|product design(?:er)?|"
            r"industrial design(?:er)?|packaging design(?:er)?|merchandise design(?:er)?)\b",
            title,
        )
        media_product_signal = re.search(
            r"\b(streaming|paramount\+|pluto|cbs|broadcast|television|tv|"
            r"news|digital media|video|audio|advertising|ad tech|production)\b",
            text,
        )
        if paramount_nonmedia_title and not media_product_signal:
            return "Paramount toys or unrelated consumer-product role outside media scope"

    if company in {"dow jones", "wall street journal / dow jones", "wall street journal", "wsj / dow jones"}:
        dow_jones_nonmedia_title = re.search(
            r"\b(mailroom|mail room|mailroom manager|shipping and receiving|"
            r"shipping & receiving|receiving clerk|facilities|custodial|custodian|"
            r"building maintenance)\b",
            title,
        )
        if dow_jones_nonmedia_title:
            return "Dow Jones mailroom, receiving or facilities role outside media scope"


    # QVC / HSN final scope guard.
    # These employers mix television/eCommerce/media jobs with a large retail,
    # fulfillment, warehouse and physical-product merchandising workforce.
    if company in {"qvc", "hsn", "qurate retail group"}:
        # Explicit fulfillment titles must never be rescued by media-category protection.
        if re.search(
            r"\b(equipment operators?|forklift operators?|warehouse equipment operators?)\b",
            title,
        ):
            return "QVC/HSN equipment/fulfillment role outside media scope"

        qvc_nonmedia_title = re.search(
            r"\b("
            r"equipment operator|forklift|picker|packer|pick pack|warehouse|"
            r"distribution center|fulfillment center|fulfillment associate|"
            r"inventory (?:associate|specialist|control)|shipping|receiving|"
            r"yard driver|material handler|retail team member|retail associate|"
            r"sales associate|seasonal sales|design associate|store associate|"
            r"store manager|assistant store manager|hiring event|"
            r"customer service specialist|customer service representative|"
            r"contact center|call center|order specialist|aging order specialist|"
            r"global sourcing|sourcing specialist|buyer|assistant buyer|"
            r"merchant|merchandiser|merchandising|"
            r"maintenance tech|maintenance technician"
            r")\b",
            title,
        )
        qvc_nonmedia_context = re.search(
            r"\b("
            r"warehouse|distribution center|fulfillment center|pick(?:ing)? and pack|"
            r"shipping and receiving|inventory control|retail store|store location|"
            r"customer service center|contact center|call center|"
            r"physical product|product sourcing|vendor sourcing|"
            r"furniture|kitchen and culinary|apparel merchandise|"
            r"fulfillment operations|distribution operations"
            r")\b",
            description,
        )
        qvc_media_title = re.search(
            r"\b("
            r"broadcast|television|tv |studio|production|producer|video|audio|"
            r"on-air|host|content|social media|digital|ecommerce|e-commerce|"
            r"live commerce|tiktok|marketing|advertising|audience|"
            r"software|engineer|engineering|developer|data|analytics|ai|"
            r"automation|information technology|it operations|cyber|security|"
            r"finance|financial|accounting|legal|human resources|hr "
            r")\b",
            title,
        )
        if qvc_nonmedia_title and not qvc_media_title:
            return "QVC/HSN retail, fulfillment, customer-service or physical-product role outside media scope"
        if qvc_nonmedia_context and qvc_nonmedia_title and not qvc_media_title:
            return "QVC/HSN non-media operations role outside media scope"


    # Paramount final scope guard.
    # Paramount's careers feed includes a substantial consumer-products/licensing
    # business. Keep media, streaming, advertising, technology and normal corporate
    # functions, but exclude physical-product/toy/licensing-design/retail roles.
    if company in {"paramount", "paramount global", "paramount pictures", "paramount skydance"}:
        paramount_nonmedia_title = re.search(
            r"\b("
            r"consumer products?|global toys?|toy designer|toys?|hardlines|softlines|"
            r"licensing design|licensing designer|product licensing|"
            r"retail marketing|retail sr manager|retail senior manager|"
            r"commercial growth and retail|"
            r"cpg and promotions|consumer packaged goods|"
            r"publishing designer|designer, publishing|"
            r"pd designer|product development designer|"
            r"toy & illustration|toy and illustration|"
            r"creative studio - consumer products"
            r")\b",
            title,
        )

        # These are strong media/technology/corporate signals. They protect legitimate
        # Paramount work where words such as "product" or "design" are used for
        # streaming/digital/software rather than physical merchandise.
        paramount_media_title = re.search(
            r"\b("
            r"broadcast|television|film|streaming|paramount\+|pluto|cbs|showtime|"
            r"news|sports|studio|production|producer|editorial|video|audio|content|"
            r"digital|social|audience|advertising|ad sales|sales|marketing|publicity|"
            r"communications|software|engineer|engineering|developer|technology|"
            r"data|analytics|ai|automation|cyber|security|product manager|"
            r"finance|financial|accounting|legal|human resources|hr|business affairs"
            r")\b",
            title,
        )

        # Some physical-product roles include generic marketing/design language, so
        # explicit consumer-products/toy/retail signals take precedence.
        paramount_hard_reject = re.search(
            r"\b("
            r"consumer products?|global toys?|toy designer|hardlines|softlines|"
            r"licensing design|toy & illustration|toy and illustration|"
            r"creative studio - consumer products|commercial growth and retail|"
            r"cpg and promotions|retail marketing"
            r")\b",
            title,
        )

        if paramount_hard_reject:
            return "Paramount consumer-products, toys, licensing-design or retail role outside media scope"
        if paramount_nonmedia_title and not paramount_media_title:
            return "Paramount physical-product/licensing role outside media scope"


    # FOX final scope cleanup.
    # FOX Careers occasionally includes facilities/construction and hospitality
    # positions that are outside MJR's media-job scope.
    # Match every FOX-derived brand emitted by the FOX collector, including
    # FOX Sports / Big Ten Network and other FOX sub-brands.
    if (
        company == "fox"
        or company.startswith("fox ")
        or company.startswith("fox/")
        or company.startswith("fox -")
    ):
        if re.search(
            r"\b(plant operations|facilities|facility operations|construction)\b",
            title,
        ):
            return "FOX facilities/construction role outside media scope"
        if re.search(
            r"\b(members suite specialist|suite host|hospitality host)\b",
            title,
        ):
            return "FOX hospitality role outside media scope"


    # Sinclair final scope cleanup.
    # Building/facilities maintenance is outside MJR scope here; this must override
    # the normal Engineering treatment for technical broadcast maintenance.
    if company == "sinclair" or company.startswith("sinclair "):
        if re.search(
            r"\b(facilities maintenance|facility maintenance|building maintenance)\b",
            title,
        ):
            return "Sinclair facilities/building maintenance role outside media scope"

    if company == "meruelo media":
        construction_title = re.search(
            r"\b(construction (?:project )?manager|construction manager|"
            r"construction superintendent|superintendent|estimator|"
            r"project engineer|field engineer|site manager|jobsite manager|"
            r"general contractor|foreman|carpenter|concrete|drywall|"
            r"electrician|plumber|hvac|heavy equipment|construction laborer|"
            r"safety manager|preconstruction|land development)\b",
            title,
        )
        construction_context = re.search(
            r"\b(construction company|construction project|general contractor|"
            r"commercial construction|residential construction|jobsite|job site|"
            r"preconstruction|real estate development|land development|"
            r"concrete|drywall|building contractor|meruelo builders|"
            r"meruelo enterprises)\b",
            description,
        )
        generic_project_title = re.search(
            r"\b(project manager|project coordinator|project engineer|"
            r"operations manager|estimator|superintendent|field engineer|"
            r"safety manager|accounting manager)\b",
            title,
        )
        if construction_title or (construction_context and generic_project_title):
            return "Meruelo construction-business role outside media scope"

    return ""


def apply_company_scope_filters(jobs):
    kept = []
    rejected = []
    for job in jobs:
        reason = company_scope_rejection_reason(job)
        if reason:
            rejected.append((job, reason))
        else:
            kept.append(job)
    return kept, rejected


def disney_public(src):
    """Disney/ABC/ESPN public search collector.

    Disney's public search results expose titles, posting dates and canonical
    detail links server-side. Use the employer-specific search URL already
    configured and follow its job links/pagination.
    """
    company = clean(src.get("Company", "")).lower()
    starts = [src["URL"]]
    if company == "disney / abc":
        starts += [
            "https://www.disneycareers.com/en/search-jobs/abc/391/1/1",
            "https://jobs.disneycareers.com/search-jobs?k=ABC",
        ]
    elif company == "espn":
        starts += [
            "https://jobs.disneycareers.com/espn",
            "https://jobs.disneycareers.com/search-jobs?ascf=%5B%7B%22key%22%3A%22custom_fields.IndustryCustomField%22%2C%22value%22%3A%22ESPN%22%7D%5D&orgIds=391-28648",
        ]

    return _crawl_rendered_job_board(
        src,
        starts,
        allow_hosts={"disneycareers.com", "jobs.disneycareers.com", "www.disneycareers.com"},
        max_pages=80,
        max_jobs=3000,
    )


def wbd_phenom(src):
    """Warner Bros. Discovery / CNN Phenom People collector.

    Try Phenom's public widgets search endpoint first, then fall back to the
    server-rendered CNN search pages. All failures remain source-local.
    """
    host = "https://careers.wbd.com"
    out = []
    seen = set()

    # Public Phenom Career Connect widgets endpoint. Different tenants/releases
    # accept slightly different bodies, so try a small set of safe variants.
    bodies = [
        {
            "lang": "en_us",
            "deviceType": "desktop",
            "country": "us",
            "pageName": "search-results",
            "ddoKey": "refineSearch",
            "sortBy": "Most relevant",
            "subsearch": "",
            "from": 0,
            "jobs": True,
            "all_fields": ["category", "location", "brand"],
            "size": 100,
        },
        {
            "lang": "en_us",
            "deviceType": "desktop",
            "country": "us",
            "pageName": "search-results",
            "ddoKey": "search",
            "from": 0,
            "size": 100,
        },
    ]

    for body in bodies:
        try:
            r = req(
                "POST",
                host + "/widgets",
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            payload = r.json()
        except Exception:
            continue

        # Recover any job URLs from the returned JSON regardless of wrapper.
        raw = json.dumps(payload, ensure_ascii=False)
        urls = set()
        for m in re.finditer(
            r'https?://careers\.wbd\.com/[^"\\\s]+|/global/en/job/[^"\\\s]+|/job/[^"\\\s]+',
            raw,
            re.I,
        ):
            u = html.unescape(m.group(0)).replace("\\/", "/")
            urls.add(urljoin(host, u))

        for url in urls:
            j = _recent_detail_job(src, url)
            if j and j.id not in seen:
                seen.add(j.id)
                out.append(j)

        if out:
            return out

    # Safe fallback: Phenom also publishes searchable HTML pages.
    return _crawl_rendered_job_board(
        src,
        [
            "https://careers.wbd.com/cnnjobs",
            "https://careers.wbd.com/global/en/search-results",
            src["URL"],
        ],
        allow_hosts={"careers.wbd.com"},
        max_pages=60,
        max_jobs=2000,
    )


def _ukg_embedded_job(src, url, payload):
    """Validate employer-provided UKG data without executing JavaScript."""
    if not isinstance(payload, dict) or payload.get("OpportunityIsClosed") is True:
        return None
    parts = _ukg_parts(url)
    oid = (parse_qs(urlparse(url).query).get("opportunityId") or [""])[0]
    if not parts or clean(payload.get("Id")).lower() != oid.lower():
        return None
    membership = next((
        item for item in payload.get("JobBoardMemberships", [])
        if isinstance(item, dict)
        and clean(item.get("JobBoardId")).lower() == parts[2].lower()
        and item.get("PublishedExternal") is True
    ), None)
    if not membership:
        return None
    posted = pdate(membership.get("ExternalPostedDate") or payload.get("PostedDate"))
    title = clean(payload.get("Title"))
    desc = format_description(payload.get("Description"))
    employment = (
        "Full Time" if payload.get("FullTime") is True
        else "Part Time" if payload.get("FullTime") is False else ""
    )
    role_type = jobtype(title, employment)
    if (
        not posted or posted < CUTOFF or not title
        or not job_is_fresh_date(posted, role_type)
        or len(strip_html(desc)) < 200
    ):
        return None
    address, location_name = None, ""
    for location in payload.get("Locations") or []:
        candidate = location.get("Address") or {}
        country = clean((candidate.get("Country") or {}).get("Code")).upper()
        if country in {"US", "USA", "CA", "CAN"}:
            address = candidate
            location_name = clean(location.get("LocalizedName"))
            break
    if address is None:
        return None
    country = clean((address.get("Country") or {}).get("Code")).upper()
    city = clean(address.get("City"))
    if not city and location_name.lower() in {"nationwide", "remote", "multiple locations"}:
        city = location_name
    state = clean((address.get("State") or {}).get("Code"))
    location_type = payload.get("JobLocationType")
    if not isinstance(location_type, str):
        location_type = ""
    return Job(
        clean(payload.get("RequisitionNumber")) or oid, title, src["Company"],
        desc, posted, role_type,
        "Internships" if role_type == "Internship"
        else category(title, desc, src.get("Industry", ""), src["Company"]),
        url.split("#", 1)[0], src["URL"], src["URL"], "",
        normalize_work_arrangement(desc, location_type),
        city, state, "CA" if country in {"CA", "CAN"} else "US",
    )


def job_is_fresh_date(posted, role_type):
    return 0 <= (TODAY - posted).days < retention_days(role_type)


def gray_direct(src):
    """Use Gray's public widget API, then verify each fresh UKG detail."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    parts = _ukg_parts(src["URL"])
    if not parts:
        raise RuntimeError("Gray UKG board not inferable")
    base, tenant, board = parts
    board_root = f"{base}/{tenant}/JobBoard/{board}"
    payload = req("GET", "https://graymedia.com/careers/careers_api_v2.php", params={
        "limit": 0, "offset": 0, "sort_order": "desc", "searchWhat": "",
        "searchWhere": "", "searchJobLocation": "", "searchJobCategory": "",
        "searchSchedule": "", "searchJobLocationType": "",
    }).json()
    if not isinstance(payload, list):
        raise RuntimeError("Gray careers API returned an unexpected inventory")

    candidates, arrangements = {}, {}
    for row in payload:
        if not isinstance(row, dict) or row.get("status") != "Published":
            continue
        oid = clean(row.get("external_id"))
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", oid, re.I):
            continue
        posted = pdate(row.get("external_posted_date"))
        role_type = jobtype(clean(row.get("title")))
        if not posted or not job_is_fresh_date(posted, role_type):
            continue
        published_here = any(
            item.get("is_published_external") is True
            and _ukg_parts(clean(item.get("recruiting_apply_url"))) == parts
            for item in row.get("job_boards") or [] if isinstance(item, dict)
        )
        if published_here:
            candidates[oid.lower()] = f"{board_root}/OpportunityDetail?opportunityId={oid}"
            explicit_type = row.get("job_location_type")
            if explicit_type in {"remote", "hybrid", "on-site"}:
                arrangements[oid.lower()] = {
                    "remote": "Remote", "hybrid": "Hybrid", "on-site": "On-Site",
                }[explicit_type]
    if len(candidates) > 400:
        raise RuntimeError(f"Gray fresh inventory exceeds bounded detail budget: {len(candidates)}")

    def fetch_one(url):
        response = _req_raw("GET", url, timeout=12, tries=2)
        return _ukg_detail(src, url, response.text)

    jobs, failed_urls = [], []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_one, url): url for url in candidates.values()}
        for future in as_completed(futures):
            try:
                job = future.result()
                if job:
                    jobs.append(job)
            except Exception:
                failed_urls.append(futures[future])
    # Retry only failed details once with a longer timeout and smaller pool.
    failures = 0
    if failed_urls:
        def retry_one(url):
            response = _req_raw("GET", url, timeout=20, tries=2)
            return _ukg_detail(src, url, response.text)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(retry_one, url) for url in failed_urls]
            for future in as_completed(futures):
                try:
                    job = future.result()
                    if job:
                        jobs.append(job)
                except Exception:
                    failures += 1
    print(f"Gray API: inventory={len(payload)} fresh={len(candidates)} parsed={len(jobs)} request_failures={failures}")
    if failures:
        raise RuntimeError(f"Gray detail collection incomplete: {failures} unresolved requests")
    if candidates and not jobs:
        raise RuntimeError("Gray fresh postings could not be validated from UKG details")
    for job in jobs:
        oid = (parse_qs(urlparse(job.url).query).get("opportunityId") or [""])[0].lower()
        if oid in arrangements:
            job.work_arrangement = arrangements[oid]
    return sorted(jobs, key=lambda job: job.id)




V17_TARGETS = {
    "siriusxm",
    "townsquare media",
    "nbcuniversal",
    "tegna",
    "cumulus media",
}


def _v17_samehost_details(src, starts, hosts, max_pages=120, max_jobs=4000):
    """Aggressive but bounded public-board discovery for v17 targets."""
    hosts = {h.lower().replace("www.", "") for h in hosts}
    queue = list(dict.fromkeys(starts))
    seen_pages = set()
    details = set()

    while queue and len(seen_pages) < max_pages and len(details) < max_jobs:
        page = queue.pop(0)
        key = page.rstrip("/")
        if key in seen_pages:
            continue
        seen_pages.add(key)

        try:
            r = req("GET", page)
        except Exception:
            continue

        final_url = str(getattr(r, "url", "") or page)
        soup = BeautifulSoup(r.text, "html.parser")
        raw = html.unescape(r.text or "").replace("\\/", "/")

        def consider(h):
            h = urljoin(final_url, h)
            hp = urlparse(h)
            host = hp.netloc.lower().replace("www.", "")
            if host not in hosts:
                return
            low = hp.path.lower()
            q = hp.query.lower()

            detailish = (
                re.search(r"/jobs?/\d+(?:/|$)", low)
                or re.search(r"/job/[^/?#]+", low)
                or "/jobdetails/" in low
                or "/job-detail/" in low
                or "/jobdescription/" in low
                or re.search(r"/careers/jobs/[^/?#]+", low)
                or ("jobid=" in q and not re.search(r"(search|results)", low))
            )
            if detailish and h.rstrip("/") != final_url.rstrip("/"):
                details.add(h.split("#", 1)[0])

        for a in soup.find_all("a", href=True):
            consider(a["href"])
            h = urljoin(final_url, a["href"])
            hp = urlparse(h)
            host = hp.netloc.lower().replace("www.", "")
            if host not in hosts:
                continue
            label = clean(a.get_text(" ")).lower()
            if (
                re.search(r"\b(next|more jobs|view more|older|load more)\b", label)
                or re.search(r"[?&](page|p|start|offset|from|pageindex)=\d+", h, re.I)
                or re.search(r"/page/\d+", hp.path.lower())
            ):
                if h.rstrip("/") not in seen_pages:
                    queue.append(h)

        for m in re.finditer(r'https?://[^"\'<>\s]+', raw):
            consider(m.group(0).rstrip(".,);"))

        # Relative URLs embedded in JSON/state.
        for m in re.finditer(
            r'["\']((?:/[^"\']*)?(?:/jobs?/\d+|/job/[^"\'?#]+|/careers/jobs/[^"\'?#]+)[^"\']*)["\']',
            raw,
            re.I,
        ):
            consider(m.group(1))

    out, seen_ids = [], set()
    for url in sorted(details):
        try:
            rr = req("GET", url)
            final_url = str(getattr(rr, "url", "") or url)
            j = _job_from_detail(src, final_url, rr.text)
            if not j:
                j = _direct_board_job(src, final_url, rr.text)
            if j and j.id not in seen_ids:
                seen_ids.add(j.id)
                out.append(j)
        except Exception:
            continue
    return out



def _siriusxm_location_parts(raw_city="", raw_state="", raw_country=""):
    """Normalize SiriusXM/Phenom location strings into JBoard city/state/country.

    SiriusXM can return values such as:
      Lewisville, Texas, United States
      Chicago, Illinois, United States / New York, New York, United States
      Bucharest, UNAVAILABLE, Romania
      UNAVAILABLE, New York, United States / UNAVAILABLE, Georgia, United States

    JBoard has one city/state/country tuple, so for multi-location jobs we use
    the first listed location and discard literal UNAVAILABLE placeholders.
    """
    state_names = {
        "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR",
        "California":"CA","Colorado":"CO","Connecticut":"CT","Delaware":"DE",
        "Florida":"FL","Georgia":"GA","Hawaii":"HI","Idaho":"ID","Illinois":"IL",
        "Indiana":"IN","Iowa":"IA","Kansas":"KS","Kentucky":"KY","Louisiana":"LA",
        "Maine":"ME","Maryland":"MD","Massachusetts":"MA","Michigan":"MI",
        "Minnesota":"MN","Mississippi":"MS","Missouri":"MO","Montana":"MT",
        "Nebraska":"NE","Nevada":"NV","New Hampshire":"NH","New Jersey":"NJ",
        "New Mexico":"NM","New York":"NY","North Carolina":"NC","North Dakota":"ND",
        "Ohio":"OH","Oklahoma":"OK","Oregon":"OR","Pennsylvania":"PA",
        "Rhode Island":"RI","South Carolina":"SC","South Dakota":"SD",
        "Tennessee":"TN","Texas":"TX","Utah":"UT","Vermont":"VT","Virginia":"VA",
        "Washington":"WA","West Virginia":"WV","Wisconsin":"WI","Wyoming":"WY",
        "District of Columbia":"DC","Washington, DC":"DC",
    }
    country_map = {
        "United States":"US","USA":"US","US":"US",
        "United Kingdom":"GB","UK":"GB","Great Britain":"GB",
        "Romania":"RO","Ireland":"IE","Canada":"CA",
        "Germany":"DE","France":"FR","Netherlands":"NL",
        "Belgium":"BE","Spain":"ES","Italy":"IT","Australia":"AU",
    }

    raw_city = clean(raw_city)
    raw_state = clean(raw_state)
    raw_country = clean(raw_country)

    # First listed location is authoritative for JBoard's single-location fields.
    first = raw_city.split(" / ", 1)[0].strip()

    # If generic parsing already returned city/state/country separately and city
    # is not a compound SiriusXM location, keep those values after normalization.
    if "," not in first and first and first.upper() != "UNAVAILABLE":
        city = first
        state = state_names.get(raw_state, raw_state if re.fullmatch(r"[A-Z]{2}", raw_state) else "")
        country = country_map.get(raw_country, raw_country if re.fullmatch(r"[A-Z]{2}", raw_country) else "US")
        return city, state, country

    parts = [clean(x) for x in first.split(",")]
    parts = [x for x in parts if x]

    city = ""
    state = ""
    country = ""

    if len(parts) >= 3:
        city_part = parts[0]
        state_part = parts[1]
        country_part = ", ".join(parts[2:])
        city = "" if city_part.upper() == "UNAVAILABLE" else city_part
        state = "" if state_part.upper() == "UNAVAILABLE" else state_names.get(state_part, state_part if re.fullmatch(r"[A-Z]{2}", state_part) else "")
        country = country_map.get(country_part, country_part if re.fullmatch(r"[A-Z]{2}", country_part) else "")
    elif len(parts) == 2:
        # Covers e.g. "London, United Kingdom" or "Bucharest, Romania".
        city = "" if parts[0].upper() == "UNAVAILABLE" else parts[0]
        if parts[1] in state_names:
            state = state_names[parts[1]]
            country = "US"
        else:
            country = country_map.get(parts[1], parts[1] if re.fullmatch(r"[A-Z]{2}", parts[1]) else "")
    elif len(parts) == 1:
        city = "" if parts[0].upper() == "UNAVAILABLE" else parts[0]

    # Fill from separately parsed fields when useful.
    if not state:
        state = state_names.get(raw_state, raw_state if re.fullmatch(r"[A-Z]{2}", raw_state) else "")
    if not country:
        country = country_map.get(raw_country, raw_country if re.fullmatch(r"[A-Z]{2}", raw_country) else "")
    if not country:
        country = "US" if state else ""

    return city, state, country


def siriusxm_v17(src):
    """SiriusXM direct Jibe/Phenom API collector.

    v67 replaces the v65-v66 browser discovery crawl with SiriusXM's own public
    /api/jobs endpoint. We try ordinary HTTP first. If SiriusXM requires a
    browser-established session, Playwright is used only once to bootstrap the
    careers page and fetch the API; we do not crawl category/location pages.

    The API is the source of job IDs, titles, descriptions, posting metadata,
    locations, employment type, and SiriusXM work-mode metadata. Canonical
    individual job-detail URLs remain the JBoard apply URLs.
    """
    host = "https://careers.siriusxm.com"
    api_base = host + "/api/jobs"
    out = []
    seen = set()
    diag = []

    def d(msg):
        diag.append(str(msg))

    def scalars(obj, prefix=""):
        """Yield flattened scalar key/value pairs from nested API data."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{prefix}.{k}" if prefix else str(k)
                yield from scalars(v, p)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from scalars(v, f"{prefix}[{i}]")
        elif obj is not None:
            yield prefix.lower(), clean(str(obj))

    def pick_by_keys(obj, key_patterns):
        for k, v in scalars(obj):
            if any(re.search(p, k, re.I) for p in key_patterns) and v:
                return v
        return ""

    def all_by_keys(obj, key_patterns):
        vals = []
        for k, v in scalars(obj):
            if any(re.search(p, k, re.I) for p in key_patterns) and v and v not in vals:
                vals.append(v)
        return vals

    def posted_date(data):
        patterns = [
            r"(?:^|\.)(?:date_posted|dateposted|posted_date|posteddate)$",
            r"(?:^|\.)(?:posting_date|postingdate)$",
            r"(?:^|\.)(?:publish_date|publisheddate|published_date)$",
            r"(?:^|\.)(?:created_date|createddate)$",
            r"(?:^|\.)(?:open_date|opendate)$",
        ]
        for v in all_by_keys(data, patterns):
            pd = pdate(v)
            if pd:
                return pd
        return None

    def explicit_work_mode(data, description=""):
        # Prefer SiriusXM/Jibe structured metadata, never generic benefits text.
        meta_vals = all_by_keys(
            data,
            [
                r"work.*(?:mode|model|arrangement|location|type)",
                r"(?:flex|remote|hybrid).*type",
                r"workplace",
                r"location_type",
                r"work_location_type",
                r"tags",
            ],
        )
        meta = " | ".join(meta_vals).lower()
        if re.search(r"\boffice[\s_-]*first\b", meta):
            return "On-Site"
        if re.search(r"\bhybrid\b", meta):
            return "Hybrid"
        if re.search(r"\bremote\b", meta):
            return "Remote"

        # Fall back only to explicit role-level wording using the conservative
        # global classifier introduced in v64.
        return normalize_work_arrangement(description, "")

    def employment_type(data, title, description):
        meta = " ".join(
            all_by_keys(
                data,
                [
                    r"employment.*type",
                    r"job.*type",
                    r"schedule.*type",
                    r"position.*type",
                    r"tags",
                ],
            )
        )
        low = meta.lower()
        if "intern" in clean(title).lower():
            return "Internship"
        if re.search(r"\bpart[\s_-]*time\b", low):
            return "Part Time"
        if re.search(r"\b(full[\s_-]*time|regular employee full)\b", low):
            return "Full Time"
        if re.search(r"\b(contract|contractor)\b", low):
            return "Contract"
        if re.search(r"\btemporary\b", low):
            return "Temporary"
        return jobtype(title, meta + " " + description[:2500])

    def location_parts(data):
        # Jibe exposes both normal geographic data and internal display labels
        # such as "CA - Los Angeles - Sycamore Ave". Normalize those labels to
        # the actual city while retaining state/country separately.
        candidates = all_by_keys(
            data,
            [
                r"(?:^|\.)(?:location|location_name|locationname|display_location|displaylocation)$",
                r"(?:^|\.)locations(?:\[\d+\])?(?:\.name)?$",
                r"(?:^|\.)address(?:\.name)?$",
            ],
        )

        city = pick_by_keys(data, [r"(?:^|\.)(?:city|locality)$"])
        state = pick_by_keys(data, [r"(?:^|\.)(?:state|region|province)$"])
        country = pick_by_keys(data, [r"(?:^|\.)(?:country|country_name|countryname)$"])

        compound = ""
        for val in candidates:
            if val:
                compound = val
                # Prefer a meaningful display location over generic fields.
                if "," in val or " / " in val or " - " in val:
                    break

        raw = clean(compound or city)

        # SiriusXM/Jibe common display labels:
        #   CA - Los Angeles - Sycamore Ave
        #   TX - Lewisville - Highland Dr
        #   NY - New York - 1221 Ave of Americas
        #   RO - Bucharest - AFI Park Floreasca
        #   UK - London - Swan House
        #   Remote - New York
        #
        # Keep only the actual city portion for JBoard.
        if " - " in raw:
            parts = [clean(x) for x in raw.split(" - ") if clean(x)]
            if parts:
                first = parts[0].upper()
                if first == "REMOTE":
                    # Keep geographic city while work_arrangement carries Remote.
                    raw = parts[1] if len(parts) > 1 else ""
                elif re.fullmatch(r"[A-Z]{2,3}", first):
                    # Prefix is state/country code; second token is city.
                    raw = parts[1] if len(parts) > 1 else ""
                elif len(parts) >= 2 and parts[0].lower() in {
                    "united states", "united kingdom", "romania", "ireland"
                }:
                    raw = parts[1]

        c, s, co = _siriusxm_location_parts(raw, state, country)

        # Preserve API-provided state/country when the cleaned city no longer
        # contains those components.
        if state:
            state_map = {
                "california":"CA","colorado":"CO","connecticut":"CT","florida":"FL",
                "georgia":"GA","illinois":"IL","indiana":"IN","maryland":"MD",
                "massachusetts":"MA","michigan":"MI","nevada":"NV","new jersey":"NJ",
                "new york":"NY","north carolina":"NC","ohio":"OH","oregon":"OR",
                "tennessee":"TN","texas":"TX","virginia":"VA","washington":"WA",
                "west virginia":"WV","washington, dc":"DC","district of columbia":"DC"
            }
            sv = clean(state)
            s = state_map.get(sv.lower(), sv if re.fullmatch(r"[A-Z]{2}", sv.upper()) else s)
            if re.fullmatch(r"[A-Z]{2}", sv.upper()):
                s = sv.upper()

        if country:
            cv = clean(country).lower()
            country_map = {
                "united states":"US","usa":"US","us":"US",
                "united kingdom":"GB","uk":"GB","gb":"GB",
                "romania":"RO","ro":"RO",
                "ireland":"IE","ie":"IE",
                "canada":"CA","ca":"CA",
            }
            co = country_map.get(cv, co)

        return c, s, co

    def category_fix(title, description, jt):
        if jt == "Internship":
            return "Internships"

        tl = clean(title).lower()
        if re.search(r"\b(noc|network operations center)\b", tl):
            return "Engineering"
        if re.search(r"\bmajor accounts?\b", tl):
            return "Sales & Marketing"
        if re.search(r"\bbusiness insights?\b", tl):
            return "Digital"

        return category(title, description, src["Industry"], src["Company"])

    def fetch_page_http(page_num):
        url = (
            f"{api_base}?page={page_num}&sortBy=relevance&descending=false"
            f"&internal=false&limit=100"
        )
        rr = req(
            "GET",
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": host + "/careers/jobs",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        return rr.json(), url

    def fetch_all_browser():
        """One-page browser bootstrap fallback; no category/location crawling."""
        if sync_playwright is None:
            raise RuntimeError("Playwright unavailable for SiriusXM API bootstrap")

        payloads = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/152.0.0.0 Safari/537.36"
                )
            )
            page.goto(host + "/careers/jobs", wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                page.wait_for_timeout(4000)

            api_page = 1
            prior_ids = set()
            while api_page <= 30:
                url = (
                    f"{api_base}?page={api_page}&sortBy=relevance&descending=false"
                    f"&internal=false&limit=100"
                )
                resp = page.request.get(
                    url,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Referer": host + "/careers/jobs",
                    },
                    timeout=30000,
                )
                if resp.status != 200:
                    raise RuntimeError(f"SiriusXM API browser HTTP {resp.status}")
                payload = resp.json()
                jobs = payload.get("jobs") or []
                ids = set()
                for item in jobs:
                    data = item.get("data", item) if isinstance(item, dict) else {}
                    jid = clean(str(data.get("slug") or data.get("req_id") or ""))
                    if jid:
                        ids.add(jid)
                payloads.append((payload, url))
                d(f"API_BROWSER page={api_page} jobs={len(jobs)}")
                if not jobs or (ids and ids.issubset(prior_ids)):
                    break
                prior_ids |= ids
                total = payload.get("totalCount")
                if isinstance(total, int) and len(prior_ids) >= total:
                    break
                api_page += 1

            browser.close()
        return payloads

    d("SIRIUSXM DIRECT API v68")
    d(f"source={src.get('URL','')}")

    payloads = []
    try:
        prior_ids = set()
        for page_num in range(1, 31):
            payload, url = fetch_page_http(page_num)
            jobs = payload.get("jobs") or []
            ids = set()
            for item in jobs:
                data = item.get("data", item) if isinstance(item, dict) else {}
                jid = clean(str(data.get("slug") or data.get("req_id") or ""))
                if jid:
                    ids.add(jid)
            payloads.append((payload, url))
            d(f"API_HTTP page={page_num} jobs={len(jobs)}")
            if not jobs or (ids and ids.issubset(prior_ids)):
                break
            prior_ids |= ids
            total = payload.get("totalCount")
            if isinstance(total, int) and len(prior_ids) >= total:
                break
    except Exception as e:
        d(f"API_HTTP_ERROR {type(e).__name__}:{e}")
        payloads = []
        try:
            payloads = fetch_all_browser()
        except Exception as be:
            d(f"API_BROWSER_ERROR {type(be).__name__}:{be}")

    raw_jobs = []
    for payload, api_url in payloads:
        for item in payload.get("jobs") or []:
            if isinstance(item, dict):
                raw_jobs.append(item.get("data", item))

    d(f"API_RECORDS={len(raw_jobs)}")

    for data in raw_jobs:
        try:
            jid = clean(str(data.get("slug") or data.get("req_id") or data.get("id") or ""))
            # Real SiriusXM requisition IDs are short numeric slugs. Reject the
            # unrelated long numeric values that polluted v66 browser discovery.
            if not re.fullmatch(r"\d{4,8}", jid):
                continue
            if jid in seen:
                continue

            title = clean(str(data.get("title") or pick_by_keys(data, [r"(?:^|\.)title$"])))
            desc_html = str(
                data.get("description")
                or pick_by_keys(data, [r"(?:^|\.)description$"])
                or ""
            )
            desc = format_description(desc_html)

            if not title or len(desc) < 150:
                continue

            pd = posted_date(data)
            if not pd:
                # Preserve the v66 policy for an API-listed currently open job
                # when SiriusXM does not publish a reliable posting date.
                pd = TODAY
            if pd < CUTOFF:
                continue

            jt = employment_type(data, title, desc)
            cat = category_fix(title, desc, jt)
            city, state, country = location_parts(data)
            wa = explicit_work_mode(data, desc)

            canonical = f"{host}/careers/jobs/{jid}"

            # Employer deadline, if SiriusXM exposes one, may shorten MJR expiry.
            employer_deadline = None
            for v in all_by_keys(
                data,
                [
                    r"(?:^|\.)(?:expiration_date|expirationdate|closing_date|closingdate)$",
                    r"(?:^|\.)(?:apply_by|applyby|deadline)$",
                ],
            ):
                employer_deadline = pdate(v)
                if employer_deadline:
                    break

            j = Job(
                jid,
                title,
                src["Company"],
                desc,
                pd,
                jt,
                cat,
                canonical,
                src["URL"],
                "https://www.siriusxm.com/",
                "",
                wa,
                city,
                state,
                country or "",
                employer_deadline,
            )

            seen.add(jid)
            out.append(j)
            if len(out) <= 60:
                d(
                    f"ACCEPT {jid} title={j.title} type={j.jobtype} "
                    f"cat={j.category} loc={j.city},{j.state},{j.country} "
                    f"wa={j.work_arrangement} date={j.date}"
                )
        except Exception as e:
            d(f"PARSE_ERROR {type(e).__name__}:{e}")

    d(f"FINAL={len(out)}")
    Path("mjr-siriusxm-diagnostic-v68.txt").write_text(
        "\n".join(diag) + "\n", encoding="utf-8"
    )
    return out



def townsquare_greenhouse(src):
    """Collect Townsquare jobs directly from Greenhouse's public API.

    Townsquare's branded careers page is dynamic, and the old same-host collector
    no longer sees its job cards. Townsquare has used both `townsquaremedia` and
    `townsquare` Greenhouse board identifiers, so try both and de-duplicate.
    """
    out = []
    seen = set()
    boards = ["townsquaremedia", "townsquare"]

    for board in boards:
        try:
            d = req(
                "GET",
                f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
            ).json()
        except Exception:
            continue

        for item in d.get("jobs", []):
            jid = str(item.get("id") or "")
            if not jid or jid in seen:
                continue

            posted = pdate(item.get("created_at")) or pdate(item.get("updated_at"))
            if not posted or posted < CUTOFF:
                continue

            title = clean(item.get("title"))
            desc = format_description(item.get("content"))
            loc = clean((item.get("location") or {}).get("name"))
            apply_url = clean(item.get("absolute_url"))
            company = clean(src.get("Company", "Townsquare Media"))

            # Division attribution must come from strong job-level signals, not a
            # passing mention of Ignite/Interactive in generic Townsquare copy.
            title_l = title.lower()
            desc_text = strip_html(desc)
            lead_text = desc_text[:1800].lower()

            ignite_signal = (
                "townsquare ignite" in title_l
                or re.search(
                    r"\b(?:join|about|team|division|department|role at|position with)\s+"
                    r"(?:the\s+)?townsquare ignite\b",
                    lead_text,
                )
            )
            interactive_signal = (
                "townsquare interactive" in title_l
                or re.search(
                    r"\b(?:join|about|team|division|department|role at|position with)\s+"
                    r"(?:the\s+)?townsquare interactive\b",
                    lead_text,
                )
            )

            if ignite_signal:
                company = "Townsquare Ignite"
            elif interactive_signal:
                company = "Townsquare Interactive"
            else:
                company = "Townsquare Media"

            seen.add(jid)
            out.append(
                Job(
                    f"townsquare-{jid}",
                    title,
                    company,
                    desc,
                    posted,
                    jobtype(title, desc),
                    category(title, desc, "Radio", company),
                    apply_url,
                    src.get("URL", "https://careers.townsquaremedia.com/job-openings/"),
                    "https://careers.townsquaremedia.com/",
                    "",
                    normalize_work_arrangement(desc, loc, title),
                    loc,
                    "",
                    infer_country(loc, company, desc),
                )
            )

    print(f"Townsquare Greenhouse: {len(out)} current jobs")
    return out


def townsquare_v17(src):
    return _v17_samehost_details(
        src,
        [
            "https://careers.townsquaremedia.com/job-openings",
            src["URL"],
        ],
        {"careers.townsquaremedia.com", "townsquaremedia.com"},
        max_pages=120,
        max_jobs=3000,
    )


def nbcuniversal_v17(src):
    """Collect NBCUniversal's public SmartRecruiters postings efficiently.

    SmartRecruiters list results already contain releasedDate and location.
    Filter the public inventory to MJR's freshness window at the list endpoint,
    reject non-US/Canada summaries before detail retrieval, and fetch details
    only for jobs that can actually enter the feed. This keeps NBCU well below
    the crawler's per-domain request safety cap.
    """
    company_id = "NBCUniversal3"
    endpoint = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
    out = []
    seen_ids = set()
    offset = 0
    limit = 100
    rows_checked = 0
    details_fetched = 0
    stale_or_invalid = 0
    foreign = 0
    released_after = CUTOFF.isoformat() + "T00:00:00.000Z"

    while offset < 5000:
        payload = req(
            "GET",
            endpoint,
            params={
                "limit": str(limit),
                "offset": str(offset),
                "destination": "PUBLIC",
                "releasedAfter": released_after,
            },
        ).json()
        rows = payload.get("content", []) if isinstance(payload, dict) else []
        if not rows:
            break

        for summary in rows:
            if not isinstance(summary, dict):
                continue
            rows_checked += 1

            jid = clean(str(summary.get("id") or ""))
            if not jid or jid in seen_ids:
                continue
            seen_ids.add(jid)

            # List objects contain releasedDate and location. Reject anything
            # outside MJR's scope before spending a detail request.
            pd = pdate(summary.get("releasedDate"))
            if not pd or pd < CUTOFF:
                stale_or_invalid += 1
                continue

            sloc = summary.get("location") or {}
            if not isinstance(sloc, dict):
                sloc = {}
            country_raw = clean(str(
                sloc.get("countryCode")
                or sloc.get("country")
                or ""
            )).upper()
            if country_raw in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
                summary_country = "US"
            elif country_raw in {"CA", "CANADA"}:
                summary_country = "CA"
            else:
                # SmartRecruiters supplies explicit country data. Do not let
                # NBCUniversal's US employer identity turn an international
                # city into a guessed US job.
                foreign += 1
                continue

            detail = summary
            ref = clean(str(summary.get("ref") or ""))
            if ref:
                try:
                    detail = req("GET", ref).json()
                    details_fetched += 1
                except Exception:
                    detail = summary
            if not isinstance(detail, dict):
                continue

            title = clean(str(detail.get("name") or summary.get("name") or ""))
            if not title:
                continue

            location_obj = detail.get("location") or sloc
            if not isinstance(location_obj, dict):
                location_obj = {}
            city = clean(str(location_obj.get("city") or ""))
            state = clean(str(
                location_obj.get("regionCode")
                or location_obj.get("region")
                or ""
            ))
            full_location = clean(str(
                location_obj.get("fullLocation")
                or ", ".join(x for x in [
                    city, state, location_obj.get("country")
                ] if x)
            ))
            detail_country_raw = clean(str(
                location_obj.get("countryCode")
                or location_obj.get("country")
                or ""
            )).upper()
            if detail_country_raw in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
                country = "US"
            elif detail_country_raw in {"CA", "CANADA"}:
                country = "CA"
            else:
                country = summary_country

            sections = ((detail.get("jobAd") or {}).get("sections") or {})
            desc_parts = []
            if isinstance(sections, dict):
                for section in sections.values():
                    if isinstance(section, dict) and section.get("text"):
                        desc_parts.append(str(section.get("text")))
            desc = format_description("".join(desc_parts))
            if len(strip_html(desc)) < 200:
                continue

            apply_url = clean(str(
                detail.get("applyUrl")
                or detail.get("postingUrl")
                or summary.get("applyUrl")
                or summary.get("postingUrl")
                or f"https://jobs.smartrecruiters.com/{company_id}/{jid}"
            ))
            employment_obj = detail.get("typeOfEmployment") or summary.get("typeOfEmployment") or {}
            employment = clean(str(
                employment_obj.get("label", "")
                if isinstance(employment_obj, dict)
                else employment_obj or ""
            ))
            remote = bool(location_obj.get("remote"))
            hybrid = bool(location_obj.get("hybrid"))
            work = (
                "Remote" if remote
                else "Hybrid" if hybrid
                else normalize_work_arrangement(desc, full_location, title)
            )

            out.append(Job(
                jid,
                title,
                src["Company"],
                desc,
                pd,
                jobtype(title, employment),
                category(title, desc, src["Industry"], src["Company"]),
                apply_url,
                src["URL"],
                "https://jobs.smartrecruiters.com/NBCUniversal3",
                "",
                work,
                city or full_location,
                state,
                country,
            ))

        total = payload.get("totalFound") if isinstance(payload, dict) else None
        offset += len(rows)
        if len(rows) < limit or (isinstance(total, int) and offset >= total):
            break

    print(
        f"NBCUniversal SmartRecruiters: rows_checked={rows_checked} "
        f"unique={len(seen_ids)} details={details_fetched} "
        f"foreign={foreign} stale_or_invalid={stale_or_invalid} "
        f"fresh_us_ca={len(out)}"
    )
    return out


def tegna_v17(src):
    return _v17_samehost_details(
        src,
        [
            "https://www.tegna.com/explore-careers",
            src["URL"],
        ],
        {"tegna.com"},
        max_pages=120,
        max_jobs=3000,
    )


def cumulus_v17(src):
    """Fast Cumulus collector.

    Cumulus's Jibe listing/API can be blocked to automated clients. Never probe
    requisition IDs sequentially: that made targeted/full crawls unacceptably
    slow. Try the bounded public API once, then the existing same-host discovery
    path. If neither enumerates jobs, return zero and leave the source flagged
    for review rather than delaying the entire crawl.
    """
    host = "https://jobs.cumulusmedia.com"
    api = host + "/api/jobs"
    detail_urls = set()
    prior_ids = set()

    try:
        for page_num in range(1, 6):
            rr = req(
                "GET",
                (
                    f"{api}?page={page_num}&sortBy=relevance&descending=false"
                    f"&internal=false&limit=100"
                ),
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": host + "/jobs",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            payload = rr.json()
            rows = payload.get("jobs") or [] if isinstance(payload, dict) else []
            ids = set()
            for item in rows:
                if not isinstance(item, dict):
                    continue
                data = item.get("data", item)
                if not isinstance(data, dict):
                    continue
                jid = clean(str(
                    data.get("slug") or data.get("req_id")
                    or data.get("id") or data.get("jobId") or ""
                ))
                if re.fullmatch(r"\\d{3,10}", jid):
                    ids.add(jid)
                    detail_urls.add(f"{host}/jobs/{jid}?lang=en-us")
            print(f"Cumulus Jibe API page={page_num} rows={len(rows)} ids={len(ids)}")
            if not rows or (ids and ids.issubset(prior_ids)):
                break
            prior_ids |= ids
            total = payload.get("totalCount") if isinstance(payload, dict) else None
            if isinstance(total, int) and len(prior_ids) >= total:
                break
    except Exception as e:
        print(f"Cumulus Jibe API unavailable: {type(e).__name__}: {clean(str(e))[:160]}")

    out, seen_ids = [], set()
    for url in sorted(detail_urls):
        try:
            rr = req("GET", url)
            final = str(getattr(rr, "url", "") or url)
            j = _job_from_detail(src, final, rr.text)
            if not j:
                j = _direct_board_job(src, final, rr.text)
            if j and j.id not in seen_ids:
                seen_ids.add(j.id)
                out.append(j)
        except Exception:
            continue

    if out:
        print(f"Cumulus Jibe direct: details={len(detail_urls)} parsed={len(out)}")
        return out

    return _v17_samehost_details(
        src,
        [host + "/jobs", host + "/", src["URL"]],
        {"jobs.cumulusmedia.com"},
        max_pages=20,
        max_jobs=500,
    )


V18_TARGETS = {
    "audacy",
    "dick broadcasting company",
    "hope media group",
    "nrg media",
    "weigel",
}


def _v18_icims_date(raw):
    s = html.unescape(raw or "").replace("\\/", "/")
    for pat in (
        r"(?:Date Posted|Posted Date|Posted)\s*:?\s*([A-Za-z]+\s+\d{1,2},\s+20\d{2})",
        r"(?:Date Posted|Posted Date|Posted)\s*:?\s*(\d{1,2}/\d{1,2}/20\d{2})",
        r'["\']datePosted["\']\s*:\s*["\']([^"\']+)["\']',
    ):
        m = re.search(pat, s, re.I)
        if m:
            d = pdate(strip_html(m.group(1)))
            if d:
                return d
    return None


def icims_v18(src):
    """Targeted iCIMS enumerator for Audacy.

    iCIMS search pages are server-rendered enough to enumerate requisition
    detail URLs. Detail pages are accepted only when an explicit recent
    posting date can be verified.
    """
    start = src["URL"]
    queue = [start]
    seen_pages = set()
    details = set()

    while queue and len(seen_pages) < 120 and len(details) < 4000:
        page = queue.pop(0)
        if page.rstrip("/") in seen_pages:
            continue
        seen_pages.add(page.rstrip("/"))
        try:
            r = req("GET", page)
        except Exception:
            continue

        final = str(getattr(r, "url", "") or page)
        soup = BeautifulSoup(r.text, "html.parser")
        host = urlparse(final).netloc.lower()

        for a in soup.find_all("a", href=True):
            h = urljoin(final, a["href"])
            hp = urlparse(h)
            if hp.netloc.lower() != host:
                continue
            low = hp.path.lower()
            if re.search(r"/jobs/\d+(?:/|$)", low):
                details.add(h.split("?", 1)[0])
                continue
            label = clean(a.get_text(" ")).lower()
            if (
                label in {"next", "next page", ">", "»"}
                or re.search(r"[?&](pr|page)=\d+", h, re.I)
            ):
                if h.rstrip("/") not in seen_pages:
                    queue.append(h)

        raw = html.unescape(r.text or "").replace("\\/", "/")
        for m in re.finditer(r'https?://[^"\'<>\s]+/jobs/\d+[^"\'<>\s]*', raw, re.I):
            h = m.group(0).rstrip(".,);")
            if urlparse(h).netloc.lower() == host:
                details.add(h.split("?", 1)[0])

    out, seen_ids = [], set()
    for url in sorted(details):
        try:
            rr = req("GET", url)
            final = str(getattr(rr, "url", "") or url)
            pd = _v18_icims_date(rr.text)
            if not pd or pd < CUTOFF:
                continue
            j = _job_from_detail(src, final, rr.text)
            if not j:
                j = _direct_board_job(src, final, rr.text)
            if j and j.id not in seen_ids:
                seen_ids.add(j.id)
                out.append(j)
        except Exception:
            continue
    return out


def _paylocity_board_root(url):
    """Return normalized Paylocity All-jobs board URL when possible."""
    p = urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    try:
        i = next(i for i, x in enumerate(parts) if x.lower() == "jobs")
    except StopIteration:
        return url
    # Detail URL: /Recruiting/Jobs/Details/123 -> cannot infer board GUID.
    if len(parts) > i + 1 and parts[i + 1].lower() == "details":
        return url
    return url




def _paylocity_rendered_detail(src, url, raw):
    """Parse Paylocity's rendered detail view without inventing a post date."""
    soup = BeautifulSoup(raw, "html.parser")
    txt = clean(soup.get_text(" "))

    # First retain the normal structured parser whenever Paylocity exposes it.
    j = _job_from_detail(src, url, raw)
    if j:
        return j

    # Paylocity's SPA uses several labels across tenant versions. Require an
    # explicit posting date from rendered text/application state.
    normalized = html.unescape(raw or "").replace("\\/", "/")
    pd = None
    date_patterns = [
        r"(?:Date Posted|Posted Date|Posting Date|Posted On|Date)\s*:?\s*"
        r"([A-Za-z]{3,9}\s+\d{1,2},\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2}|\d{4}-\d{2}-\d{2})",
        r'["\'](?:datePosted|postedDate|postingDate|createdDate|createDate)["\']\s*:\s*["\']([^"\']+)["\']',
    ]
    for pat in date_patterns:
        m = re.search(pat, normalized if "date" in pat.lower() and "[\"\\']" in pat else txt, re.I)
        if m:
            pd = pdate(m.group(1))
            if pd:
                break
    if not pd:
        # Diagnostic only: never manufacture a date.
        print(
            f"Paylocity detail reject {src['Company']}: no_date "
            f"url={url} title={clean((soup.find('h1') or soup.find('h2') or soup.title).get_text(' ') if (soup.find('h1') or soup.find('h2') or soup.title) else '')[:100]} "
            f"text={txt[:260]}"
        )
        return None
    if pd < feed_cutoff(jobtype("", txt)) or pd > TODAY:
        print(f"Paylocity detail reject {src['Company']}: date={pd} url={url}")
        return None

    # Prefer the visible detail heading; reject board/application chrome.
    title = ""
    bad = {
        "job opportunities", "job details", "apply now", "nrg media llc",
        "careers", "employment opportunities",
    }
    for node in soup.find_all(["h1","h2","h3"]):
        cand = clean(node.get_text(" "))
        if cand and cand.lower() not in bad and 3 <= len(cand) <= 180:
            title = cand
            break
    if not title:
        m = re.search(
            r"(?:Job Title|Position Title)\s*:?\s*(.{3,180}?)(?=\s+(?:Location|Department|Date Posted|Posted Date|Job Type|$))",
            txt, re.I,
        )
        if m:
            title = clean(m.group(1))
    if not title:
        print(f"Paylocity detail reject {src['Company']}: no_title url={url} text={txt[:260]}")
        return None

    main = (
        soup.find("main")
        or soup.find(attrs={"class": re.compile(r"(job.?description|job.?detail|description)", re.I)})
        or soup
    )
    desc = format_description(str(main))
    if len(strip_html(desc)) < 200:
        print(f"Paylocity detail reject {src['Company']}: short_desc={len(strip_html(desc))} title={title[:100]} url={url}")
        return None

    loc = ""
    for pat in (
        r"(?:Job Location|Location)\s*:?\s*(.{2,100}?)(?=\s+(?:Department|Job Type|Employment Type|Date Posted|Posted Date|Apply|$))",
        r"\b([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b",
    ):
        m = re.search(pat, txt, re.I)
        if m:
            loc = clean(m.group(1))
            break
    city = state = ""
    mm = re.match(r"(.+?),\s*([A-Z]{2})\b", loc)
    if mm:
        city, state = clean(mm.group(1)), mm.group(2).upper()
    else:
        city = loc

    m_id = re.search(r"/recruiting/jobs/details/(\d+)", url, re.I)
    jid = m_id.group(1) if m_id else hashlib.sha1(url.encode()).hexdigest()[:16]
    canonical = url.split("#",1)[0]
    return Job(
        jid, title, src["Company"], desc, pd, jobtype(title, txt),
        category(title, desc, src["Industry"], src["Company"]), canonical,
        src["URL"], src["URL"], "", normalize_work_arrangement(desc, loc or txt),
        city, state, infer_country(loc or txt, src["Company"], desc),
    )


def _paylocity_rendered_v18(src, starts):
    """Bounded Chromium fallback for Paylocity's JavaScript-only public board."""
    if sync_playwright is None:
        print(f"Paylocity rendered {src['Company']}: Playwright unavailable")
        return []
    details = set()
    rendered = {}
    board_stats = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent=SESSION.headers.get(
                    "User-Agent",
                    "MJR-Jobs-Feed/1.0 (+https://www.mediajobsreport.com)",
                ),
                viewport={"width": 1440, "height": 1200},
            )
            page = context.new_page()
            page.set_default_timeout(20000)

            for start_url in starts[:3]:
                before = len(details)
                final_url = ""
                title = ""
                try:
                    resp = page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3500)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    for _ in range(5):
                        page.mouse.wheel(0, 2600)
                        page.wait_for_timeout(500)

                    final_url = page.url
                    title = clean(page.title())
                    hrefs = page.locator("a").evaluate_all(
                        "(els) => els.map(a => a.href).filter(Boolean)"
                    )
                    raw = page.content()
                    for href in hrefs:
                        if re.search(r"/recruiting/jobs/details/\d+", str(href), re.I):
                            details.add(str(href).split("#", 1)[0])
                    # Paylocity may serialize routes in its rendered application
                    # state without creating an anchor until the card is clicked.
                    raw2 = html.unescape(raw or "").replace("\\/", "/")
                    for m in re.finditer(
                        r'(?:https?://recruiting\.paylocity\.com)?'
                        r'(/recruiting/jobs/details/\d+[^"\'<>\s]*)',
                        raw2, re.I,
                    ):
                        details.add(urljoin(final_url or start_url, m.group(1)).split("#",1)[0])
                    status = getattr(resp, "status", None)
                    board_stats.append(
                        f"{status or 'no-status'} {title[:80]} final={final_url} "
                        f"hrefs={len(hrefs)} new_details={len(details)-before}"
                    )
                except Exception as e:
                    board_stats.append(f"error {type(e).__name__}: {clean(str(e))[:160]}")

            # Keep the fallback bounded; radio boards are small.
            for url in sorted(details)[:100]:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1500)
                    rendered[url] = page.content()
                except Exception:
                    continue
            browser.close()
    except Exception as e:
        print(f"Paylocity rendered {src['Company']}: browser error {type(e).__name__}: {clean(str(e))[:180]}")
        return []

    out, seen_ids = [], set()
    parse_failures = 0
    stale = 0
    for url, raw in rendered.items():
        j = _paylocity_rendered_detail(src, url, raw)
        if not j:
            parse_failures += 1
            continue
        if not job_is_fresh(j):
            stale += 1
        if j.id not in seen_ids:
            seen_ids.add(j.id)
            out.append(j)

    print(
        f"Paylocity rendered {src['Company']}: "
        f"boards=[{' || '.join(board_stats)}] details={len(details)} "
        f"rendered_details={len(rendered)} parsed={len(out)} "
        f"parse_failures={parse_failures} not_fresh={stale}"
    )
    return out


def nrg_paylocity(src):
    """NRG Media Paylocity collector.

    NRG's current All board is not exposing detail cards to either the
    server-rendered or Chromium v18 discovery path. Try Paylocity's public job
    feed for the board GUID first; if that tenant has not enabled the feed,
    preserve the full v18 server/rendered recovery path.
    """
    feed_jobs = paylocity(src)
    if feed_jobs or clean(src["Company"]).lower() in PUBLIC_BOARD_ENUMERATION:
        print(f"NRG Paylocity: parsed={len(feed_jobs)}")
        return feed_jobs
    return paylocity_v18(src)


def paylocity_v18(src):
    """Targeted Paylocity public-board crawler.

    Supports both All/{board-guid}/{company} boards and individual Details
    URLs. It enumerates only actual Paylocity detail pages and requires a
    recent explicit posting date before import.
    """
    starts = [src["URL"]]
    company = clean(src.get("Company", "")).lower()

    # Known current public board roots from the source inventory.
    known = {
        "dick broadcasting company": [
            "https://recruiting.paylocity.com/recruiting/jobs/All/da27c45a-0c7a-4cbe-a575-3444d884e49b/Dick-Broadcasting-Company-Inc",
        ],
        "nrg media": [
            "https://recruiting.paylocity.com/recruiting/jobs/All/76da5c58-0cdb-4886-86b6-41d72879e541/NRG-MEDIA-LLC",
        ],
        "weigel": [
            "https://recruiting.paylocity.com/recruiting/jobs/All/7cbe86ee-b534-47b4-9c82-d15e8b55a6cb/Weigel-Broadcasting-Co",
        ],
    }
    starts.extend(known.get(company, []))
    starts = list(dict.fromkeys(starts))

    queue = starts[:]
    seen_pages = set()
    details = set()

    while queue and len(seen_pages) < 160 and len(details) < 5000:
        page = queue.pop(0)
        if page.rstrip("/") in seen_pages:
            continue
        seen_pages.add(page.rstrip("/"))

        try:
            r = req("GET", page)
        except Exception:
            continue

        final = str(getattr(r, "url", "") or page)
        soup = BeautifulSoup(r.text, "html.parser")
        raw = html.unescape(r.text or "").replace("\\/", "/")

        def add(h):
            h = urljoin(final, h)
            hp = urlparse(h)
            if "recruiting.paylocity.com" not in hp.netloc.lower():
                return
            if re.search(r"/recruiting/jobs/details/\d+", hp.path, re.I):
                details.add(h.split("#", 1)[0])

        for a in soup.find_all("a", href=True):
            add(a["href"])
            h = urljoin(final, a["href"])
            hp = urlparse(h)
            if "recruiting.paylocity.com" not in hp.netloc.lower():
                continue
            label = clean(a.get_text(" ")).lower()
            if (
                re.search(r"\b(next|more|view more|load more)\b", label)
                or re.search(r"[?&](page|pageindex|start|offset)=\d+", h, re.I)
            ):
                if h.rstrip("/") not in seen_pages:
                    queue.append(h)

        for m in re.finditer(
            r'https?://recruiting\.paylocity\.com/[^"\'<>\s]*?/jobs/details/\d+[^"\'<>\s]*',
            raw,
            re.I,
        ):
            add(m.group(0).rstrip(".,);"))

        for m in re.finditer(r'["\']([^"\']*/Recruiting/Jobs/Details/\d+[^"\']*)["\']', raw, re.I):
            add(m.group(1))

    # If the source itself is a single detail page (Hope), include it.
    if re.search(r"/recruiting/jobs/details/\d+", src["URL"], re.I):
        details.add(src["URL"])

    out, seen_ids = [], set()
    for url in sorted(details):
        try:
            rr = req("GET", url)
            final = str(getattr(rr, "url", "") or url)
            j = _job_from_detail(src, final, rr.text)
            if not j:
                j = _direct_board_job(src, final, rr.text)
            if j and j.id not in seen_ids:
                seen_ids.add(j.id)
                out.append(j)
        except Exception:
            continue

    print(
        f"Paylocity server {src['Company']}: "
        f"listing_pages={len(seen_pages)} detail_urls={len(details)} parsed={len(out)}"
    )
    if out:
        return out
    return _paylocity_rendered_v18(src, starts)
def _ashby_board_name(url):
    p = urlparse(url)
    if "ashbyhq.com" not in p.netloc.lower():
        return ""
    parts = [unquote(x) for x in p.path.split("/") if x]
    return parts[0] if parts else ""


def ashby(src):
    """Native Ashby public job-board collector.

    Ashby exposes published jobs through a public posting API keyed by the
    board name. The collector uses canonical job URLs and still enforces the
    MJR posting window before emitting jobs.
    """
    board = _ashby_board_name(src["URL"])
    if not board:
        return []

    api = f"https://api.ashbyhq.com/posting-api/job-board/{quote(board)}"
    try:
        r = req("GET", api)
        payload = r.json()
    except Exception:
        return []

    rows = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []

    out, seen_ids = [], set()

    for row in rows:
        if not isinstance(row, dict):
            continue

        # Ashby can expose publishedAt / publishedDate depending on API version.
        pd = None
        for key in ("publishedAt", "publishedDate", "createdAt", "updatedAt"):
            if row.get(key):
                pd = pdate(str(row.get(key)))
                if pd:
                    break
        if not pd or pd < CUTOFF:
            continue

        title = clean(row.get("title") or "")
        if not title:
            continue

        location = clean(
            row.get("location")
            or row.get("locationName")
            or row.get("workplaceLocation")
            or ""
        )

        desc_html = (
            row.get("descriptionHtml")
            or row.get("description")
            or row.get("descriptionPlain")
            or ""
        )
        desc = clean(BeautifulSoup(str(desc_html), "html.parser").get_text(" "))
        if len(desc) < 100:
            continue

        job_url = clean(
            row.get("jobUrl")
            or row.get("applyUrl")
            or row.get("url")
            or ""
        )
        if not job_url:
            jid0 = clean(row.get("id") or "")
            if jid0:
                job_url = f"https://jobs.ashbyhq.com/{quote(board)}/{quote(jid0)}"
        if not job_url:
            continue

        jid = clean(row.get("id") or "")
        if not jid:
            jid = hashlib.sha1(job_url.encode()).hexdigest()[:16]

        if jid in seen_ids:
            continue
        seen_ids.add(jid)

        combined = " ".join(
            clean(str(row.get(k) or ""))
            for k in ("title", "department", "team", "employmentType", "workplaceType")
        )

        out.append(
            Job(
                jid,
                title,
                src["Company"],
                desc,
                pd,
                jobtype(title, combined),
                category(title, desc, src["Industry"], src["Company"]),
                job_url,
                src["URL"],
                src["URL"],
                "",
                normalize_work_arrangement(
                    " ".join([desc, clean(str(row.get("workplaceType") or ""))]),
                    location,
                ),
                location,
                "",
                infer_country(location, src["Company"], desc),
            )
        )

    return out


RADIO_RECOVERY_COMPANIES = {
    "cumulus media", "educational media foundation", "bell media", "evanov",
    "lotus", "midwest communications", "pattison media", "rogers sports & media",
    "stingray", "townsquare media", "stephens media group", "pamal broadcasting",
}

def _radio_recovery_job(src, url, raw):
    j = _job_from_detail(src, url, raw)
    if j:
        return j
    soup = BeautifulSoup(raw, "html.parser")
    txt = clean(soup.get_text(" "))
    if len(txt) < 220:
        return None
    low = txt.lower()
    signals = ("apply","responsibilities","qualifications","requirements","employment",
               "full-time","part-time","position","resume","deadline","department","location")
    if sum(1 for x in signals if x in low) < 2:
        return None
    pd = _direct_board_date(raw)
    if pd and pd < CUTOFF:
        return None
    if not pd:
        # Never manufacture freshness for an undated evergreen radio page.
        # Source-specific collectors may use a persisted first-seen date only
        # when they can prove the URL is an actively enumerated job posting.
        return None
    headings = soup.find_all(["h1", "h2", "h3"])
    title = ""
    bad = {
        "careers",
        "jobs",
        "career opportunities",
        "job openings",
        "midwest careers",
        "stingray jobs",
        "open position at stingray",
    }

    # Prefer a real job-title heading. Stingray pages put "Stingray Jobs" first,
    # then "Open Position at Stingray", then the actual title.
    for node in headings:
        cand = clean(node.get_text(" "))
        cl = cand.lower()
        if not cand or cl in bad:
            continue
        if cl.startswith("job:"):
            cand = clean(cand[4:])
            cl = cand.lower()

        # Reject obvious section/branding headings.
        if cl in bad or cl.startswith("career opportunities"):
            continue

        if 4 <= len(cand) <= 180:
            # On individual Stingray /job/... pages the first remaining H1 is
            # the actual job title, even when it lacks one of our keyword hints.
            if "jobs.stingray.com/job/" in url.lower() and node.name == "h1":
                title = cand
                break

            if any(k in cl for k in (
                "producer","reporter","anchor","host","announcer","sales","account executive",
                "engineer","technician","director","manager","coordinator","assistant",
                "specialist","editor","personality","program","digital","marketing","promotions",
                "developer","analyst","auditor","partner","lead","executive","strategist",
                "operations","content","finance","hr","human resources"
            )):
                title = cand
                break
    if not title:
        return None
    # Lotus/OneCMS pages have a clean WordPress article body but substantial
    # navigation and footer content outside it.
    lotus_main = None
    if clean(src.get("Company", "")).lower() == "lotus":
        lotus_main = soup.select_one(".entry-content, .post-content, article .entry-content")
    main=(lotus_main or soup.find("main") or soup.find("article")
          or soup.find(attrs={"class":re.compile(r"(job.?description|job.?detail|posting|entry-content|career)",re.I)})
          or soup)
    desc=format_description(str(main))
    if len(strip_html(desc))<250:
        return None
    loc=""
    for pat in (
        r"(?:Job Location|Location)\s*:?\s*([A-Za-z0-9 .,'/\-&]+?)(?=\s+(?:Job Type|Employment Type|Category|Department|Posted|Apply|Deadline|$))",
        r"\b([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b",
        r"\b([A-Z][A-Za-z .'-]+,\s*(?:ON|BC|AB|SK|MB|QC|NS|NB|NL|PE))\b",
    ):
        mm=re.search(pat,txt)
        if mm: loc=clean(mm.group(1)); break
    canonical=url.split("#",1)[0]
    jid=hashlib.sha1(canonical.encode()).hexdigest()[:16]
    return Job(jid,title,src["Company"],desc,pd,jobtype(title,txt),
               category(title,desc,src["Industry"],src["Company"]),canonical,
               src["URL"],src["URL"],"",normalize_work_arrangement(desc,loc or txt),
               loc,"",infer_country(loc or txt,src["Company"],desc))


def _radio_page_posted_date(raw):
    """Parse explicit posted dates used by direct radio-company career pages."""
    txt = clean(BeautifulSoup(raw, "html.parser").get_text(" "))
    patterns = (
        r"Posted\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*,?\s*"
        r"([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?\s*,\s*20\d{2})",
        r"(?:Posted|Date Posted|Posted Date)\s*:?\s*"
        r"([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?\s*,\s*20\d{2})",
    )
    for pat in patterns:
        m = re.search(pat, txt, re.I)
        if not m:
            continue
        value = re.sub(r"(\d{1,2})(?:st|nd|rd|th)", r"\1", m.group(1), flags=re.I)
        parsed = pdate(value)
        if parsed:
            return parsed
    return _direct_board_date(raw)


def _radio_direct_detail(src, url, raw, forced_title=""):
    """Build one direct-company radio job while requiring a real posted date."""
    soup = BeautifulSoup(raw, "html.parser")
    txt = clean(soup.get_text(" "))
    pd = _radio_page_posted_date(raw)
    if not pd or pd > TODAY or pd < feed_cutoff(jobtype(forced_title or txt[:180], txt)):
        return None

    h1 = soup.find("h1")
    h2 = soup.find("h2")
    title = clean(forced_title or (h1.get_text(" ") if h1 else "") or (h2.get_text(" ") if h2 else ""))
    if not title or title.lower() in {"careers", "careers list", "career opportunities", "jobs", "employment"}:
        return None

    main = (
        soup.select_one(".entry-content, .post-content, .career-content, .job-content")
        or soup.find("main") or soup.find("article") or soup
    )
    desc = format_description(str(main))
    if len(strip_html(desc)) < 220:
        return None

    loc = ""
    for pat in (
        r"(?:Work Location|Job Location|Location)\s*:?\s*([A-Za-z0-9 .,'/\-&]+?)(?=\s+(?:Job Type|Employment Type|Benefits|Schedule|How to Apply|Posted|$))",
        r"\b([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b",
    ):
        m = re.search(pat, txt)
        if m:
            loc = clean(m.group(1))
            break

    canonical = url.split("#", 1)[0]
    jid = hashlib.sha1((src["Company"] + "|" + title + "|" + canonical).encode()).hexdigest()[:16]
    return Job(
        jid, title, src["Company"], desc, pd, jobtype(title, txt),
        category(title, desc, src["Industry"], src["Company"]), canonical,
        src["URL"], src["URL"], "", normalize_work_arrangement(desc, loc or txt),
        loc, "", infer_country(loc or txt, src["Company"], desc),
    )


def _radiofix_fast_get(url, timeout=4):
    """Single-attempt GET for distributed radio career sites.

    These employer-specific collectors must never use req(), whose nested retry
    policy is appropriate for primary ATS APIs but can turn a dead local radio
    site into a multi-minute stall.
    """
    try:
        r = SESSION.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return None
        return r
    except requests.RequestException:
        return None


def renda_media_direct(src):
    """Enumerate Renda's current first-party career list without shared retries."""
    roots = [
        "https://rendabroadcasting.com/careers-list/",
        src.get("URL", ""),
    ]
    details = set()
    deadline = time.monotonic() + 20.0

    for root in list(dict.fromkeys(x for x in roots if x)):
        if time.monotonic() >= deadline:
            break
        r = _radiofix_fast_get(root, timeout=4)
        if not r:
            continue
        final = str(getattr(r, "url", "") or root)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(final, a["href"]).split("#", 1)[0]
            host = urlparse(href).netloc.lower()
            path = urlparse(href).path.lower().rstrip("/")
            if host not in {"rendamedia.com", "www.rendamedia.com", "rendabroadcasting.com", "www.rendabroadcasting.com"}:
                continue
            if re.search(r"/careers-list/\d+$", path):
                details.add(href)
        for m in re.finditer(r'https?://(?:www\.)?(?:rendamedia|rendabroadcasting)\.com/careers-list/\d+', r.text, re.I):
            details.add(m.group(0).replace("\\/", "/"))
        for m in re.finditer(r'["\'](/careers-list/\d+)["\']', r.text, re.I):
            details.add(urljoin(final, m.group(1)))

    out, seen = [], set()
    for url in sorted(details)[:40]:
        if time.monotonic() >= deadline:
            break
        rr = _radiofix_fast_get(url, timeout=4)
        if not rr:
            continue
        try:
            final = str(getattr(rr, "url", "") or url)
            j = _radio_direct_detail(src, final, rr.text)
            if j and j.id not in seen:
                seen.add(j.id)
                out.append(j)
        except Exception:
            continue
    return out





def connoisseur_paycor(src):
    """Collect Connoisseur's first-party WP Job Openings inventory.

    Connoisseur's career-openings page uses the AWSM WP Job Openings plugin.
    Enumerate the rendered first-party job cards/details there; Paycor is only
    an application destination for some records, not the discovery source.
    """
    board = "https://connoisseurmedia.com/careers/"
    st = load_state()
    detail_urls = []
    paycor_rendered = {}
    paycor_meta = {}
    paycor_board_meta = {}
    def paycor_key(value):
        """Stable Paycor identity independent of source/lang query noise."""
        try:
            pu = urlparse(clean(str(value or "")))
            qu = parse_qs(pu.query)
            cid = clean((qu.get("clientId") or [""])[0])
            jid = clean((qu.get("id") or [""])[0])
            return (cid + "|" + jid) if jid else clean(str(value or "")).split("#", 1)[0]
        except Exception:
            return clean(str(value or "")).split("#", 1)[0]

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=SESSION.headers.get(
                    "User-Agent",
                    "MJR-Jobs-Feed/1.0 (+https://www.mediajobsreport.com)",
                ),
                viewport={"width": 1440, "height": 1200},
            )
            page = context.new_page()

            # Capture the AWSM/WP Job Openings AJAX exchange. The public page
            # hydrates its inventory dynamically, so the request/response is
            # more authoritative than guessing at rendered anchors.
            awsm_requests = []
            awsm_responses = []
            def _cap_req(req):
                try:
                    if "admin-ajax.php" in (req.url or ""):
                        awsm_requests.append({
                            "url": req.url,
                            "method": req.method,
                            "post_data": req.post_data or "",
                        })
                except Exception:
                    pass
            def _cap_resp(resp):
                try:
                    if "admin-ajax.php" in (resp.url or ""):
                        body = resp.text()
                        awsm_responses.append({
                            "url": resp.url,
                            "status": resp.status,
                            "body": (body or "")[:12000],
                        })
                except Exception:
                    pass
            page.on("request", _cap_req)
            page.on("response", _cap_resp)

            embedded_hits = []
            def _embedded_response(resp):
                try:
                    u = resp.url or ""
                    if (
                        resp.request.resource_type in ("xhr", "fetch", "document")
                        and (
                            "recruitingbypaycor.com" in u.lower()
                            or "connoisseurmedia.com" in u.lower()
                        )
                    ):
                        body = ""
                        try:
                            body = resp.text()
                        except Exception:
                            pass
                        embedded_hits.append((
                            resp.request.method,
                            resp.status,
                            resp.request.resource_type,
                            u,
                            body[:8000],
                        ))
                except Exception:
                    pass
            page.on("response", _embedded_response)
            page.goto(board, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(3500)
            print("Connoisseur embedded frames:", " | ".join(fr.url for fr in page.frames))
            try:
                controls = page.locator("a, button")
                limit = min(controls.count(), 80)
                for i in range(limit):
                    el = controls.nth(i)
                    txt = clean(el.inner_text() or "")
                    href = el.get_attribute("href") or ""
                    if re.search(r"(career|job|opening|position|view|search)", txt + " " + href, re.I):
                        print("CONNOISSEUR_CONTROL:", txt[:250], "|", href[:1000])
            except Exception as e:
                print("Connoisseur control scan error:", type(e).__name__, str(e)[:300])
            try:
                for fr in page.frames:
                    if "recruitingbypaycor.com" in (fr.url or "").lower():
                        print("CONNOISSEUR_PAYCOR_FRAME:", fr.url)
                        try:
                            print("CONNOISSEUR_PAYCOR_FRAME_TEXT:", clean(fr.locator("body").inner_text())[:10000])
                        except Exception:
                            pass
            except Exception:
                pass
            page.wait_for_timeout(1500)
            print(f"Connoisseur embedded network: responses={len(embedded_hits)}")
            for method, status, rtype, url, body in embedded_hits[:80]:
                if (
                    "recruitingbypaycor.com" in url.lower()
                    or re.search(r"(job|career|position|requisition|opening)", body[:6000], re.I)
                ):
                    print(
                        "CONNOISSEUR_EMBEDDED_NETWORK:",
                        method, "|", status, "|", rtype, "|", url,
                        "|", clean(body)[:6000]
                    )
            try:
                page.locator(".t-acceptAllButton").click(timeout=1000)
                page.wait_for_timeout(500)
            except Exception:
                pass
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
            except Exception:
                pass

            hrefs = page.locator("a[href]").evaluate_all("(els) => els.map(a => a.href)")
            for h in hrefs:
                h = clean(str(h or "")).split("#", 1)[0]
                if not h:
                    continue
                hp = urlparse(h)
                host = hp.netloc.lower().replace("www.", "")
                path = hp.path.rstrip("/")
                if host == "connoisseurmedia.com" and (
                    re.search(r"/(?:job-openings|career-opportunity)/[^/]+$", path, re.I)
                    or re.search(r"/jobs?/[^/]+$", path, re.I)
                ):
                    detail_urls.append(h)

            # AWSM may keep the listing URL in card data attributes rather than
            # a conventional anchor. Capture those first-party values too.
            try:
                vals = page.locator("[class*='awsm-job'], [data-job-id], [data-id]").evaluate_all(
                    """els => els.flatMap(e => Array.from(e.attributes || [])
                        .map(a => a.value)
                        .filter(v => /connoisseurmedia\\.com\\/(?:job-openings|career-opportunity|job)\\//i.test(v)))"""
                )
                detail_urls.extend(clean(str(v or "")).split("#", 1)[0] for v in vals if v)
            except Exception:
                pass

            # Probe Paycor/Newton CareerV3 directly. Connoisseur's frame
            # source references /career/css/careerv3/newton.css, so enumerate
            # the sibling JS/resources and inspect them for the inventory
            # request used by Newton's public career UI.
            try:
                paycor_url = "https://recruitingbypaycor.com/career/iframe.action?clientId=8a7883d082ae53c80182f17d3aba194b"
                probe = context.new_page()
                probe.goto(paycor_url, wait_until="domcontentloaded", timeout=15000)
                probe.wait_for_timeout(2500)
                print("CONNOISSEUR_NEWTON_URL:", probe.url)
                resources = probe.locator("script[src], link[href]").evaluate_all(
                    """els => els.map(e => e.src || e.href || "").filter(Boolean)"""
                )
                for u in resources:
                    if re.search(r"(careerv3|newton|career|recruit)", u, re.I):
                        print("CONNOISSEUR_NEWTON_RESOURCE:", u[:3000])
                        if re.search(r"\\.js(?:\\?|$)", u, re.I):
                            try:
                                rr = probe.request.get(u, timeout=7000)
                                body = rr.text()
                                print("CONNOISSEUR_NEWTON_JS:", u[:1500], "|", clean(body)[:12000])
                            except Exception as ex:
                                print("CONNOISSEUR_NEWTON_JS_ERROR:", u[:1500], type(ex).__name__, str(ex)[:300])
                ph = probe.content()
                for pat in [
                    r"[^\\\"']*(?:Career|Job|Position|Requisition)[^\\\"']*\\.(?:action|json|do)[^\\\"']*",
                    r"/(?:career|Career)/[^\\\"'<> ]+",
                ]:
                    try:
                        for hit in re.findall(pat, ph, re.I)[:80]:
                            print("CONNOISSEUR_NEWTON_ENDPOINT_HINT:", clean(str(hit))[:3000])
                    except Exception:
                        pass
                probe.close()
            except Exception as e:
                print("Connoisseur Newton probe failed:", type(e).__name__, str(e)[:500])

            # Inspect the "View All Current Openings" handoff itself.
            # The landing page may launch Paycor through a form/script rather
            # than exposing its inventory as ordinary anchors.
            try:
                handoff = context.new_page()
                handoff.goto("https://connoisseurmedia.com/career-openings/", wait_until="domcontentloaded", timeout=15000)
                handoff.wait_for_timeout(2000)
                print("CONNOISSEUR_HANDOFF_URL:", handoff.url)
                for fr in handoff.frames:
                    print("CONNOISSEUR_HANDOFF_FRAME:", fr.url)
                    if "recruitingbypaycor.com" in (fr.url or "").lower():
                        try:
                            print("CONNOISSEUR_HANDOFF_PAYCOR_TEXT:", clean(fr.locator("body").inner_text())[:20000])
                        except Exception as ex:
                            print("CONNOISSEUR_HANDOFF_PAYCOR_TEXT_ERROR:", type(ex).__name__, str(ex)[:300])
                        try:
                            fres = fr.locator("script[src], link[href]").evaluate_all(
                                "els => els.map(e => e.src || e.href || '').filter(Boolean)"
                            )
                            for u in fres[:120]:
                                print("CONNOISSEUR_HANDOFF_PAYCOR_RESOURCE:", str(u)[:3000])
                        except Exception as ex:
                            print("CONNOISSEUR_HANDOFF_PAYCOR_RESOURCE_ERROR:", type(ex).__name__, str(ex)[:300])
                        try:
                            fh = fr.locator("a[href]").evaluate_all("""els => els.map(e => ({
                                text:(e.innerText || '').trim(),
                                href:e.href || '',
                                parent:(e.parentElement?.innerText || '').trim(),
                                grand:(e.parentElement?.parentElement?.innerText || '').trim()
                            }))""")
                            # Pair each job anchor with its containing Paycor
                            # listing card text so we retain the board's location.
                            try:
                                cards = fr.locator("a[href*='JobIntroduction.action']").evaluate_all("""els => els.map(a => ({
                                    href: a.href || '',
                                    text: (a.innerText || '').replace(/\\s+/g,' ').trim(),
                                    card: ((a.closest('li, tr, article, [class*=job], [class*=position]') || a.parentElement || a).innerText || '').replace(/\\s+/g,' ').trim()
                                }))""")
                                for card in cards:
                                    hu = clean(str(card.get("href") or "")).split("#",1)[0]
                                    if hu:
                                        # Normalize away source/lang noise so board metadata
                                        # matches the canonical detail URL used downstream.
                                        parsed_hu = urlparse(hu)
                                        q_hu = parse_qs(parsed_hu.query)
                                        cid_hu = (q_hu.get("clientId") or [""])[0]
                                        jid_hu = (q_hu.get("id") or [""])[0]
                                        meta_key = cid_hu + "|" + jid_hu if jid_hu else hu
                                        paycor_board_meta[meta_key] = {
                                            "title": clean(str(card.get("text") or "")),
                                            "card": clean(str(card.get("card") or "")),
                                        }
                            except Exception:
                                pass
                            paycor_found = 0
                            for item in fh[:300]:
                                href = clean(str(item.get("href") or "")).split("#", 1)[0]
                                if re.search(r"/career/JobIntroduction\.action\?", href, re.I):
                                    detail_urls.append(href)
                                    title_text = clean(str(item.get("text") or ""))
                                    ph = urlparse(href)
                                    qh = parse_qs(ph.query)
                                    cid = (qh.get("clientId") or [""])[0]
                                    jid = (qh.get("id") or [""])[0]
                                    mk = cid + "|" + jid if jid else href
                                    existing = paycor_board_meta.get(mk) or {}
                                    # Paycor often puts the clickable target on an
                                    # empty/icon anchor while the title and address
                                    # live in its parent container. Preserve that
                                    # nearby listing text with the job ID itself.
                                    nearby = str(item.get("parent") or "")
                                    grand = str(item.get("grand") or "")
                                    context_text = nearby if len(clean(nearby)) >= len(clean(title_text)) + 3 else grand
                                    lines = [
                                        clean(x) for x in re.split(r"[\\r\\n]+", context_text)
                                        if clean(x)
                                    ]
                                    if not title_text:
                                        for line in lines:
                                            if (
                                                len(line) <= 180
                                                and not re.search(r"^(?:apply|view|details|career openings?)$", line, re.I)
                                                and not re.match(r"^\\d{1,6}\\s+", line)
                                            ):
                                                title_text = line
                                                break
                                    existing["title"] = title_text or existing.get("title", "")
                                    existing["card"] = clean(context_text) or existing.get("card", "")
                                    existing["card_lines"] = lines
                                    paycor_board_meta[mk] = existing
                                    paycor_found += 1
                                print("CONNOISSEUR_HANDOFF_PAYCOR_LINK:", clean(str(item))[:4000])
                            print("CONNOISSEUR_PAYCOR_ENUMERATED:", paycor_found)
                            for _mk, _mv in list(paycor_board_meta.items())[:3]:
                                print("CONNOISSEUR_PAYCOR_BOARD_META:", _mk, "|", clean(str(_mv))[:1800])

                            # Paycor occasionally returns a blank CareerHome iframe.
                            # Retry the first-party board directly before accepting zero.
                            if paycor_found == 0:
                                retry_url = "https://recruitingbypaycor.com/career/CareerHome.action?clientId=8a7883d082ae53c80182f17d3aba194b"
                                for _attempt in range(2):
                                    try:
                                        rp = context.new_page()
                                        rp.goto(retry_url, wait_until="domcontentloaded", timeout=20000)
                                        rp.wait_for_timeout(2500)
                                        retry_links = rp.locator("a[href*='JobIntroduction.action']").evaluate_all(
                                            "els => els.map(a => ({href:a.href||'', text:(a.innerText||'').trim(), card:((a.parentElement?.parentElement?.innerText||a.parentElement?.innerText||'')).replace(/\\s+/g,' ').trim()}))"
                                        )
                                        rp.close()
                                        for item in retry_links:
                                            hu = clean(str(item.get("href") or "")).split("#", 1)[0]
                                            if not hu:
                                                continue
                                            detail_urls.append(hu)
                                            ph = urlparse(hu)
                                            qh = parse_qs(ph.query)
                                            cid = (qh.get("clientId") or [""])[0]
                                            jid = (qh.get("id") or [""])[0]
                                            mk = cid + "|" + jid if jid else hu
                                            paycor_board_meta[mk] = {
                                                "title": clean(str(item.get("text") or "")),
                                                "card": clean(str(item.get("card") or "")),
                                            }
                                        paycor_found = len(retry_links)
                                        print("CONNOISSEUR_PAYCOR_RETRY_ENUMERATED:", paycor_found)
                                        if paycor_found:
                                            break
                                    except Exception as ex:
                                        print("CONNOISSEUR_PAYCOR_RETRY_ERROR:", type(ex).__name__, str(ex)[:200])

                            # Render Paycor detail pages in the authenticated/live
                            # browser context. Standalone HTTP requests to these
                            # JobIntroduction pages do not expose parseable content.
                            paycor_urls = list(dict.fromkeys(
                                u for u in detail_urls
                                if "recruitingbypaycor.com/career/JobIntroduction.action" in u
                            ))
                            detail_page = context.new_page()
                            detail_page.set_default_timeout(8000)
                            for n, job_url in enumerate(paycor_urls[:300], 1):
                                try:
                                    detail_page.goto(job_url, wait_until="domcontentloaded", timeout=9000)
                                    detail_page.wait_for_timeout(350)
                                    rendered_html = detail_page.content()
                                    rendered_text = clean(detail_page.locator("body").inner_text())
                                    # Paycor occasionally returns Connoisseur's generic
                                    # wrapper instead of the requested posting. Do not retry
                                    # every bad detail here: skip it for this crawl and let
                                    # normal state/retention preserve previously seen jobs.
                                    wrapper_marker = "Connoisseur Media is an equal-opportunity employer"
                                    # The Connoisseur wrapper is generic; the actual
                                    # Paycor job description lives inside its iframe.
                                    for _job_frame in detail_page.frames:
                                        if "recruitingbypaycor.com" not in (_job_frame.url or "").lower():
                                            continue
                                        try:
                                            _frame_text = clean(_job_frame.locator("body").inner_text())
                                            _frame_html = _job_frame.locator("body").inner_html()
                                            if len(_frame_text) >= 200:
                                                rendered_text = _frame_text
                                                rendered_html = "<html><body>" + _frame_html + "</body></html>"
                                                break
                                        except Exception:
                                            pass
                                    if n == 1:
                                        print("CONNOISSEUR_PAYCOR_DETAIL_URL:", detail_page.url)
                                        print("CONNOISSEUR_PAYCOR_DETAIL_TEXT:", rendered_text[:12000])
                                        for _fr in detail_page.frames:
                                            print("CONNOISSEUR_PAYCOR_DETAIL_FRAME:", _fr.url)
                                            try:
                                                _ft = clean(_fr.locator("body").inner_text())
                                                if _ft and _ft != rendered_text:
                                                    print("CONNOISSEUR_PAYCOR_DETAIL_FRAME_TEXT:", _ft[:12000])
                                            except Exception:
                                                pass
                                        try:
                                            _detail_links = detail_page.locator("a[href]").evaluate_all(
                                                "els => els.map(a => ({text:(a.innerText||'').trim(), href:a.href||''})).filter(x => /job|description|position|apply|career/i.test(x.text+' '+x.href))"
                                            )
                                            print("CONNOISSEUR_PAYCOR_DETAIL_LINKS:", clean(str(_detail_links))[:12000])
                                        except Exception:
                                            pass
                                    if wrapper_marker in rendered_text and "Skip to content" in rendered_text:
                                        print("CONNOISSEUR_PAYCOR_WRAPPER_SKIPPED:", n, job_url[:500])
                                        continue
                                    pj = urlparse(job_url)
                                    qj = parse_qs(pj.query)
                                    render_key = ((qj.get("clientId") or [""])[0] + "|" + (qj.get("id") or [""])[0])
                                    bm = paycor_board_meta.get(render_key) or paycor_board_meta.get(job_url) or {}
                                    paycor_rendered[render_key] = (
                                        rendered_html,
                                        rendered_text,
                                        clean(str(bm.get("title") or "")),
                                        clean(str(bm.get("card") or "")),
                                    )
                                    try:
                                        meta = detail_page.locator("body").evaluate("""body => {
                                            const norm = s => (s || '').replace(/\\s+/g,' ').trim();
                                            const headings = Array.from(body.querySelectorAll('h1,h2,h3,.job-title,.position-title,[class*=title]'))
                                                .map(e => norm(e.innerText)).filter(Boolean);
                                            const title = headings.find(x => !/career openings|connoisseur media|job description|apply/i.test(x)) || '';
                                            const text = norm(body.innerText);
                                            const loc = text.match(/(?:Location|Job Location)\\s*:?\\s*([^\\n]{2,180})/i);
                                            return {title, location: loc ? norm(loc[1]) : ''};
                                        }""")
                                        if meta:
                                            paycor_meta[render_key] = meta
                                    except Exception:
                                        pass
                                except Exception as ex:
                                    print("CONNOISSEUR_PAYCOR_DETAIL_ERROR:", n, job_url[:500], type(ex).__name__, str(ex)[:300])
                            detail_page.close()
                            print("CONNOISSEUR_PAYCOR_RENDERED:", len(paycor_rendered))
                        except Exception as ex:
                            print("CONNOISSEUR_HANDOFF_PAYCOR_LINK_ERROR:", type(ex).__name__, str(ex)[:300])
                forms = handoff.locator("form").evaluate_all("""els => els.map(f => ({
                    action: f.action || "",
                    method: f.method || "",
                    text: (f.innerText || "").slice(0,1000),
                    html: f.outerHTML.slice(0,4000)
                }))""")
                for item in forms[:30]:
                    print("CONNOISSEUR_HANDOFF_FORM:", clean(str(item))[:5000])
                scripts = handoff.locator("script").evaluate_all(
                    "els => els.map(s => (s.src || '') + ' ' + (s.textContent || '')).filter(x => /paycor|recruit|clientId|careerhome|opening/i.test(x))"
                )
                for item in scripts[:30]:
                    print("CONNOISSEUR_HANDOFF_SCRIPT:", clean(str(item))[:8000])
                html = handoff.content()
                for m in re.findall(r"https?://[^\\\"'<> ]+", html, re.I):
                    if re.search(r"(paycor|recruit|career|job|opening)", m, re.I):
                        print("CONNOISSEUR_HANDOFF_LINK:", m[:3000])
                handoff.close()
            except Exception as e:
                print("Connoisseur handoff diagnostic failed:", type(e).__name__, str(e)[:500])

            print(f"Connoisseur AWSM board: hrefs={len(hrefs)} details={len(detail_urls)}")
            print(f"Connoisseur AWSM AJAX: requests={len(awsm_requests)} responses={len(awsm_responses)}")
            for item in awsm_requests[:20]:
                pdata = clean(str(item.get("post_data") or ""))
                print(
                    "CONNOISSEUR_AWSM_REQUEST:",
                    item.get("method"), "|", item.get("url"), "|", pdata[:4000]
                )
            for item in awsm_responses[:20]:
                body = clean(str(item.get("body") or ""))
                print(
                    "CONNOISSEUR_AWSM_RESPONSE:",
                    item.get("status"), "|", item.get("url"), "|", body[:8000]
                )
            browser.close()
    except Exception as e:
        print(f"Connoisseur AWSM render failed: {e}")

    detail_urls = list(dict.fromkeys(detail_urls))
    out = []
    for url in detail_urls[:300]:
        key = url.rstrip("/").lower()
        stable_paycor_key = paycor_key(url)
        rendered = paycor_rendered.get(stable_paycor_key) or paycor_rendered.get(url)
        if rendered:
            raw_html, txt, rendered_title, rendered_card = rendered
            class _RenderedResponse:
                text = raw_html
            r = _RenderedResponse()
        else:
            try:
                r = _req_raw("GET", url, timeout=4, tries=1)
            except Exception:
                continue
            raw_html = r.text
            txt = clean(BeautifulSoup(raw_html, "html.parser").get_text(" "))
        soup = BeautifulSoup(raw_html, "html.parser")
        stable_paycor_key = paycor_key(url)
        board_meta_direct = paycor_board_meta.get(stable_paycor_key) or {}
        direct_title = clean(str(board_meta_direct.get("title") or rendered_title or "")) if rendered else clean(str(board_meta_direct.get("title") or ""))
        direct_card = clean(str(board_meta_direct.get("card") or rendered_card or "")) if rendered else clean(str(board_meta_direct.get("card") or ""))

        # Paycor iframe pages do not expose the structured title/date fields
        # expected by the generic detail parser. When we have the authoritative
        # board record plus the real iframe body, construct the job directly.
        if rendered and direct_title and txt and len(txt) >= 200:
            loc_matches = re.findall(
                r"([A-Z][A-Za-z .'-]{1,80}),[ ]*([A-Z]{2})(?:[ ]+[0-9]{5}(?:-[0-9]{4})?)?",
                direct_card,
            )
            city, state = "", ""
            if loc_matches:
                city, state = loc_matches[-1]
                city = clean(city.split(",")[-1])
                state = state.upper()

            pd = _direct_board_date(txt)
            if not pd:
                # Paycor does not consistently publish a posting date. Preserve
                # the first-seen date by stable Paycor job identity rather than
                # assigning TODAY on every crawl. Global state is URL-keyed, so
                # locate the prior record by the deterministic Job.id generated
                # from clientId|Paycor job id.
                stable_job_id = hashlib.sha1(stable_paycor_key.encode()).hexdigest()[:16]
                stored = {}
                for _state_row in st.values():
                    if not isinstance(_state_row, dict):
                        continue
                    _stored_job = _state_row.get("job") or {}
                    if isinstance(_stored_job, dict) and clean(str(_stored_job.get("id") or "")) == stable_job_id:
                        stored = _stored_job
                        break
                try:
                    pd = date.fromisoformat(str(stored.get("date") or ""))
                except Exception:
                    # First production sighting only: establish the first-seen
                    # date. Subsequent crawls recover this value by Paycor ID.
                    pd = TODAY
            jt = jobtype(direct_title, txt)
            cat = category(direct_title, txt, src["Industry"], src["Company"])
            _ct = direct_title.lower()
            if "intern" in _ct:
                cat = "Internships"
            elif any(x in _ct for x in ("account executive", "account manager", "sales executive", "sales director", "sales manager", "marketing consultant", "digital sales")):
                cat = "Sales & Marketing"
            elif any(x in _ct for x in ("network administrator", "chief engineer", "remote technician")):
                cat = "Engineering"
            elif "traffic coordinator" in _ct or "sales assistant" in _ct or "administrative" in _ct:
                cat = "Business Office"
            elif any(x in _ct for x in ("on-air", "on air", "board operator", "program director", "news reporter", "street team", "promotions")):
                cat = "Radio"

            # Keep only the actual iframe posting text. Paycor's body HTML
            # also contains application-form controls that polluted the feed.
            desc_html = "<p>" + (
                txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            ) + "</p>"
            out.append(Job(
                hashlib.sha1(stable_paycor_key.encode()).hexdigest()[:16],
                direct_title, src["Company"], desc_html, pd, jt, cat,
                url, src["URL"], src["URL"], "",
                normalize_work_arrangement(txt, (city + ", " + state).strip(", ")),
                city, state, "US",
            ))
            continue

        j = _job_from_detail(src, url, raw_html)
        if j:
            if rendered:
                meta = paycor_meta.get(stable_paycor_key) or paycor_meta.get(url) or {}
                pu = urlparse(url)
                qu = parse_qs(pu.query)
                meta_key = ((qu.get("clientId") or [""])[0] + "|" + (qu.get("id") or [""])[0])
                board_meta = paycor_board_meta.get(stable_paycor_key) or paycor_board_meta.get(meta_key) or paycor_board_meta.get(url) or {}
                real_title = clean(str(rendered_title or board_meta.get("title") or meta.get("title") or ""))
                # The Paycor iframe now supplies the true description,
                # while the live board remains the authoritative title source.
                if real_title:
                    j.title = real_title
                    j.jobtype = jobtype(j.title, txt)
                    j.category = category(j.title, txt, src["Industry"], src["Company"])
                    _ct = j.title.lower()
                    if "intern" in _ct:
                        j.category = "Internships"
                    elif any(x in _ct for x in ("account executive", "account manager", "sales executive", "sales director", "sales manager", "marketing consultant", "digital sales")):
                        j.category = "Sales & Marketing"
                    elif any(x in _ct for x in ("network administrator", "chief engineer", "remote technician")):
                        j.category = "Engineering"
                    elif "traffic coordinator" in _ct or "sales assistant" in _ct or "administrative" in _ct:
                        j.category = "Business Office"
                    elif any(x in _ct for x in ("on-air", "on air", "board operator", "program director", "news reporter", "street team", "promotions")):
                        j.category = "Radio"

                # The live Paycor listing carries the physical address even when
                # JobIntroduction renders a generic shell. Bind city/state from
                # that same job-ID record instead of leaving Google Jobs location
                # fields blank.
                card_lines = board_meta.get("card_lines") or []
                card_text = " | ".join(card_lines) or clean(str(rendered_card or board_meta.get("card") or ""))
                loc_matches = re.findall(
                    r"\\b([A-Z][A-Za-z .'-]{1,80}),\\s*([A-Z]{2})(?:\\s+\\d{5}(?:-\\d{4})?)?\\b",
                    card_text,
                )
                if loc_matches:
                    loc_city, loc_state = loc_matches[-1]
                    # Address text can precede the final city; keep only the
                    # final comma-delimited place component.
                    j.city = clean(loc_city.split(",")[-1])
                    j.state = loc_state.upper()
                    j.country = "US"
                    j.work_arrangement = normalize_work_arrangement(
                        txt, j.city + ", " + j.state
                    )

                # Fall back to the board anchor text if Paycor omits a usable H1.
                if j.title.lower() in ("career openings", "careers", "job openings"):
                    try:
                        anchor_title = next(
                            clean(str(x.get("text") or "")) for x in fh
                            if clean(str(x.get("href") or "")).split("#",1)[0] == url
                        )
                        if anchor_title:
                            j.title = anchor_title
                    except Exception:
                        pass
            # Connoisseur is radio-first for programming/on-air/promotions.
            probe = (j.title + " " + strip_html(j.description)[:1200])
            if re.search(r"\\b(program|on[- ]?air|air talent|host|promotion|content director|producer|board operator)\\b", probe, re.I):
                if not re.search(r"\\b(sales|account executive|market manager|general manager)\\b", j.title, re.I):
                    j.category = "Radio"
            if job_is_fresh(j):
                out.append(j)
            continue

        h1 = soup.find("h1")
        title = clean(h1.get_text(" ") if h1 else "")
        if not title:
            continue
        main = (
            soup.find(attrs={"class": re.compile(r"(awsm-job-content|job-content|entry-content)", re.I)})
            or soup.find("main") or soup.find("article") or soup
        )
        desc = format_description(str(main))
        plain = clean(main.get_text(" "))
        if len(strip_html(desc)) < 120:
            continue

        pd = _direct_board_date(r.text)
        if not pd:
            stored = st.get(key, {}).get("job", {}) if isinstance(st.get(key), dict) else {}
            try:
                pd = date.fromisoformat(str(stored.get("date") or ""))
            except Exception:
                pd = TODAY

        jt = jobtype(title, txt)
        if not job_is_fresh(Job("", title, src["Company"], desc, pd, jt, "", url,
                                board, board, "", "", "", "", "")):
            continue

        city = state = ""
        m = re.search(r"(?:Location|Job Location)\\s*:?\\s*([A-Z][A-Za-z .'-]+),\\s*([A-Z]{2})\\b", txt, re.I)
        if m:
            city, state = clean(m.group(1)), m.group(2).upper()

        cat = category(title, plain, src["Industry"], src["Company"])
        if re.search(r"\\b(program|on[- ]?air|air talent|host|promotion|content director|producer|board operator)\\b", title + " " + plain[:1200], re.I):
            if not re.search(r"\\b(sales|account executive|market manager|general manager)\\b", title, re.I):
                cat = "Radio"

        apply_url = url
        for a in soup.find_all("a", href=True):
            label = clean(a.get_text(" "))
            h = urljoin(url, a["href"])
            if re.search(r"\\b(apply|application)\\b", label, re.I) and h.startswith("http"):
                apply_url = h
                break

        out.append(Job(
            hashlib.sha1(key.encode()).hexdigest()[:16], title, src["Company"],
            desc, pd, jt, cat, apply_url, url, board, "",
            normalize_work_arrangement(txt, (city + (", " + state if state else ""))),
            city, state, infer_country((city + " " + state), src["Company"], plain),
        ))

    print(f"Connoisseur AWSM active board: enumerated={len(detail_urls)} parsed={len(out)}")
    return out


def connoisseur_direct(src):
    """Collect Connoisseur's authoritative active career-opportunity pages."""
    roots = [
        "https://connoisseurmedia.com/careers/",
        "https://connoisseurmedia.com/career-opportunity/",
    ]
    st = load_state()
    detail_urls = []
    for root in roots:
        try:
            r = _req_raw("GET", root, timeout=5, tries=1)
        except Exception:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            h = urljoin(str(getattr(r, "url", "") or root), a["href"]).split("#", 1)[0]
            if re.search(r"connoisseurmedia\.com/career-opportunity/[^/?#]+/?$", h, re.I):
                detail_urls.append(h)
    detail_urls = list(dict.fromkeys(detail_urls))

    # The current Connoisseur careers inventory is hydrated client-side.
    # If the server HTML exposes no detail links, render only the authoritative
    # careers page and harvest first-party /career-opportunity/ URLs.
    if not detail_urls:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto("https://connoisseurmedia.com/careers/", wait_until="domcontentloaded", timeout=15000)
                try:
                    page.wait_for_timeout(2500)
                except Exception:
                    pass
                # One bounded scroll is enough to trigger lazy-loaded cards.
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
                hrefs = page.locator("a[href]").evaluate_all(
                    "(els) => els.map(a => a.href)"
                )
                # Paycor's hosted widget can live in an iframe. Capture its
                # actual recruitingbypaycor.com source and links rather than
                # guessing a tenant/client ID.
                frame_urls = [fr.url for fr in page.frames if fr.url]
                paycor_frames = [
                    u for u in frame_urls
                    if "recruitingbypaycor.com" in u.lower()
                ]
                for fr in page.frames:
                    if "recruitingbypaycor.com" not in (fr.url or "").lower():
                        continue
                    try:
                        hrefs.extend(fr.locator("a[href]").evaluate_all(
                            "(els) => els.map(a => a.href)"
                        ))
                    except Exception:
                        pass
                print("CONNOISSEUR_PAYCOR_FRAMES:", " | ".join(paycor_frames))
                for h in hrefs:
                    h = clean(str(h or "")).split("#", 1)[0]
                    if (
                        "recruitingbypaycor.com" in h.lower()
                        and re.search(r"(?:JobIntroduction\\.action|[?&](?:id|jobId)=)", h, re.I)
                    ) or (
                        "connoisseurmedia.com/career-openings/" in h.lower()
                        and re.search(r"[?&]gnk=job(?:&|$)", h, re.I)
                        and re.search(r"[?&]gni=", h, re.I)
                    ):
                        detail_urls.append(h)
                print(
                    f"Connoisseur rendered board: status={page.url} "
                    f"hrefs={len(hrefs)} details={len(detail_urls)}"
                )
                if not detail_urls:
                    for _h in hrefs[:80]:
                        print("CONNOISSEUR_HREF:", clean(str(_h or "")))
                browser.close()
        except Exception as e:
            print(f"Connoisseur rendered board failed: {e}")

    detail_urls = list(dict.fromkeys(detail_urls))
    out = []
    for url in detail_urls[:250]:
        key = url.rstrip("/").lower()
        try:
            r = _req_raw("GET", url, timeout=5, tries=1)
        except Exception:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        txt = clean(soup.get_text(" "))
        h1 = soup.find("h1")
        title = clean(h1.get_text(" ") if h1 else "")
        if not title:
            continue

        # Description ends before the application form where possible.
        main = soup.find("main") or soup.find("article") or soup
        for form in main.find_all("form"):
            form.decompose()
        desc = format_description(str(main))
        plain = clean(main.get_text(" "))
        if len(strip_html(desc)) < 200:
            continue

        pd = _direct_board_date(r.text)
        if not pd:
            stored = st.get(key, {}).get("job", {}) if isinstance(st.get(key), dict) else {}
            try:
                pd = date.fromisoformat(str(stored.get("date") or ""))
            except Exception:
                pd = TODAY

        jt = jobtype(title, txt)
        probe = Job("", title, src["Company"], desc, pd, jt, "", url,
                    src["URL"], src["URL"], "", "", "", "")
        if not job_is_fresh(probe):
            continue

        city = state = ""
        country = "US"
        m = re.search(r"(?:Location\s*:?\s*)?([A-Z][A-Za-z .'-]+),\s*([A-Z]{2})\b", txt)
        if m:
            city, state = clean(m.group(1)), m.group(2)
        elif re.search(r"\bVarious Markets Nationwide\b", txt, re.I):
            city = "Various Markets Nationwide"

        cat = category(title, plain, src["Industry"], src["Company"])
        # Connoisseur is radio-first; programming/on-air/promotions/content
        # openings belong in Radio unless a more specific business function
        # (Management/Sales/etc.) is explicit.
        if re.search(r"\b(program|on[- ]?air|air talent|host|promotion|content director)\b", title + " " + plain[:1200], re.I):
            if not re.search(r"\b(sales|market manager|general manager)\b", title, re.I):
                cat = "Radio"

        out.append(Job(
            hashlib.sha1(key.encode()).hexdigest()[:16],
            title, src["Company"], desc, pd, jt, cat, url,
            src["URL"], src["URL"], "",
            normalize_work_arrangement(txt, (city + (", " + state if state else ""))),
            city, state, country,
        ))
    print(f"Connoisseur active board: enumerated={len(detail_urls)} parsed={len(out)}")
    return out


def midwest_family_direct(src):
    """Collect Mid-West Family openings from verified local market career pages."""
    markets = [
        ("Madison, WI", "https://www.midwestfamilymadison.com/careers/"),
        ("La Crosse, WI", "https://midwestfamilylacrosse.com/careers/"),
        ("Southwest, MI", "https://www.midwestfamilyswmi.com/careers/"),
        ("Springfield, MO", "https://www.mwfmarketing.fm/category/careers/"),
        ("Eau Claire, WI", "https://www.midwestfamilyeauclaire.com/careers/"),
        ("Rockford, IL", "https://midwestfamilynorthernillinois.com/careers/"),
        ("South Bend, IN", "https://www.midwestfamilysouthbend.com/careers/"),
    ]
    st = load_state()
    out, seen = [], set()

    def stable_date(key, explicit=None):
        if explicit:
            return explicit
        stored = st.get(key, {}).get("job", {}) if isinstance(st.get(key), dict) else {}
        try:
            return date.fromisoformat(str(stored.get("date") or ""))
        except Exception:
            return TODAY

    skip_titles = {
        "careers", "open positions", "explore opportunities", "contact us",
        "company", "services", "why join us", "our mission & vision",
        "job responsibilities", "responsibilities", "requirements",
        "qualifications", "benefits", "salary and benefits", "contact",
        "position details", "description", "job description",
        "about us", "bonus skills", "eeo statement", "experience",
        "pay range", "personal requirements", "salary",
        "we’re looking for", "we're looking for", "what you need",
        "what you’ll do", "what you'll do", "what’s in it for you",
        "what's in it for you", "why this role", "work schedule",
        "hard skills", "additional qualifications"
    }

    for market, page in markets:
        try:
            r = _req_raw("GET", page)
        except Exception as ex:
            print("MIDWEST_FAMILY_PAGE_ERROR:", market, type(ex).__name__, str(ex)[:200])
            continue

        final = str(getattr(r, "url", "") or page)
        soup = BeautifulSoup(r.text, "html.parser")
        candidates = []

        # First collect obvious job/card containers. Modern market sites wrap
        # each opening in article/entry/post/card blocks rather than placing all
        # description text as direct siblings of the title heading.
        for box in soup.select("article, .post, .entry, .job, .career, [class*='job-'], [class*='career-'], [class*='post-']"):
            txt = clean(box.get_text(" "))
            if len(txt) < 120 or not any(term in txt.lower() for term in (
                "apply", "resume", "employment", "full-time", "full time",
                "part-time", "part time", "responsibilit", "qualification",
                "salary", "compensation", "position",
            )):
                continue
            head = box.find(["h1","h2","h3","h4","h5"])
            title = clean(head.get_text(" ") if head else "")
            if not title or title.lower().rstrip(":") in skip_titles:
                # An apply/details anchor often carries the role when the card
                # has no useful heading.
                for a in box.find_all("a", href=True):
                    label = clean(a.get_text(" "))
                    if label and len(label) < 180 and not re.match(r"^(apply|learn more|details|read more)$", label, re.I):
                        title = label
                        break
            if title and title.lower().rstrip(":") not in skip_titles:
                candidates.append((title, box))

        # Fallback: use headings but climb to the nearest substantial parent
        # instead of relying on direct next_siblings.
        for head in soup.find_all(["h1","h2","h3","h4","h5"]):
            title = clean(head.get_text(" "))
            if not title or title.lower().rstrip(":") in skip_titles:
                continue
            box = head
            for _ in range(5):
                parent = getattr(box, "parent", None)
                if not parent:
                    break
                ptxt = clean(parent.get_text(" "))
                if 150 <= len(ptxt) <= 12000:
                    box = parent
                    if any(term in ptxt.lower() for term in (
                        "apply", "resume", "employment", "responsibilit",
                        "qualification", "salary", "position",
                    )):
                        break
                else:
                    break
            candidates.append((title, box))

        print("MIDWEST_FAMILY_CANDIDATES:", market, len(candidates))

        for title, box in candidates:
            title_key = title.lower().strip().rstrip(":")
            if title_key == "join the ownership class." and "account-executive" in str(box).lower():
                title = "Account Executive"
                title_key = "account executive"
            if (
                "@" in title
                or title_key in skip_titles
                or title_key.startswith("what’s in it for you")
                or title_key.startswith("what's in it for you")
            ):
                continue
            raw = str(box)
            body = clean(box.get_text(" "))
            if len(body) < 120:
                continue
            if not any(term in body.lower() for term in (
                "apply", "resume", "employment", "full-time", "full time",
                "part-time", "part time", "responsibilit", "qualification",
                "salary", "compensation", "position",
            )):
                continue

            apply_url = final
            for a in box.find_all("a", href=True):
                h = urljoin(final, a["href"]).split("#", 1)[0]
                label = clean(a.get_text(" ")).lower()
                if not h.startswith("http") or h.rstrip("/") == final.rstrip("/"):
                    continue
                if (
                    re.search(r"/(?:job|jobs|career|careers|apply|employment)/", h, re.I)
                    or h.lower().endswith(".pdf")
                    or any(term in label for term in ("apply", "job description", "details", "learn more"))
                ):
                    apply_url = h
                    break

            key = apply_url.rstrip("/").lower()
            if key == final.rstrip("/").lower():
                key += "#" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                apply_url = key
            if key in seen:
                continue

            pd = _direct_board_date(raw) or _direct_board_date(body[:2500])
            pd = stable_date(key, pd)
            jt = jobtype(title, body)
            probe = Job("", title, src["Company"], body, pd, jt, "", apply_url,
                        src["URL"], src["URL"], "", "", "", "")
            if not job_is_fresh(probe):
                continue

            locm = re.search(r"([A-Z][A-Za-z .'-]+),[ ]*([A-Z]{2})", body)
            market_match = re.match(r"(.+?),[ ]*([A-Z]{2})$", market)
            if locm:
                city, state = clean(locm.group(1)), locm.group(2)
            elif market_match:
                city, state = clean(market_match.group(1)), market_match.group(2)
            else:
                city, state = market, ""
            loc = (city + ", " + state).strip(", ")


            seen.add(key)
            cat = category(title, body, src["Industry"], src["Company"])
            # Mid-West Family is radio-first for programming/on-air/promotions.
            if any(term in (title + " " + body[:1200]).lower() for term in (
                "program director", "on-air", "on air", "air personality",
                "promotions", "producer", "board operator", "morning show",
            )):
                if not any(term in title.lower() for term in ("sales", "account executive", "marketing")):
                    cat = "Radio"

            desc = format_description(raw)
            if len(desc) > 12000:
                desc = "<p>" + body[:10000].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</p>"

            out.append(Job(
                hashlib.sha1(key.encode()).hexdigest()[:16], title, src["Company"],
                desc, pd, jt, cat,
                apply_url, src["URL"], src["URL"], "",
                normalize_work_arrangement(body, loc), city, state, "US",
            ))

    print("MIDWEST_FAMILY_PARSED:", len(out))
    return out


def saga_distributed_direct(src):
    """Collect Saga jobs only from a small first-party employment allowlist.

    Do not enumerate Saga's station directory during a crawl. Saga operates many
    independently hosted local sites, so probing arbitrary station domains is
    intentionally excluded from the production path. Known first-party career
    pages can be expanded after they are verified outside the crawler.
    """
    pages = [
        "https://kmit.com/jobs/",
        "https://wnax.com/employment-opportunities/",
        "https://1017chuckfm.com/jobs/",
        "https://pureoldies1069.com/careers/",
        "https://wixy.com/jobs/",
        "https://mix945.com/jobs/",
        "https://myez997.com/jobs/",
    ]
    deadline = time.monotonic() + 25.0
    out, seen = [], set()

    for url in pages:
        if time.monotonic() >= deadline:
            break
        rr = _radiofix_fast_get(url, timeout=3)
        if not rr:
            continue
        try:
            final = str(getattr(rr, "url", "") or url)
            j = _radio_direct_detail(src, final, rr.text)
            if j and j.id not in seen:
                seen.add(j.id)
                out.append(j)
        except Exception:
            continue
    return out

def radio_recovery(src):
    company=clean(src.get("Company","")).lower()
    if company not in RADIO_RECOVERY_COMPANIES:
        return []
    extras={
      "cumulus media":["https://jobs.cumulusmedia.com/careers"],
      "educational media foundation":["https://www.klove.com/about/careers"],
      "bell media":["https://jobs.bell.ca/ca/en/c/media-jobs"],
      "evanov":["https://evanov.ca/careers"],
      "lotus":["https://www.lotuscorp.com/category/careers/"],
      "midwest communications":["https://midwestcareers.com/","https://recruiting.paylocity.com/recruiting/jobs/All/0cb3a074-2113-4e9e-a32d-27e40c132e62/Midwest-Communications"],
      "pattison media":["https://www.pattisonmedia.com/careers"],
      "rogers sports & media":["https://jobs.rogers.com/go/Rogers-Sports-and-Media/8824500/"],
      "stingray":["https://jobs.stingray.com/career-opportunities/"],
      "townsquare media":["https://careers.townsquaremedia.com/job-openings"],
      "stephens media group":["https://cherryfm.com/smg-jobs/"],
      "pamal broadcasting":["https://www.pamal.com/jobs1/jobs/","https://www.pamal.com/jobs1/catamount-radio-jobs/"],
    }
    starts=list(dict.fromkeys([src["URL"]]+extras.get(company,[])))
    details=set()
    for page in starts:
        try: r=req("GET",page)
        except Exception: continue
        final=str(getattr(r,"url","") or page)
        soup=BeautifulSoup(r.text,"html.parser")
        host=urlparse(final).netloc.lower()
        for a in soup.find_all("a",href=True):
            href=urljoin(final,a["href"]).split("#",1)[0]
            label=clean(a.get_text(" ")).lower()
            hp=urlparse(href)
            same=hp.netloc.lower()==host
            ext=any(x in hp.netloc.lower() for x in ("paylocity.com","icims.com","myworkdayjobs.com","jobs.rogers.com","greenhouse.io","lever.co"))
            if not (same or ext): continue
            p=hp.path.lower()
            if (re.search(r"/(?:job|jobs|career|careers|job-openings?)/",p)
                or re.search(r"/job/\d+",p)
                or label in {"view details","learn more","apply","apply now"}
                or any(k in label for k in ("producer","reporter","anchor","host","announcer","account executive","sales","engineer","technician","director","manager","coordinator","assistant"))):
                if href.rstrip("/") not in {s.rstrip("/") for s in starts}: details.add(href)
    out=[]; seen=set()
    for url in sorted(details):
        try:
            rr=req("GET",url); final=str(getattr(rr,"url","") or url)
            j=_radio_recovery_job(src,final,rr.text)
            if j and j.id not in seen: seen.add(j.id); out.append(j)
        except Exception: continue
    return out


V23_RADIO_TARGETS = {
    "audacy",
    "townsquare media",
    "hubbard broadcasting",
    "midwest communications",
    "educational media foundation",
}

def _v23_detail_candidates(base_url, raw):
    soup = BeautifulSoup(raw, "html.parser")
    out = set()
    base_host = urlparse(base_url).netloc.lower()

    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#", 1)[0]
        label = clean(a.get_text(" ")).lower()
        p = urlparse(href)
        host = p.netloc.lower()
        path = p.path.lower()
        query = p.query.lower()

        known_job_host = any(x in host for x in (
            "icims.com", "greenhouse.io", "lever.co", "paylocity.com",
            "adp.com", "myworkdayjobs.com",
        ))
        job_path = (
            re.search(r"/jobs?/\d+", path)
            or "/job/" in path
            or "/jobs/" in path
            or "jobid=" in query
            or "jobid=" in href.lower()
            or "gh_jid=" in href.lower()
        )
        job_label = any(k in label for k in (
            "apply", "view job", "view details", "learn more",
            "producer", "reporter", "anchor", "host", "engineer",
            "account executive", "sales", "program director",
            "on-air", "personality", "technician", "coordinator",
        ))

        if (host == base_host or known_job_host) and (job_path or job_label):
            out.add(href)
    return out


def audacy_v23(src):
    """Enumerate Audacy iCIMS public search pages and detail pages."""
    starts = [
        src["URL"],
        "https://careers-audacy.icims.com/jobs/search?ss=1",
    ]
    details = set()
    seen = set()

    # iCIMS search pages can paginate by pr= page number.
    for page_num in range(1, 26):
        for root in starts[:1]:
            sep = "&" if "?" in root else "?"
            page = root if page_num == 1 else f"{root}{sep}pr={page_num}"
            if page in seen:
                continue
            seen.add(page)
            try:
                r = req("GET", page)
            except Exception:
                continue
            final = str(getattr(r, "url", "") or page)
            found = _v23_detail_candidates(final, r.text)
            # Also pull canonical iCIMS job paths from raw HTML/JSON.
            for m in re.finditer(
                r'https?://careers-audacy\.icims\.com/jobs/\d+/[^"\'<>\s]+',
                r.text, re.I
            ):
                found.add(m.group(0).replace("\\/", "/"))
            for m in re.finditer(r'["\'](/jobs/\d+/[^"\']+)["\']', r.text, re.I):
                found.add(urljoin(final, m.group(1)))
            if not found and page_num > 2:
                break
            details.update(found)

    out, ids = [], set()
    for url in sorted(details):
        try:
            rr = req("GET", url)
            final = str(getattr(rr, "url", "") or url)
            j = _radio_recovery_job(src, final, rr.text)
            if j and j.id not in ids:
                ids.add(j.id)
                out.append(j)
        except Exception:
            continue
    return out


def townsquare_v23(src):
    """Use Townsquare's public career pages and Greenhouse job IDs."""
    roots = [
        src["URL"],
        "https://careers.townsquaremedia.com/job-openings/",
    ]
    details = set()
    for root in roots:
        try:
            r = req("GET", root)
        except Exception:
            continue
        final = str(getattr(r, "url", "") or root)
        details.update(_v23_detail_candidates(final, r.text))
        # Their branded careers site exposes Greenhouse IDs as gh_jid.
        for m in re.finditer(r'gh_jid(?:=|%3D)(\d+)', r.text, re.I):
            jid = m.group(1)
            details.add(f"https://careers.townsquaremedia.com/job-openings/?gh_jid={jid}")
    out, ids = [], set()
    for url in sorted(details):
        try:
            rr = req("GET", url)
            j = _radio_recovery_job(src, str(getattr(rr, "url", "") or url), rr.text)
            if j and j.id not in ids:
                ids.add(j.id); out.append(j)
        except Exception:
            continue
    return out


def hubbard_adp_cx(src):
    """Recover Hubbard jobs from its ADP CX board.

    Hubbard's ADP CX listing is a client-rendered shell and its private browser
    list call is not stable enough to treat as a public crawler contract.
    Search engines and Hubbard's station/community career pages do, however,
    expose the canonical ADP reqId detail URLs. Seed those verified public
    requisition URLs, then let ADP's own detail pages/schema provide the job.
    The older rendered-board recovery remains as the automatic fallback.
    """
    out = []
    seen = set()

    # Current canonical Hubbard requisitions independently exposed by public
    # station/community job pages. These are only seeds; each ADP detail page
    # must still parse as a fresh JobPosting before entering the feed.
    seed_ids = {
        "5001222833906",  # Director - Rochester
        "5001217681406",  # Multiplatform News Producer - Albany
        "5001217673806",  # Media Sales Associate - Albany
        "5001211060606",  # Multimedia Journalist/Newscast Producer - Rochester
        "5001205181406",  # Reporter/Anchor - Albany
    }

    # Preserve any requisition IDs that ADP chooses to expose in the listing
    # shell/application state.
    try:
        landing = req("GET", src["URL"])
        raw = html.unescape(landing.text or "").replace("\\/", "/")
        for m in re.finditer(r'(?i)reqId(?:%3D|=|["\']?\\s*[:=]\\s*["\'])(500\\d{10})', raw):
            seed_ids.add(m.group(1))
        for m in re.finditer(r'(?i)["\'](?:jobId|requisitionId|reqId)["\']\\s*:\\s*["\']?(500\\d{10})', raw):
            seed_ids.add(m.group(1))
    except Exception:
        pass

    for jid in sorted(seed_ids):
        url = (
            "https://myjobs.adp.com/hubbardbroadcasting/cx/job-details"
            f"?reqId={jid}"
        )
        try:
            rr = req("GET", url)
            final = str(getattr(rr, "url", "") or url)
            j = _job_from_detail(src, final, rr.text)
            if not j:
                j = _radio_recovery_job(src, final, rr.text)
            if j and j.id not in seen:
                seen.add(j.id)
                out.append(j)
        except Exception:
            continue

    # Also retain the older public-board discovery path. It can automatically
    # pick up additional jobs whenever ADP exposes detail URLs in rendered HTML.
    for j in hubbard_v23(src):
        if j.id not in seen:
            seen.add(j.id)
            out.append(j)

    return out

def hubbard_v23(src):
    """Recover Hubbard's newer ADP CX job-detail links from the public board."""
    roots = [
        src["URL"],
        "https://myjobs.adp.com/hubbardbroadcasting/cx/job-listing",
    ]
    details = set()
    for root in roots:
        try:
            r = req("GET", root)
        except Exception:
            continue
        final = str(getattr(r, "url", "") or root)
        details.update(_v23_detail_candidates(final, r.text))
        # Capture ADP CX detail URLs and requisition IDs embedded in JS.
        for m in re.finditer(
            r'https?://myjobs\.adp\.com/hubbardbroadcasting/cx/job-details\?[^"\'<>\s]+',
            r.text, re.I
        ):
            details.add(m.group(0).replace("\\/", "/").replace("&amp;", "&"))
        for m in re.finditer(r'jobId["\']?\s*[:=]\s*["\']([^"\']+)["\']', r.text, re.I):
            jid = m.group(1)
            details.add(
                "https://myjobs.adp.com/hubbardbroadcasting/cx/job-details"
                f"?reqId={jid}"
            )
    out, ids = [], set()
    for url in sorted(details):
        try:
            rr = req("GET", url)
            j = _radio_recovery_job(src, str(getattr(rr, "url", "") or url), rr.text)
            if j and j.id not in ids:
                ids.add(j.id); out.append(j)
        except Exception:
            continue
    return out


def midwest_v23(src):
    """Enumerate Midwest Careers' JavaScript WP Job Manager board.

    The public homepage intentionally contains no server-rendered listings.
    WP Job Manager loads them from /jm-ajax/get_listings/; collect those
    returned detail URLs first, then retain the older Paylocity/direct-page
    discovery as a fallback.
    """
    roots = [
        "https://midwestcareers.com/",
        "https://recruiting.paylocity.com/recruiting/jobs/All/0cb3a074-2113-4e9e-a32d-27e40c132e62/Midwest-Communications",
    ]
    details = set()

    # Primary source: the AJAX endpoint used by the visible Midwest Careers
    # listings UI. Bound pagination so a site change cannot create a runaway.
    ajax = "https://midwestcareers.com/jm-ajax/get_listings/"
    for page_num in range(1, 11):
        try:
            r = req(
                "POST",
                ajax,
                data={
                    "action": "get_listings",
                    "search_keywords": "",
                    "search_location": "",
                    "search_categories[]": "",
                    "filter_job_type[]": "",
                    "per_page": "50",
                    "page": str(page_num),
                    "orderby": "featured",
                    "order": "DESC",
                    "show_pagination": "false",
                },
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://midwestcareers.com/",
                },
            )
            payload = r.json()
        except Exception:
            break

        raw = ""
        if isinstance(payload, dict):
            raw = str(payload.get("html") or payload.get("data") or "")
        elif isinstance(payload, str):
            raw = payload
        if not raw:
            break

        before = len(details)
        soup = BeautifulSoup(raw, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin("https://midwestcareers.com/", a["href"]).split("#", 1)[0]
            p = urlparse(href)
            if p.netloc.lower() not in {"midwestcareers.com", "www.midwestcareers.com"}:
                continue
            if re.search(r"/(?:job|jobs)/[^/?#]+/?$", p.path, re.I):
                details.add(href)

        # Also catch links serialized inside response fragments.
        for m in re.finditer(
            r'https?://(?:www\.)?midwestcareers\.com/(?:job|jobs)/[^"\'< >]+',
            raw,
            re.I,
        ):
            details.add(html.unescape(m.group(0)).rstrip("\\/,.;)"))

        # WP Job Manager normally reports whether another page exists.
        max_num_pages = 0
        if isinstance(payload, dict):
            try:
                max_num_pages = int(payload.get("max_num_pages") or 0)
            except Exception:
                max_num_pages = 0
        if (max_num_pages and page_num >= max_num_pages) or len(details) == before:
            break

    # Fallback discovery preserves compatibility if Midwest changes the AJAX
    # endpoint or temporarily exposes links directly in either public board.
    for root in roots:
        try:
            r = req("GET", root)
        except Exception:
            continue
        final = str(getattr(r, "url", "") or root)
        details.update(_v23_detail_candidates(final, r.text))
        for m in re.finditer(
            r'https?://recruiting\.paylocity\.com/recruiting/jobs/Details/\d+/[^"\'< >]+',
            r.text,
            re.I,
        ):
            details.add(m.group(0).replace("\\/", "/"))

    out, ids = [], set()
    for url in sorted(details):
        try:
            rr = req("GET", url)
            final = str(getattr(rr, "url", "") or url)
            j = _radio_recovery_job(src, final, rr.text)
            if not j:
                j = _job_from_detail(src, final, rr.text)
            if j and j.id not in ids:
                ids.add(j.id)
                out.append(j)
        except Exception:
            continue
    return out


def emf_v23(src):
    """Enumerate K-LOVE/EMF branded careers pages and any linked ATS detail URLs."""
    roots = [
        src["URL"],
        "https://www.klove.com/about/careers",
    ]
    details = set()
    for root in roots:
        try:
            r = req("GET", root)
        except Exception:
            continue
        final = str(getattr(r, "url", "") or root)
        details.update(_v23_detail_candidates(final, r.text))
        # Catch job URLs embedded in Next.js JSON.
        for m in re.finditer(
            r'https?://[^"\'<>\s]+(?:job|career)[^"\'<>\s]+',
            r.text, re.I
        ):
            u = m.group(0).replace("\\/", "/").replace("\\u0026", "&")
            if any(x in u.lower() for x in ("klove", "icims", "job", "career")):
                details.add(u)
    out, ids = [], set()
    for url in sorted(details):
        try:
            rr = req("GET", url)
            j = _radio_recovery_job(src, str(getattr(rr, "url", "") or url), rr.text)
            if j and j.id not in ids:
                ids.add(j.id); out.append(j)
        except Exception:
            continue
    return out


def radio_targeted_v23(src):
    company = clean(src.get("Company", "")).lower()
    if company == "audacy":
        return audacy_v23(src)
    if company == "townsquare media":
        return townsquare_v23(src)
    if company == "hubbard broadcasting":
        return hubbard_v23(src)
    if company == "midwest communications":
        return midwest_v23(src)
    if company == "educational media foundation":
        return emf_v23(src)
    return []


V25_FAST_RADIO_TARGETS = {
    "audacy",
    "educational media foundation",
}

def _v25_collect_job_links(base_url, raw, host_hint=None, max_links=80):
    """Extract likely job-detail links from HTML/JSON without brute-force probing."""
    soup = BeautifulSoup(raw, "html.parser")
    links = []
    seen = set()

    def add(u):
        if not u:
            return
        u = urljoin(base_url, u).replace("\\/", "/").replace("&amp;", "&")
        if u in seen:
            return
        if host_hint and host_hint not in urlparse(u).netloc.lower():
            return
        low = u.lower()
        if not (
            re.search(r"/jobs/\d+", low)
            or "/job-details" in low
            or "jobid=" in low
            or "gh_jid=" in low
        ):
            return
        seen.add(u)
        links.append(u)

    for a in soup.find_all("a", href=True):
        add(a.get("href"))

    for m in re.finditer(r'https?://[^"\'<>\s]+', raw, re.I):
        add(m.group(0))

    for m in re.finditer(r'["\'](/jobs/\d+/[^"\']+)["\']', raw, re.I):
        add(m.group(1))

    return links[:max_links]


def _v25_fetch_details(src, links, max_jobs=80):
    out, ids = [], set()
    for url in links[:max_jobs]:
        try:
            r = req("GET", url)
        except Exception:
            continue
        final = str(getattr(r, "url", "") or url)
        j = _radio_recovery_job(src, final, r.text)
        if j and j.id not in ids:
            ids.add(j.id)
            out.append(j)
    return out


def audacy_v25(src):
    """Discover Audacy iCIMS jobs from search/list pages only; no numeric ID scan."""
    root = "https://careers-audacy.icims.com/jobs/search?ss=1"
    all_links = []
    seen = set()

    for page_num in range(1, 9):
        page = root if page_num == 1 else f"{root}&pr={page_num}"
        try:
            r = req("GET", page)
        except Exception:
            continue
        final = str(getattr(r, "url", "") or page)
        links = _v25_collect_job_links(
            final, r.text, host_hint="careers-audacy.icims.com", max_links=100
        )
        new_links = [u for u in links if u not in seen]
        if not new_links and page_num > 2:
            break
        for u in new_links:
            seen.add(u)
            all_links.append(u)

    return _v25_fetch_details(src, all_links, max_jobs=120)


def emf_v25(src):
    """Preserve EMF success using listing discovery, not 200+ ID probes."""
    root = "https://careers-kloveair1.icims.com/jobs/search?ss=1"
    all_links = []
    seen = set()

    for page_num in range(1, 6):
        page = root if page_num == 1 else f"{root}&pr={page_num}"
        try:
            r = req("GET", page)
        except Exception:
            continue
        final = str(getattr(r, "url", "") or page)
        links = _v25_collect_job_links(
            final, r.text, host_hint="careers-kloveair1.icims.com", max_links=100
        )
        new_links = [u for u in links if u not in seen]
        if not new_links and page_num > 2:
            break
        for u in new_links:
            seen.add(u)
            all_links.append(u)

    return _v25_fetch_details(src, all_links, max_jobs=80)


def radio_direct_v25(src):
    company = clean(src.get("Company", "")).lower()
    if company == "audacy":
        return audacy_v25(src)
    if company == "educational media foundation":
        return emf_v25(src)
    return []


V26_EMF_NARROW_SCAN = True

def emf_v26_narrow(src):
    """
    Narrow fallback scan for EMF/K-LOVE only.
    Keeps v25 fast discovery first, then probes a small recent iCIMS ID window
    to recover jobs that are live but not enumerable from search pages.
    """
    out = emf_v25(src)
    if out:
        return out

    seen = set()
    recovered = []

    # Narrow range centered around the IDs that produced the 7 v24 jobs.
    # This is intentionally much smaller than v24's 2280-2480 scan.
    for jid in range(2310, 2361):
        url = f"https://careers-kloveair1.icims.com/jobs/{jid}/job?in_iframe=1"
        try:
            r = req("GET", url)
        except Exception:
            continue

        final = str(getattr(r, "url", "") or url)
        raw = r.text
        soup = BeautifulSoup(raw, "html.parser")
        txt = clean(soup.get_text(" "))
        low = txt.lower()

        if (
            len(txt) < 250
            or "job locations" not in low
            or not any(k in low for k in ("posted date", "job id", "overview"))
        ):
            continue

        j = _radio_recovery_job(src, final, raw)
        if j and j.id not in seen:
            seen.add(j.id)
            recovered.append(j)

    return recovered


V27_STRUCTURED_TEST_COMPANIES = {
    "gray television",
    "salem media group",
    "educational media foundation",
    "midwest communications",
    "associated press",
    "wall street journal",
    "woodward communications",
    "washington post",
    "newsmax",
}

def _v27_jsonld_objects(raw):
    """Yield JSON-LD objects from a page, flattening @graph and arrays."""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        payload = tag.string or tag.get_text()
        if not payload:
            continue
        payload = payload.strip()
        try:
            data = json.loads(payload)
        except Exception:
            # Some sites include multiple JSON values or stray control chars.
            try:
                payload2 = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", payload)
                data = json.loads(payload2)
            except Exception:
                continue

        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                graph = obj.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
                yield obj


def _v27_is_jobposting(obj):
    typ = obj.get("@type")
    if isinstance(typ, list):
        vals = [str(x).lower() for x in typ]
    else:
        vals = [str(typ).lower()]
    return "jobposting" in vals


def _v27_location_from_ld(obj):
    loc = obj.get("jobLocation")
    if not loc:
        return ""
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    if not isinstance(loc, dict):
        return clean(str(loc))
    addr = loc.get("address", loc)
    if not isinstance(addr, dict):
        return clean(str(addr))
    bits = [
        addr.get("addressLocality"),
        addr.get("addressRegion"),
        addr.get("postalCode"),
        addr.get("addressCountry"),
    ]
    return clean(", ".join(str(x) for x in bits if x))


def _v27_job_from_jsonld(src, page_url, obj):
    title = clean(str(obj.get("title") or obj.get("name") or ""))
    desc_html = obj.get("description") or ""
    if isinstance(desc_html, (dict, list)):
        desc_html = json.dumps(desc_html, ensure_ascii=False)
    desc = format_description(str(desc_html))
    if not title or len(desc) < 40:
        return None

    pd = pdate(obj.get("datePosted")) or TODAY
    if pd < CUTOFF:
        return None

    valid_through = pdate(obj.get("validThrough"))
    loc = _v27_location_from_ld(obj)

    employment = obj.get("employmentType") or ""
    if isinstance(employment, list):
        employment = ", ".join(str(x) for x in employment)
    employment = clean(str(employment))

    apply_url = clean(str(obj.get("url") or page_url))
    if apply_url.startswith("/"):
        apply_url = urljoin(page_url, apply_url)

    ident = obj.get("identifier") or {}
    jid = ""
    if isinstance(ident, dict):
        jid = clean(str(ident.get("value") or ident.get("name") or ""))
    elif ident:
        jid = clean(str(ident))
    if not jid:
        jid = hashlib.sha1(apply_url.encode()).hexdigest()[:16]

    return Job(
        jid,
        title,
        src["Company"],
        desc,
        pd,
        jobtype(title, employment),
        category(title, desc, src.get("Industry", ""), src["Company"]),
        apply_url,
        src["URL"],
        src["URL"],
        "",
        normalize_work_arrangement(desc, loc),
        loc,
        "",
        infer_country(loc, src["Company"], desc),
        valid_through,
    )

def _v27_discover_detail_links(base_url, raw, max_links=120):
    """Find likely job-detail URLs from ordinary HTML plus embedded JSON."""
    soup = BeautifulSoup(raw, "html.parser")
    out, seen = [], set()
    base_host = urlparse(base_url).netloc.lower()

    def add(href, label=""):
        if not href:
            return
        u = urljoin(base_url, href).replace("\\/", "/").replace("&amp;", "&").split("#", 1)[0]
        if u in seen:
            return
        p = urlparse(u)
        host = p.netloc.lower()
        low = u.lower()
        lab = clean(label).lower()

        jobish = (
            re.search(r"/jobs?/\d+", low)
            or "/job/" in low
            or "/careers/job" in low
            or "/job-details" in low
            or "gh_jid=" in low
            or "jobid=" in low
            or "reqid=" in low
            or "career" in low and any(k in lab for k in ("apply", "view", "details", "job"))
        )
        trusted_host = (
            host == base_host
            or any(x in host for x in (
                "icims.com", "greenhouse.io", "lever.co", "paylocity.com",
                "adp.com", "myworkdayjobs.com", "dayforcehcm.com",
                "successfactors.com", "oraclecloud.com"
            ))
        )
        if jobish and trusted_host:
            seen.add(u)
            out.append(u)

    for a in soup.find_all("a", href=True):
        add(a.get("href"), a.get_text(" "))

    for m in re.finditer(r'https?://[^"\'<>\s]+', raw, re.I):
        add(m.group(0), "")

    return out[:max_links]


def structured_jobs_v27(src):
    """
    JBoard-style fallback:
      listing/source URL -> discover likely job-detail URLs -> parse JSON-LD JobPosting.
    No ATS-specific field mapping required.
    """
    start = clean(src.get("URL", ""))
    if not start:
        return []

    pages = [start]
    seen_pages = set()
    detail_links = []
    jobs, ids = [], set()

    # Crawl only a few listing pages to stay bounded.
    for page in pages[:8]:
        if page in seen_pages:
            continue
        seen_pages.add(page)
        try:
            r = req("GET", page)
        except Exception:
            continue
        final = str(getattr(r, "url", "") or page)

        # If listing page itself contains JobPosting JSON-LD, ingest it.
        for obj in _v27_jsonld_objects(r.text):
            if _v27_is_jobposting(obj):
                j = _v27_job_from_jsonld(src, final, obj)
                if j and j.id not in ids:
                    ids.add(j.id)
                    jobs.append(j)

        detail_links.extend(_v27_discover_detail_links(final, r.text, max_links=120))

    # Follow only discovered likely detail pages.
    for url in detail_links[:120]:
        try:
            r = req("GET", url)
        except Exception:
            continue
        final = str(getattr(r, "url", "") or url)

        page_had_job = False
        for obj in _v27_jsonld_objects(r.text):
            if not _v27_is_jobposting(obj):
                continue
            page_had_job = True
            j = _v27_job_from_jsonld(src, final, obj)
            if j and j.id not in ids:
                ids.add(j.id)
                jobs.append(j)

        # If no JSON-LD is present, do not invent a job from generic page text here.
        # Older radio recovery logic remains available after this fallback.
        if not page_had_job:
            continue

    return jobs


# ============================================================
# v28 SAFE TEST / PRODUCTION CONTROLS
# ============================================================
MJR_TEST_COMPANIES = {
    clean(x).lower()
    for x in os.getenv("MJR_TEST_COMPANIES", "").split(",")
    if clean(x)
}
MJR_REQUEST_DELAY_MIN = float(os.getenv("MJR_REQUEST_DELAY_MIN", "0.20"))
MJR_REQUEST_DELAY_MAX = float(os.getenv("MJR_REQUEST_DELAY_MAX", "0.65"))
MJR_DOMAIN_REQUEST_CAP = int(os.getenv("MJR_DOMAIN_REQUEST_CAP", "175"))
_v28_domain_counts = {}

def _company_test_key(value):
    """Normalize company names used by targeted tests without changing feed labels."""
    key = clean(value).lower()
    key = re.sub(r"\s*/\s*", "/", key)
    aliases = {
        "disney/abc": "disney/abc",
        "disney / abc": "disney/abc",
        "abc": "disney/abc",
        "abc news": "disney/abc",
        "the walt disney company / abc": "disney/abc",
        "the walt disney company/abc": "disney/abc",
        "paramount": "paramount",
        "paramount global": "paramount",
        "paramount pictures": "paramount",
        "paramount skydance": "paramount",
        "paramount, a skydance corporation": "paramount",
        "fox": "fox",
        "fox corporation": "fox",
        "fox careers": "fox",
        "fox television stations": "fox",
        "fox tv stations": "fox",
        "fox entertainment": "fox",
        "fox news media": "fox",
        "townsquare": "townsquare",
        "townsquare media": "townsquare",
        "townsquare media, inc.": "townsquare",
        "townsquare media inc": "townsquare",
        "townsquare interactive": "townsquare",
        "townsquare ignite": "townsquare",
        "wsj/dow jones": "wsj/dow jones",
        "wall street journal / dow jones": "wsj/dow jones",
        "wall street journal/dow jones": "wsj/dow jones",
        "dow jones": "wsj/dow jones",
        "wall street journal": "wsj/dow jones",
    }
    # Future-proof Paramount source labels while keeping unrelated CBS rows
    # isolated unless they are explicitly part of the Paramount source row.
    if key.startswith("paramount ") or key.startswith("paramount,"):
        return "paramount"
    if key.startswith("townsquare ") or key.startswith("townsquare,"):
        return "townsquare"
    return aliases.get(key, key)

def _v28_source_enabled(src):
    if not MJR_TEST_COMPANIES:
        return True
    source_key = _company_test_key(src.get("Company", ""))
    test_keys = {_company_test_key(x) for x in MJR_TEST_COMPANIES}
    return source_key in test_keys

def _v28_before_request(url):
    host = urlparse(url).netloc.lower()
    count = _v28_domain_counts.get(host, 0)

    # Paramount has a very large active SuccessFactors board. Its listing is
    # pre-filtered by posting date before detail pages are requested, so allow
    # enough requests to complete the current 21-day set without weakening the
    # 175-request protection used by every other source.
    host_cap = MJR_DOMAIN_REQUEST_CAP
    if host in {"careers.paramount.com", "www.careers.paramount.com"}:
        host_cap = max(host_cap, int(os.getenv("MJR_PARAMOUNT_REQUEST_CAP", "300")))

    # NBCUniversal publishes a large current inventory through SmartRecruiters.
    # The collector first filters list summaries by date and US/Canada location,
    # then follows only qualifying detail refs. Give that verified, bounded
    # collector enough room to finish without weakening the global default.
    if host == "api.smartrecruiters.com":
        host_cap = max(host_cap, int(os.getenv("MJR_SMARTRECRUITERS_REQUEST_CAP", "350")))

    # Hearst Television and Hearst Newspapers use different Oracle sites on
    # the same hostname. The domain counter is shared, so the normal per-source
    # allowance can be exhausted by the first site before the second begins.
    if host == "eevd.fa.us6.oraclecloud.com":
        host_cap = max(host_cap, int(os.getenv("MJR_HEARST_ORACLE_REQUEST_CAP", "400")))

    if count >= host_cap:
        raise RuntimeError(f"v28 domain request cap reached for {host}: {host_cap}")
    _v28_domain_counts[host] = count + 1
    lo = max(0.0, MJR_REQUEST_DELAY_MIN)
    hi = max(lo, MJR_REQUEST_DELAY_MAX)
    if hi:
        time.sleep(random.uniform(lo, hi))

def _v28_backoff_seconds(attempt):
    return min(12.0, 1.5 * (2 ** attempt)) + random.uniform(0.0, 0.75)

def associated_press(src):
    """Read AP's public SuccessFactors board and JobPosting microdata."""
    base = "https://careers.ap.org/go/View-All-Jobs/4304700/"
    pages, seen_pages, detail_urls = [base], set(), set()
    while pages and len(seen_pages) < 8:
        page = pages.pop(0)
        if page in seen_pages:
            continue
        seen_pages.add(page)
        response = req("GET", page)
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.find_all("a", href=True):
            url = urljoin(base, anchor["href"]).split("#", 1)[0]
            parsed = urlparse(url)
            if parsed.netloc.lower() != "careers.ap.org":
                continue
            if re.fullmatch(r"/job/[^/]+/\d+/?", parsed.path):
                detail_urls.add(url.split("?", 1)[0])
            elif parsed.path.startswith("/go/View-All-Jobs/4304700/"):
                if url not in seen_pages:
                    pages.append(url)

    out = []
    for url in sorted(detail_urls)[:120]:
        response = req("GET", url)
        soup = BeautifulSoup(response.text, "html.parser")

        def field(name):
            node = soup.select_one(f'[itemprop="{name}"]')
            return clean(node.get("content") or node.get_text(" ", strip=True)) if node else ""

        posted = pdate(field("datePosted"))
        title = field("title")
        country = field("addressCountry").upper()
        node = soup.select_one('[itemprop="description"]')
        description = format_description(str(node)) if node else ""
        role_type = jobtype(title, strip_html(description))
        if (
            not posted or posted < CUTOFF
            or (TODAY - posted).days >= retention_days(role_type)
            or country not in {"US", "CA"}
            or not title or len(strip_html(description)) < 200
        ):
            continue
        identifier = re.search(r"/(\d+)/?$", urlparse(url).path).group(1)
        out.append(Job(
            identifier, title, src["Company"], description, posted, role_type,
            category(title, description, src.get("Industry") or "Journalism", src["Company"]),
            url, base, "https://www.ap.org/", "",
            normalize_work_arrangement(description, field("addressLocality")),
            field("addressLocality"), field("addressRegion"), country,
        ))
    return out


def generic(src):
    # Strict fallback: only individual pages with an explicit recent posted
    # date and a substantial description.
    r = req("GET", src["URL"])
    soup = BeautifulSoup(r.text, "html.parser")
    links = set()

    generic_patterns = [
        r"/jobs/", r"/job/", r"jobdetail", r"/details/",
        r"opportunitydetail", r"job-details", r"requisitions",
        r"career/JobIntroduction\.action", r"/apply/", r"/p/",
    ]

    for a in soup.find_all("a", href=True):
        h = urljoin(src["URL"], a["href"])
        if any(re.search(p, h, re.I) for p in generic_patterns) or _known_ats_url(h):
            links.add(h)

    # Generic company career pages frequently embed the real ATS URLs only in
    # JavaScript/application state. Pull those out too.
    links.update(_candidate_urls_from_text(src["URL"], r.text, generic_patterns))

    # JSON-LD canonical URLs are another reliable discovery path.
    for jp in _jsonld_jobs(soup):
        h = clean(jp.get("url") or jp.get("sameAs") or "")
        if h:
            links.add(urljoin(src["URL"], h))

    out = []

    for url in list(links)[:1000]:
        try:
            rr = req("GET", url)
            ss = BeautifulSoup(rr.text, "html.parser")
            txt = clean(ss.get_text(" "))

            h1 = ss.find("h1")
            title = clean(
                h1.get_text(" ")
                if h1
                else (ss.title.get_text(" ") if ss.title else "")
            )

            m = re.search(
                r"(?:posted|date posted|posted date)\s*:?\s*"
                r"([A-Za-z]+\s+\d{1,2},\s+20\d{2}|"
                r"\d{1,2}/\d{1,2}/20\d{2}|"
                r"\d+\s+days?\s+ago)",
                txt,
                re.I,
            )

            pd = pdate(m.group(1)) if m else None
            if not pd or pd < CUTOFF:
                continue

            main = ss.find("main") or ss.find("article") or ss
            desc = clean(main.get_text(" "))

            if len(desc) < 250:
                continue

            out.append(
                Job(
                    hashlib.sha1(url.encode()).hexdigest()[:16],
                    title,
                    src["Company"],
                    desc,
                    pd,
                    jobtype(title, txt),
                    category(
                        title,
                        desc,
                        src["Industry"],
                        src["Company"],
                    ),
                    url,
                    src["URL"],
                    src["URL"],
                    "",
                    normalize_work_arrangement(desc, txt),
                    "",
                    "",
                    infer_country(txt, src["Company"], desc),
                )
            )

        except Exception:
            pass

    return out


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def stateful(jobs):
    st = load_state()
    now = {j.url.rstrip("/").lower(): j for j in jobs}
    ret = list(jobs)

    for k, j in now.items():
        st[k] = {
            "misses": 0,
            "last_seen": TODAY.isoformat(),
            "job": {
                x: (
                    getattr(j, x).isoformat()
                    if isinstance(getattr(j, x), date)
                    else getattr(j, x)
                )
                for x in j.__dataclass_fields__
            },
        }

    for k, r in list(st.items()):
        if k in now:
            continue

        # Purge previously retained conglomerate jobs as soon as a new scope
        # filter identifies them. Do not let the normal one-run miss grace
        # period reintroduce rejected Disney/ESPN or Meruelo positions.
        stored_job = r.get("job", {})
        if company_scope_rejection_reason(stored_job):
            del st[k]
            continue

        r["misses"] = int(r.get("misses", 0)) + 1

        if r["misses"] >= 2:
            del st[k]
            continue

        x = stored_job

        try:
            pd = date.fromisoformat(x["date"])
        except Exception:
            del st[k]
            continue

        life = retention_days(x.get("jobtype"))

        if (TODAY - pd).days >= life:
            del st[k]
            continue

        dl = (
            date.fromisoformat(x["employer_deadline"])
            if x.get("employer_deadline")
            else None
        )

        x["date"] = pd
        x["employer_deadline"] = dl

        try:
            # Recalculate category for retained jobs too, so a classifier
            # improvement takes effect immediately instead of waiting for
            # the state entry to disappear.
            x["category"] = category(
                x.get("title", ""),
                x.get("description", ""),
                "",
                x.get("company", ""),
            )
            # v73: employment-type parser improvements also apply immediately
            # to jobs retained from state.
            x["jobtype"] = jobtype(
                x.get("title", ""),
                x.get("description", ""),
            )
            x["country"] = infer_country(
                x.get("city", ""),
                x.get("company", ""),
                x.get("description", ""),
            )
            # v81: recalculate retained jobs too. Older state entries may carry
            # a false Remote/Hybrid value from an earlier classifier.
            retained_location = ", ".join(
                part for part in [x.get("city", ""), x.get("state", "")] if part
            )
            x["work_arrangement"] = normalize_work_arrangement(
                x.get("description", ""),
                retained_location,
                x.get("title", ""),
            )
            ret.append(Job(**x))
        except Exception:
            pass

    STATE_FILE.write_text(json.dumps(st, indent=2, default=str))
    return ret


def _recover_missing_location(j):
    """Recover only clearly labelled city/state locations; never guess."""
    if clean(j.city):
        return False

    text = strip_html(j.description)
    us_codes = {
        "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
        "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
        "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
        "TX","UT","VT","VA","WA","WV","WI","WY","DC",
    }
    ca_codes = {"AB","BC","MB","NB","NL","NS","NT","NU","ON","PE","QC","SK","YT"}
    region_names = {
        "alabama":"AL", "alaska":"AK", "arizona":"AZ", "arkansas":"AR",
        "california":"CA", "colorado":"CO", "connecticut":"CT", "delaware":"DE",
        "florida":"FL", "georgia":"GA", "hawaii":"HI", "idaho":"ID",
        "illinois":"IL", "indiana":"IN", "iowa":"IA", "kansas":"KS",
        "kentucky":"KY", "louisiana":"LA", "maine":"ME", "maryland":"MD",
        "massachusetts":"MA", "michigan":"MI", "minnesota":"MN", "mississippi":"MS",
        "missouri":"MO", "montana":"MT", "nebraska":"NE", "nevada":"NV",
        "new hampshire":"NH", "new jersey":"NJ", "new mexico":"NM", "new york":"NY",
        "north carolina":"NC", "north dakota":"ND", "ohio":"OH", "oklahoma":"OK",
        "oregon":"OR", "pennsylvania":"PA", "rhode island":"RI", "south carolina":"SC",
        "south dakota":"SD", "tennessee":"TN", "texas":"TX", "utah":"UT",
        "vermont":"VT", "virginia":"VA", "washington":"WA", "west virginia":"WV",
        "wisconsin":"WI", "wyoming":"WY", "district of columbia":"DC",
        "alberta":"AB", "british columbia":"BC", "manitoba":"MB", "new brunswick":"NB",
        "newfoundland and labrador":"NL", "nova scotia":"NS", "ontario":"ON",
        "prince edward island":"PE", "quebec":"QC", "saskatchewan":"SK", "yukon":"YT",
    }

    def assign(city, region=""):
        city = clean(city).strip(" -–—,.")
        region = clean(region).strip(" ()").upper()
        if not city or len(city) > 60:
            return False
        if region and region not in us_codes | ca_codes:
            mapped = region_names.get(region.lower())
            if not mapped:
                return False
            region = mapped
        j.city = city
        j.state = region
        j.country = "CA" if region in ca_codes or "canada" in city.lower() else "US"
        return True

    # Stingray exposes a clean Department/Location header without punctuation.
    if clean(j.company).lower() == "stingray":
        m = re.search(
            r"\bLocation\s+(.{2,60}?)(?=\s+(?:At Stingray|Stingray is)\b)",
            text,
            re.I,
        )
        if m:
            value = clean(m.group(1))
            mm = re.match(r"(.+?)\s*\(([A-Z]{2})\)$", value)
            if mm and assign(mm.group(1), mm.group(2)):
                return True
            if value.lower() in {"montreal", "montréal", "western canada"}:
                if assign(value, "QC" if "montreal" in value.lower() else ""):
                    j.country = "CA"
                    return True
            if value.lower() == "new york" and assign(value, "NY"):
                return True

    candidates = [clean(j.title), text[:3200]]
    patterns = [
        r"(?:Job\s+)?Location(?:\(s\))?\s*[:\-]\s*"
        r"([A-Z][A-Za-zÀ-ÿ .'-]{1,60}),\s*([A-Z]{2})(?:\b|,)",
        r"(?:Job\s+)?Location(?:\(s\))?\s*[:\-]\s*"
        r"([A-Z][A-Za-zÀ-ÿ .'-]{1,60}),\s*([A-Z][A-Za-z ]{3,30}?)"
        r"(?=\s+(?:Position|Department|Job Type|Employment|Responsibilities)\b|$)",
        r"(?:based|located|work(?:ing)?|position)\s+in\s+"
        r"([A-Z][A-Za-zÀ-ÿ .'-]{1,60}),\s*([A-Z]{2})\b",
        r"[-–—]\s*([A-Z][A-Za-zÀ-ÿ .'-]{1,60}),?\s+([A-Z]{2})\s*$",
        r"[-–—]\s*([A-Z][A-Za-zÀ-ÿ .'-]{1,60}),\s*([A-Z][A-Za-z ]{3,30})\s*$",
        r"\|\s*([A-Z][A-Za-zÀ-ÿ .'-]{1,60}),\s*([A-Z][A-Za-z ]{3,30}?)"
        r"(?=\s+(?:Part[- ]Time|Full[- ]Time|Temporary|Contract)\b)",
    ]
    for source in candidates:
        for pat in patterns:
            m = re.search(pat, source, re.I)
            if not m:
                continue
            if assign(m.group(1), m.group(2)):
                return True
    return False


def _canonical_requisition_key(j):
    """Collapse alternate slugs that point to the same underlying requisition."""
    company = clean(j.company).lower()
    url = clean(j.url).rstrip("/")
    if company in {"espn", "disney / abc"}:
        m = re.search(r"/(\d{8,})(?:[/?#]|$)", url)
        if m:
            return f"disney:{m.group(1)}"
    return f"url:{url.lower()}"


def _clean_lotus_description(title, description):
    """Keep the Lotus/OneCMS article while removing duplicated site chrome."""
    soup = BeautifulSoup(description or "", "html.parser")
    wanted = clean(title).lower()
    start = None
    for node in soup.find_all(["h1", "h2", "h3"]):
        label = clean(node.get_text(" ")).lower()
        if label == wanted or (wanted and wanted in label and label != "blog"):
            start = node
            break
    if start is None:
        return description

    pieces = []
    for node in [start, *list(start.next_siblings)]:
        label = clean(node.get_text(" ") if hasattr(node, "get_text") else str(node)).lower()
        if any(marker in label for marker in (
            "terms of use privacy policy fcc applications",
            "powered by onecms",
            "served by intertech media",
        )):
            break
        pieces.append(str(node))
    cleaned = format_description("".join(pieces))
    return cleaned if len(strip_html(cleaned)) >= 200 else description


def finalize_jobs(jobs):
    """Apply feed-wide corrections after fresh and retained jobs are combined."""
    recovered = set()
    arrangement_corrections = []
    for j in jobs:
        j.description = format_description(j.description)
        if clean(j.company).lower() == "lotus":
            j.description = _clean_lotus_description(j.title, j.description)
        if _recover_missing_location(j):
            recovered.add(j.url)
        location = ", ".join(x for x in [j.city, j.state, j.country] if clean(x))
        old_arrangement = j.work_arrangement
        j.work_arrangement = normalize_work_arrangement(
            j.description,
            location,
            j.title,
            j.work_arrangement,
        )
        if j.work_arrangement != old_arrangement:
            arrangement_corrections.append((j, old_arrangement, j.work_arrangement))

    chosen = {}
    duplicate_warnings = []
    for j in jobs:
        key = _canonical_requisition_key(j)
        old = chosen.get(key)
        if old is None:
            chosen[key] = j
            continue
        # Prefer the clearer/longer source title when one requisition appears
        # under multiple Disney/ESPN slugs.
        keep, drop = (j, old) if len(j.title) > len(old.title) else (old, j)
        chosen[key] = keep
        duplicate_warnings.append((drop, keep, key))

    kept_urls = {j.url for j in chosen.values()}
    arrangement_corrections = [
        item for item in arrangement_corrections if item[0].url in kept_urls
    ]
    return list(chosen.values()), recovered, duplicate_warnings, arrangement_corrections


def write_quality_report(
    jobs,
    recovered_locations,
    duplicate_warnings,
    arrangement_corrections,
):
    rows = []

    def add(kind, severity, j, detail):
        rows.append([
            kind,
            severity,
            j.company,
            j.title,
            j.id,
            j.work_arrangement,
            j.city,
            j.state,
            j.url,
            detail,
        ])

    for dropped, kept, key in duplicate_warnings:
        add(
            "duplicate_requisition_removed",
            "info",
            dropped,
            f"Kept '{kept.title}' using key {key}",
        )

    for j, old, new in arrangement_corrections:
        add(
            "work_arrangement_corrected",
            "info",
            j,
            f"Changed from {old} to {new} using explicit job-level wording",
        )

    generic_paths = {
        "", "/", "/jobs", "/jobs/", "/careers", "/careers/",
        "/job-openings", "/job-openings/",
    }
    for j in jobs:
        desc = j.description or ""
        plain = strip_html(desc)
        structural = len(re.findall(r"<(?:p|li|ul|ol|h[2-5]|br)\b", desc, re.I))
        if not clean(j.city):
            add("missing_location", "warning", j, "No reliable city was found")
        elif j.url in recovered_locations:
            add("location_recovered", "info", j, "Recovered from a labelled title/description location")
        if len(plain) > 700 and structural < 2:
            add("block_description", "warning", j, f"Only {structural} structural HTML elements")
        parsed = urlparse(j.url)
        if parsed.path.lower() in generic_paths and not parsed.query:
            add("generic_apply_link", "warning", j, "URL appears to be a general jobs/careers page")
        if not clean(j.url):
            add("missing_apply_link", "error", j, "No application URL")

        title_low = clean(j.title).lower()
        if j.jobtype == "Internship" and j.category != "Internships":
            add("category_conflict", "warning", j, "Internship job is not in Internships")
        elif re.search(r"\bmaster control\b", title_low) and j.category != "Television":
            add("category_conflict", "warning", j, "Master Control should be Television")
        elif (
            (
                re.search(r"\b(?:software|infrastructure|cybersecurity|developer|engineer)\b", title_low)
                or re.search(
                    r"\b(?:systems?|network)\s+(?:engineer|administrator|analyst|developer|specialist)\b",
                    title_low,
                )
            )
            and not re.search(r"\b(?:sales|account executive)\b", title_low)
            and not re.search(r"\b(?:facilities systems?|space planner)\b", title_low)
            and j.category != "Internships"
            and j.category != "Engineering"
        ):
            add("category_conflict", "warning", j, "Technical title may belong in Engineering")
        elif (
            re.search(r"\b(?:account executive|sales manager|director of sales|sales director)\b", title_low)
            and j.jobtype != "Internship"
            and j.category != "Sales & Marketing"
        ):
            add("category_conflict", "warning", j, "Sales title may belong in Sales & Marketing")

    rows.sort(key=lambda x: (x[1], x[0], x[2].lower(), x[3].lower()))
    with QUALITY_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "issue", "severity", "company", "title", "job_id",
            "work_arrangement", "city", "state", "url", "detail",
        ])
        w.writerows(rows)

    counts = {}
    for row in rows:
        counts[row[0]] = counts.get(row[0], 0) + 1
    print(f"Quality report: {len(rows)} flags -> {QUALITY_FILE}")
    for kind, count in sorted(counts.items()):
        print(f"  {kind}: {count}")


def write_xml(jobs):
    root = ET.Element("jobs")

    for j in sorted(
        jobs,
        key=lambda x: (
            x.company.lower(),
            x.title.lower(),
            x.city.lower(),
        ),
    ):
        e = ET.SubElement(root, "job")

        description = j.description
        if j.source == CAREERONESTOP_SOURCE:
            description += (
                '<hr><p><a href="https://www.careeronestop.org/">'
                f'<img src="{CAREERONESTOP_LOGO}" alt="CareerOneStop"></a></p>'
                '<p>Job data provided by CareerOneStop, sponsored by the '
                'U.S. Department of Labor Employment and Training Administration '
                'and produced by the Minnesota Department of Employment and '
                'Economic Development.</p>'
            )

        vals = [
            ("id", j.id),
            ("title", j.title),
            ("company", j.company),
            ("description", description),
            ("date", j.date.isoformat()),
            ("expiration", str(j.expiration)),
            ("jobtype", j.jobtype),
            ("category", j.category),
            ("url", j.url),
            ("source", j.source),
            ("url_verified", TODAY.isoformat()),
            ("company_website", j.company_website),
            ("logo", j.logo),
            # The existing JBoard importer maps its boolean Remote field to
            # work_arrangement. It treats any non-empty XML value as remote,
            # so only genuinely remote jobs may contain a value here.
            ("work_arrangement", "true" if j.work_arrangement == "Remote" else ""),
            # Preserve MJR's three-way classification for audits, future
            # integrations, and any importer that supports Hybrid explicitly.
            ("arrangement_type", j.work_arrangement),
            # Also expose a clearly named boolean field for new importers.
            ("remote", "true" if j.work_arrangement == "Remote" else ""),
        ]

        for t, v in vals:
            ET.SubElement(e, t).text = str(v or "")

        l = ET.SubElement(e, "location")

        for t, v in [
            ("city", j.city),
            ("state", j.state),
            ("country", j.country),
        ]:
            ET.SubElement(l, t).text = v or ""

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(
        OUTFILE,
        encoding="utf-8",
        xml_declaration=True,
    )



# ============================================================
# v29 SALEM iCIMS TARGETED ENUMERATION
# ============================================================

def salem_icims_v29(src):
    """Bounded Salem-specific iCIMS collector.

    Salem's public detail pages are server-rendered, but the default search
    URL does not consistently expose links to requests-based crawlers. Try
    several supported public portal render variants, collect only real
    /jobs/<id>/<slug>/job URLs, then parse those detail pages normally.
    No requisition-ID brute force is used.
    """
    parsed = urlparse(src["URL"])
    base = f"{parsed.scheme}://{parsed.netloc}"

    variants = [
        base + "/jobs/search?ss=1&searchRelation=keyword_all&in_iframe=1",
        base + "/jobs/search?ss=1&searchRelation=keyword_all&mobile=true&needsRedirect=false",
        base + "/jobs/search?ss=1&searchRelation=keyword_all",
        base + "/jobs/search?ss=1&in_iframe=1",
    ]

    queue = list(dict.fromkeys(variants))
    seen_pages = set()
    details = set()

    while queue and len(seen_pages) < 20 and len(details) < 250:
        page = queue.pop(0)
        if page in seen_pages:
            continue
        seen_pages.add(page)
        try:
            r = req("GET", page)
        except Exception:
            continue

        final = str(getattr(r, "url", "") or page)
        host = urlparse(final).netloc.lower()
        raw = html.unescape(r.text or "").replace("\\/", "/")
        soup = BeautifulSoup(raw, "html.parser")

        def add_detail(href):
            if not href:
                return
            u = urljoin(final, href)
            up = urlparse(u)
            if up.netloc.lower() != host:
                return
            if re.search(r"/jobs/\d+/(?:[^/?#]+/)?job(?:[/?#]|$)", u, re.I):
                # Use iframe-rendered detail page because Salem exposes the full
                # job content there, while the wrapper can be mostly noscript.
                q = parse_qs(up.query)
                q["in_iframe"] = ["1"]
                newq = urlencode({k: v[-1] for k, v in q.items()})
                u = urlunparse((up.scheme, up.netloc, up.path, "", newq, ""))
                details.add(u)

        for a in soup.find_all("a", href=True):
            h = a.get("href")
            add_detail(h)
            u = urljoin(final, h)
            up = urlparse(u)
            if up.netloc.lower() != host:
                continue
            label = clean(a.get_text(" ")).lower()
            if "/jobs/search" in up.path.lower() and (
                re.search(r"[?&](pr|page)=\d+", u, re.I)
                or label in {"next", "next page", ">", "»"}
            ):
                if u not in seen_pages and u not in queue:
                    queue.append(u)

        # iCIMS often serializes portal URLs inside script/config objects.
        patterns = [
            r'https?://[^"\'<>\s]+/jobs/\d+/[^"\'<>\s]+/job[^"\'<>\s]*',
            r'/jobs/\d+/[^"\'<>\s]+/job[^"\'<>\s]*',
        ]
        for pat in patterns:
            for m in re.finditer(pat, raw, re.I):
                add_detail(m.group(0).rstrip(".,);"))

    out, seen_ids = [], set()
    for url in sorted(details):
        try:
            rr = req("GET", url)
        except Exception:
            continue
        final = str(getattr(rr, "url", "") or url)
        j = _job_from_detail(src, final, rr.text)
        if not j:
            j = _direct_board_job(src, final, rr.text)
        if not j:
            continue

        # v73 Salem: iCIMS often omits a trustworthy posting date even while
        # the requisition is live. Use the employer date when available;
        # otherwise preserve MJR's original discovery date from state. A newly
        # discovered live job starts its 21-day MJR window today.
        pd = _v18_icims_date(rr.text)
        if not pd:
            try:
                st = load_state()
                key = (getattr(j, "url", "") or final).rstrip("/").lower()
                rec = st.get(key, {})
                saved = (rec.get("job") or {}).get("date")
                if saved:
                    pd = date.fromisoformat(saved)
            except Exception:
                pd = None
        if not pd:
            pd = TODAY
        if pd < CUTOFF:
            continue
        j.date = pd

        if j.id not in seen_ids:
            seen_ids.add(j.id)
            out.append(j)

    return out


# ============================================================
# v30 SALEM RENDERED-BROWSER FALLBACK
# ============================================================



def _icims_effective_source(src):
    """
    Normalize known branded career pages to their underlying iCIMS host.
    """
    company_key = clean(src.get("Company", "")).lower()
    url = src.get("URL", "")

    if company_key == "educational media foundation":
        fixed = dict(src)
        fixed["URL"] = "https://careers-kloveair1.icims.com/jobs/search?ss=1"
        fixed["ATS"] = "iCIMS"
        return fixed

    return src


def _icims_frame_detail_urls(src, max_details=160):
    """
    Generic rendered iCIMS listing discovery.
    Modern iCIMS portals often put actual search results inside an
    in_iframe=1 frame. This follows only IDs/URLs actually present there.
    """
    if sync_playwright is None:
        return []

    src = _icims_effective_source(src)
    parsed = urlparse(src["URL"])
    base = f"{parsed.scheme}://{parsed.netloc}"
    start_url = src["URL"] if "/jobs/search" in parsed.path.lower() else base + "/jobs/search?ss=1"
    detail_urls = set()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent=SESSION.headers.get(
                    "User-Agent",
                    "MJR-Jobs-Feed/1.0 (+https://www.mediajobsreport.com)",
                ),
                viewport={"width": 1440, "height": 1100},
            )
            page = context.new_page()
            page.set_default_timeout(20000)

            _v28_before_request(start_url)
            page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(1800)

            frames = []
            for frame in page.frames:
                fu = frame.url or ""
                up = urlparse(fu)
                if up.netloc.lower() == parsed.netloc.lower() and "/jobs/search" in up.path.lower():
                    frames.append(frame)

            frames.sort(key=lambda f: ("in_iframe=1" not in (f.url or ""), f.url or ""))

            for frame in frames:
                try:
                    hrefs = frame.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.href)"
                    )
                except Exception:
                    hrefs = []

                for href in hrefs:
                    if not href:
                        continue
                    up = urlparse(href)
                    if up.netloc.lower() != parsed.netloc.lower():
                        continue
                    if re.search(r"/jobs/\d+/(?:[^/?#]+/)?job(?:[/?#]|$)", href, re.I):
                        q = parse_qs(up.query)
                        q["in_iframe"] = ["1"]
                        nq = urlencode({k: v[-1] for k, v in q.items()})
                        detail_urls.add(
                            urlunparse((up.scheme, up.netloc, up.path, "", nq, ""))
                        )

                try:
                    html = frame.content()
                except Exception:
                    html = ""

                # Direct URLs embedded in markup/scripts.
                for m in re.finditer(
                    r"(?:https?://[^\"'<> ]+)?(/jobs/(\d+)/(?:[^\"'<>/?# ]+/)?job(?:\?[^\"'<> ]*)?)",
                    html,
                    re.I,
                ):
                    href = urljoin(frame.url, m.group(1).replace("&amp;", "&"))
                    up = urlparse(href)
                    if up.netloc.lower() != parsed.netloc.lower():
                        continue
                    q = parse_qs(up.query)
                    q["in_iframe"] = ["1"]
                    nq = urlencode({k: v[-1] for k, v in q.items()})
                    detail_urls.add(
                        urlunparse((up.scheme, up.netloc, up.path, "", nq, ""))
                    )

                # Explicit job IDs present in the rendered results data.
                ids = set(re.findall(r'"jobId"\s*:\s*"?(\d+)"?', html, re.I))
                ids.update(re.findall(r"/jobs/(\d+)/", html, re.I))
                for job_id in ids:
                    detail_urls.add(f"{base}/jobs/{job_id}/job?in_iframe=1")

            browser.close()

    except Exception as e:
        print(f"Generic iCIMS rendered discovery failed for {src['Company']}: {e}")

    return sorted(detail_urls)[:max_details]


def _icims_canonical_apply_url(detail_url, html):
    """
    Return the job-specific Audacy/iCIMS application URL when one is present.
    v73 prefers the actual "Apply for this job online" URL over the wrapper
    detail page so JBoard does not send applicants to a generic iCIMS page.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    host = urlparse(detail_url).netloc.lower()
    m = re.search(r"/jobs/(\d+)/", detail_url, re.I)
    job_id = m.group(1) if m else None

    # Strongest candidate: an explicit apply link for this exact requisition.
    if job_id:
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            label = clean(a.get_text(" ", strip=True)).lower()
            u = urljoin(detail_url, href)
            p = urlparse(u)
            if p.netloc.lower() != host:
                continue
            if not re.search(rf"/jobs/{re.escape(job_id)}/", u, re.I):
                continue
            q = parse_qs(p.query)
            is_apply = (
                q.get("apply", [""])[-1].lower() == "yes"
                or "apply for this job" in label
                or re.search(r"\bapply\b", label)
            )
            if is_apply:
                return u

    # Fallback: exact job detail URL, never a search/intro page.
    candidates = []
    can = soup.find("link", rel=lambda x: x and "canonical" in str(x).lower())
    if can and can.get("href"):
        candidates.append(can.get("href"))
    og = soup.find("meta", attrs={"property": "og:url"})
    if og and og.get("content"):
        candidates.append(og.get("content"))
    candidates.append(detail_url)

    for c in candidates:
        if not c:
            continue
        u = urljoin(detail_url, c)
        p = urlparse(u)
        if p.netloc.lower() != host:
            continue
        if not re.search(r"/jobs/\d+/(?:[^/?#]+/)?job(?:[/?#]|$)", u, re.I):
            continue
        q = parse_qs(p.query)
        q.pop("in_iframe", None)
        nq = urlencode({k: v[-1] for k, v in q.items()})
        return urlunparse((p.scheme, p.netloc, p.path, "", nq, ""))

    return detail_url



def _audacy_direct_icims_urls_v40(src, max_pages=12, max_details=180):
    """
    v44 Audacy enumeration.

    Audacy's search HTML does NOT expose posting dates. It exposes individual
    job links plus requisition IDs such as 2026-8337. Preserve the listing
    order and let the detail-page validator read the actual posting date.
    """
    base = "https://careers-audacy.icims.com"
    ordered = []
    seen = set()
    previous_signature = None

    for page_num in range(max_pages):
        url = (
            f"{base}/jobs/search?"
            f"ss=1&searchRelation=keyword_all&pr={page_num}&in_iframe=1"
        )

        try:
            rr = req("GET", url)
        except Exception as e:
            print(f"Audacy v66 search fetch failed page {page_num}: {e}")
            break

        html = rr.text or ""
        final_url = str(getattr(rr, "url", "") or url)
        soup = BeautifulSoup(html, "html.parser")

        page_urls = []
        page_ids = []

        # Preserve DOM/listing order.
        for a_tag in soup.find_all("a", href=True):
            href = urljoin(final_url, a_tag.get("href") or "")
            m = re.search(r"/jobs/(\d+)/", href, re.I)
            if not m:
                continue
            job_id = m.group(1)
            if job_id in page_ids:
                continue

            up = urlparse(href)
            if up.netloc.lower() != "careers-audacy.icims.com":
                continue

            q = parse_qs(up.query)
            q["in_iframe"] = ["1"]
            nq = urlencode({k: v[-1] for k, v in q.items()})
            normalized = urlunparse(
                (up.scheme, up.netloc, up.path, "", nq, "")
            )
            page_ids.append(job_id)
            page_urls.append(normalized)

        # Fallback if anchors are absent.
        if not page_urls:
            for m in re.finditer(r"/jobs/(\d+)/", html, re.I):
                job_id = m.group(1)
                if job_id in page_ids:
                    continue
                page_ids.append(job_id)
                page_urls.append(
                    f"{base}/jobs/{job_id}/job?in_iframe=1"
                )

        signature = tuple(page_ids)
        print(
            f"Audacy v66 listing page {page_num}: "
            f"{len(page_ids)} ordered job IDs"
        )

        if not page_ids:
            break
        if signature == previous_signature:
            break
        previous_signature = signature

        for job_id, job_url in zip(page_ids, page_urls):
            if job_id in seen:
                continue
            seen.add(job_id)
            ordered.append(job_url)

        if len(ordered) >= max_details:
            break

    print(f"Audacy v66 enumerated {len(ordered)} ordered jobs")
    return ordered[:max_details], len(ordered)


def collect_audacy_v40(src):
    """
    v47 Audacy importer with guaranteed validation logging.

    Collection logic is intentionally unchanged from v46. The only material
    addition is a persistent per-job decision log explaining exactly why each
    enumerated detail page was accepted or rejected.
    """
    urls, enumerated_count = _audacy_direct_icims_urls_v40(src)
    log_lines = [
        "job_id\tdecision\treason\ttitle\tdate\tapply_url\tfinal_url"
    ]

    def log(job_id, decision, reason="", title="", pd="", apply_url="", final_url=""):
        line = "\t".join([
            str(job_id or ""),
            str(decision or ""),
            str(reason or "").replace("\t", " ").replace("\n", " "),
            str(title or "").replace("\t", " ").replace("\n", " "),
            str(pd or ""),
            str(apply_url or "").replace("\t", " "),
            str(final_url or "").replace("\t", " "),
        ])
        log_lines.append(line)

    if not urls:
        log("", "SUMMARY", f"0 URLs enumerated; enumerated_count={enumerated_count}")
        Path("mjr-audacy-validation-v66.txt").write_text(
            "\n".join(log_lines), encoding="utf-8"
        )
        return [], enumerated_count

    out = []
    seen = set()
    detail_fetches = 0
    max_detail_fetches = 110

    def text_meta(soup, names):
        for name in names:
            tag = soup.find("meta", attrs={"name": name})
            if not tag:
                tag = soup.find("meta", attrs={"property": name})
            if tag and tag.get("content"):
                v = clean(tag.get("content"))
                if v:
                    return v
        return ""

    def first_text(soup, selectors):
        for sel in selectors:
            try:
                node = soup.select_one(sel)
            except Exception:
                node = None
            if node:
                v = clean(node.get_text(" ", strip=True))
                if v:
                    return v
        return ""

    def html_from_selectors(soup, selectors):
        for sel in selectors:
            try:
                node = soup.select_one(sel)
            except Exception:
                node = None
            if node:
                for bad in node.find_all(["script", "style", "noscript"]):
                    bad.decompose()
                raw = str(node)
                if clean(node.get_text(" ", strip=True)):
                    return raw
        return ""

    def extract_jsonld_job(soup):
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text(" ", strip=True)
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue

            objs = obj if isinstance(obj, list) else [obj]
            expanded = []
            for item in objs:
                if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                    expanded.extend(item["@graph"])
                else:
                    expanded.append(item)

            for item in expanded:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    return item
        return {}

    def get_location_from_jsonld(j):
        loc = j.get("jobLocation")
        if isinstance(loc, list):
            loc = loc[0] if loc else None
        if isinstance(loc, dict):
            addr = loc.get("address")
            if isinstance(addr, dict):
                parts = [
                    clean(addr.get("addressLocality", "")),
                    clean(addr.get("addressRegion", "")),
                    clean(addr.get("postalCode", "")),
                    clean(addr.get("addressCountry", "")),
                ]
                return ", ".join([p for p in parts if p])
        return ""

    def trusted_detail_date(j, html):
        candidates = []

        jd = j.get("datePosted") if isinstance(j, dict) else None
        if jd:
            candidates.append(jd)

        soup_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        for pat in [
            r"Posted\s+Date\s*[:\-]?\s*([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})",
            r"Posted\s+Date\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
            r"Date\s+Posted\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
            r"Posting\s+Date\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        ]:
            m = re.search(pat, soup_text, re.I)
            if m:
                candidates.append(m.group(1))

        for value in candidates:
            try:
                pd = dateparser.parse(str(value), fuzzy=True).date()
            except Exception:
                continue
            if pd >= CUTOFF and pd <= TODAY:
                return pd

        return None

    def audacy_location(soup, j, html):
        # 1. Structured JobPosting location.
        loc = get_location_from_jsonld(j) if isinstance(j, dict) else ""
        if loc:
            return loc

        # 2. iCIMS visible location containers / labels.
        loc = first_text(
            soup,
            [
                ".iCIMS_JobHeader .iCIMS_JobLocation",
                ".iCIMS_JobLocation",
                ".iCIMS_JobHeader .iCIMS_JobHeaderField",
                "[class*='job-location']",
                "[class*='jobLocation']",
                "[class*='JobLocation']",
            ],
        )
        if loc:
            loc = re.sub(
                r"^(?:Location|Job Location|Primary Location)\s*[:\-]?\s*",
                "",
                loc,
                flags=re.I,
            ).strip()
            if loc:
                return loc

        # 3. Parse labeled visible text.
        visible = soup.get_text(" ", strip=True)
        patterns = [
            r"(?:Job\s+Location|Primary\s+Location|Location)\s*[:\-]\s*"
            r"([^|•]{2,120}?)(?=\s+(?:Job\s+ID|ID|Category|Position|Overview|Responsibilities|$))",
            r"(?:Job\s+Location|Primary\s+Location|Location)\s*[:\-]\s*"
            r"([A-Za-z .'-]+,\s*[A-Z]{2})(?:\s|$)",
        ]
        for pat in patterns:
            m = re.search(pat, visible, re.I)
            if m:
                candidate = clean(m.group(1))
                if candidate:
                    return candidate

        # 4. Common JSON/JS fields in iCIMS HTML.
        for pat in [
            r'"(?:jobLocation|location|locationName|primaryLocation)"\s*:\s*"([^"]+)"',
            r"'(?:jobLocation|location|locationName|primaryLocation)'\s*:\s*'([^']+)'",
        ]:
            m = re.search(pat, html, re.I)
            if m:
                candidate = clean(
                    m.group(1)
                    .replace("\\/", "/")
                    .replace("\\u0026", "&")
                )
                if candidate:
                    return candidate

        return ""

    def audacy_city_state_country(location):
        """Map Audacy/iCIMS location text into XML city/state/country."""
        raw = clean(location or "")
        if not raw:
            return "", "", "US"

        # Common Audacy format: IL-Chicago, UNAVAILABLE, 60601, USA
        m = re.match(r"^([A-Z]{2})-([^,]+)", raw, re.I)
        if m:
            return clean(m.group(2)), m.group(1).upper(), "US"

        # Conventional format: Chicago, IL 60601
        m = re.match(
            r"^([^,]+),\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?",
            raw,
            re.I,
        )
        if m:
            return clean(m.group(1)), m.group(2).upper(), "US"

        return raw, "", infer_country(raw, "Audacy", "")

    def audacy_job_type(title, description_text):
        """
        Prevent Audacy boilerplate from turning unrelated jobs into internships.
        Internship is title-led; other job types can use the existing classifier.
        """
        t = clean(title).lower()

        internship_title = bool(
            re.search(
                r"\b(intern|internship|internships|student intern|summer intern|"
                r"fall intern|spring intern)\b",
                t,
                re.I,
            )
        )
        if internship_title:
            return "Internship"

        # Run the existing classifier on title plus a reduced opening portion
        # of the description, after removing internship boilerplate sentences.
        desc = clean(description_text or "")
        sentences = re.split(r"(?<=[.!?])\s+", desc)
        cleaned_sentences = []
        for sentence in sentences[:40]:
            low = sentence.lower()
            if (
                "internship" in low
                or re.search(r"\binterns?\b", low)
                or "equal opportunity" in low
                or "reasonable accommodation" in low
            ):
                continue
            cleaned_sentences.append(sentence)

        reduced = " ".join(cleaned_sentences)[:5000]
        jt = jobtype(title, reduced)

        # Hard guard: non-intern titles must never become Internship merely
        # because of description boilerplate.
        if str(jt).strip().lower() == "internship":
            jt = jobtype(title, "")

        return jt

    for detail_url in urls:
        if detail_fetches >= max_detail_fetches:
            log("", "STOP", "detail safety limit reached")
            print("Audacy v66 stopped at detail safety limit")
            break

        requested_id_match = re.search(r"/jobs/(\d+)/", detail_url, re.I)
        requested_id = requested_id_match.group(1) if requested_id_match else None
        if not requested_id:
            log("", "REJECT", "missing job id in enumerated URL", apply_url=detail_url)
            continue

        try:
            rr = req("GET", detail_url)
            detail_fetches += 1
        except Exception as e:
            log(requested_id, "REJECT", f"detail fetch failed: {type(e).__name__}: {e}", apply_url=detail_url)
            continue

        final = str(getattr(rr, "url", "") or detail_url)
        html = rr.text or ""
        if not html:
            log(requested_id, "REJECT", "empty detail HTML", final_url=final)
            continue

        final_id_match = re.search(r"/jobs/(\d+)/", final, re.I)
        final_id = final_id_match.group(1) if final_id_match else None
        if final_id and final_id != requested_id:
            reason = f"redirect id mismatch requested={requested_id} final={final_id}"
            log(requested_id, "REJECT", reason, final_url=final)
            continue

        if (
            f"/jobs/{requested_id}/" not in final
            and f"/jobs/{requested_id}/" not in html
        ):
            log(requested_id, "REJECT", "response is not matching detail page", final_url=final)
            continue

        soup = BeautifulSoup(html, "html.parser")
        j = extract_jsonld_job(soup)

        title = clean(j.get("title", "")) if isinstance(j, dict) else ""
        if not title:
            title = text_meta(soup, ["og:title", "twitter:title"])
        if not title:
            title = first_text(
                soup,
                [
                    "h1",
                    ".iCIMS_Header h1",
                    ".iCIMS_JobHeader h1",
                    ".job-title",
                    "[class*='job-title']",
                    "[class*='jobTitle']",
                ],
            )

        title = re.sub(r"\s+\|\s+Audacy.*$", "", title, flags=re.I).strip()
        title = re.sub(r"\s+-\s+Audacy.*$", "", title, flags=re.I).strip()

        if not title:
            log(requested_id, "REJECT", "blank title after direct parsing", final_url=final)
            continue

        description = ""
        if isinstance(j, dict):
            description = j.get("description") or ""

        description_text = ""
        if description:
            description_text = clean(
                BeautifulSoup(description, "html.parser").get_text(" ", strip=True)
            )

        if not description_text:
            description = html_from_selectors(
                soup,
                [
                    ".iCIMS_JobContent",
                    ".iCIMS_Expandable_Text",
                    ".iCIMS_JobDescription",
                    "[class*='job-description']",
                    "[class*='jobDescription']",
                    "main",
                    "article",
                ],
            )
            description_text = clean(
                BeautifulSoup(description or "", "html.parser").get_text(" ", strip=True)
            )

        if not description_text:
            log(requested_id, "REJECT", "blank description after direct parsing", title=title, final_url=final)
            continue

        location = audacy_location(soup, j, html)
        audacy_city, audacy_state, audacy_country = (
            audacy_city_state_country(location)
        )

        employer_date = trusted_detail_date(j, html)
        mjr_discovery_date = datetime.now(
            ZoneInfo("America/New_York")
        ).date()
        pd = employer_date or mjr_discovery_date

        try:
            live_apply = _icims_canonical_apply_url(final, html)
        except Exception as e:
            log(requested_id, "REJECT", f"canonical apply URL parser failed: {type(e).__name__}: {e}", title=title, pd=pd, final_url=final)
            continue

        live_id_match = re.search(r"/jobs/(\d+)/", live_apply or "", re.I)
        live_id = live_id_match.group(1) if live_id_match else None

        if live_id != requested_id:
            reason = f"apply URL id mismatch requested={requested_id} apply={live_id}"
            log(requested_id, "REJECT", reason, title=title, pd=pd, apply_url=live_apply, final_url=final)
            continue

        job = None
        parser_attempts = []

        try:
            job = _job_from_detail(src, final, html)
        except Exception as e:
            parser_attempts.append(f"_job_from_detail:{type(e).__name__}:{e}")

        if not job:
            try:
                job = _direct_board_job(src, final, html)
            except Exception as e:
                parser_attempts.append(f"_direct_board_job:{type(e).__name__}:{e}")

        if not job:
            # v48: Audacy's stale datePosted prevents the generic parsers from
            # constructing a Job. We already have verified title, description,
            # location and job-specific apply URL, so construct the project's
            # standard Job object directly.
            try:
                loc_for_job = location or ""
                job = Job(
                    requested_id,
                    title,
                    src["Company"],
                    description_text,
                    pd,
                    audacy_job_type(title, description_text),
                    category(
                        title,
                        description_text,
                        src["Industry"],
                        src["Company"],
                    ),
                    live_apply,
                    src["URL"],
                    src["URL"],
                    "",
                    normalize_work_arrangement(
                        description_text,
                        loc_for_job,
                    ),
                    audacy_city,
                    audacy_state,
                    audacy_country,
                    None,
                )
            except Exception as e:
                parser_attempts.append(
                    f"direct_Job:{type(e).__name__}:{e}"
                )
                reason = "could not instantiate standard job object"
                if parser_attempts:
                    reason += " | " + " | ".join(parser_attempts)
                log(
                    requested_id,
                    "REJECT",
                    reason,
                    title=title,
                    pd=pd,
                    apply_url=live_apply,
                    final_url=final,
                )
                continue

        job.title = title
        job.date = pd
        if hasattr(job, "job_type"):
            job.job_type = audacy_job_type(title, description_text)
        if hasattr(job, "jobtype"):
            job.jobtype = audacy_job_type(title, description_text)
        if hasattr(job, "type"):
            job.type = audacy_job_type(title, description_text)

        if hasattr(job, "description"):
            job.description = description
        if hasattr(job, "city"):
            job.city = audacy_city
        if hasattr(job, "state"):
            job.state = audacy_state
        if hasattr(job, "country"):
            job.country = audacy_country
        if hasattr(job, "location") and location:
            job.location = location
        if hasattr(job, "url"):
            job.url = live_apply
        if hasattr(job, "apply_url"):
            job.apply_url = live_apply

        dedupe_key = (requested_id, title.lower())
        if dedupe_key in seen:
            log(requested_id, "REJECT", "duplicate job id/title", title=title, pd=pd, apply_url=live_apply, final_url=final)
            continue

        seen.add(dedupe_key)
        out.append(job)

        source = "employer" if employer_date else "MJR discovery"
        log(
            requested_id,
            "ACCEPT",
            f"date source={source}; location={location}; city={audacy_city}; state={audacy_state}; country={audacy_country}; job_type={audacy_job_type(title, description_text)}",
            title=title,
            pd=pd,
            apply_url=live_apply,
            final_url=final,
        )

    log(
        "",
        "SUMMARY",
        f"enumerated={enumerated_count}; detail_checked={detail_fetches}; accepted={len(out)}"
    )

    Path("mjr-audacy-validation-v66.txt").write_text(
        "\n".join(log_lines),
        encoding="utf-8",
    )

    print(
        f"Audacy v66: {enumerated_count} enumerated, "
        f"{detail_fetches} detail pages checked, "
        f"{len(out)} verified jobs"
    )
    return out, enumerated_count


def collect_icims_rendered_generic(src):
    """
    Return (jobs, enumerated_count). The count lets the audit distinguish
    'enumerated_no_fresh_jobs' from 'zero_or_not_enumerable'.
    """
    src = _icims_effective_source(src)
    urls = _icims_frame_detail_urls(src)
    if not urls:
        return [], 0

    out = []
    seen = set()

    for url in urls:
        try:
            rr = req("GET", url)
        except Exception:
            continue

        final = str(getattr(rr, "url", "") or url)
        html = rr.text or ""

        j = _job_from_detail(src, final, html)
        if not j:
            j = _direct_board_job(src, final, html)
        if not j:
            continue

        pd = _v18_icims_date(html) or getattr(j, "date", None)
        if not pd or pd < CUTOFF:
            continue
        j.date = pd

        live_apply = _icims_canonical_apply_url(final, html)
        if hasattr(j, "url"):
            j.url = live_apply
        if hasattr(j, "apply_url"):
            j.apply_url = live_apply

        if j.id not in seen:
            seen.add(j.id)
            out.append(j)

    print(
        f"Generic iCIMS collector {src['Company']}: "
        f"{len(urls)} enumerated detail URLs, {len(out)} fresh jobs"
    )
    return out, len(urls)

def salem_icims_rendered_v30(src):
    """
    v34 Salem collector: inspect the actual iCIMS results iframe.
    """
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed")

    parsed = urlparse(src["URL"])
    base = f"{parsed.scheme}://{parsed.netloc}"
    start_url = base + "/jobs/search?ss=1&searchRelation=keyword_all"
    detail_urls = set()
    max_details = 120

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent=SESSION.headers.get(
                "User-Agent",
                "MJR-Jobs-Feed/1.0 (+https://www.mediajobsreport.com)",
            ),
            viewport={"width": 1440, "height": 1000},
        )
        page = context.new_page()
        page.set_default_timeout(20000)

        _v28_before_request(start_url)
        page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(1800)

        candidate_frames = []
        for frame in page.frames:
            fu = frame.url or ""
            up = urlparse(fu)
            if (
                up.netloc.lower() == parsed.netloc.lower()
                and "/jobs/search" in up.path.lower()
            ):
                candidate_frames.append(frame)

        print("Salem results frames:", [f.url for f in candidate_frames])

        for frame in candidate_frames:
            try:
                hrefs = frame.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => e.href)"
                )
            except Exception:
                hrefs = []

            for href in hrefs:
                if not href:
                    continue
                up = urlparse(href)
                if up.netloc.lower() != parsed.netloc.lower():
                    continue
                if re.search(r"/jobs/\d+/(?:[^/?#]+/)?job(?:[/?#]|$)", href, re.I):
                    q = parse_qs(up.query)
                    q["in_iframe"] = ["1"]
                    newq = urlencode({k: v[-1] for k, v in q.items()})
                    detail_urls.add(
                        urlunparse((up.scheme, up.netloc, up.path, "", newq, ""))
                    )

            try:
                html = frame.content()
            except Exception:
                html = ""

            pattern = r"(?i)(?:https?://[^\\\"'<> ]+)?/jobs/\d+/(?:[^\\\"'<>/?# ]+/)?job(?:\?[^\\\"'<> ]*)?"
            for match in re.findall(pattern, html):
                href = urljoin(frame.url, match.replace("&amp;", "&"))
                up = urlparse(href)
                if up.netloc.lower() != parsed.netloc.lower():
                    continue
                q = parse_qs(up.query)
                q["in_iframe"] = ["1"]
                newq = urlencode({k: v[-1] for k, v in q.items()})
                detail_urls.add(
                    urlunparse((up.scheme, up.netloc, up.path, "", newq, ""))
                )

        print(f"Salem frame-aware collector discovered {len(detail_urls)} detail URLs.")
        browser.close()

    out = []
    seen_ids = set()

    for url in sorted(detail_urls)[:max_details]:
        try:
            rr = req("GET", url)
        except Exception:
            continue

        final = str(getattr(rr, "url", "") or url)
        j = _job_from_detail(src, final, rr.text)
        if not j:
            j = _direct_board_job(src, final, rr.text)
        if not j:
            continue

        # v74 Salem: rendered iCIMS discovery is authoritative for liveness.
        # Salem frequently omits a dependable posting date on otherwise-live
        # detail pages. Prefer the employer date; otherwise preserve MJR's
        # first-discovery date from state, and use TODAY only for a genuinely
        # new live requisition.
        pd = _v18_icims_date(rr.text)
        if not pd:
            try:
                st = load_state()
                key = (getattr(j, "url", "") or final).rstrip("/").lower()
                rec = st.get(key, {})
                saved = (rec.get("job") or {}).get("date")
                if saved:
                    pd = date.fromisoformat(saved)
            except Exception:
                pd = None
        if not pd:
            pd = TODAY
        if pd < CUTOFF:
            continue
        j.date = pd

        if j.id not in seen_ids:
            seen_ids.add(j.id)
            out.append(j)

    print(f"Salem frame-aware collector qualifying jobs: {len(out)}")
    return out






# ============================================================
# v75 SALEM EXPLICIT iCIMS PAGINATION
# ============================================================

def salem_icims_v75(src):
    """Enumerate Salem jobs from explicit public iCIMS result pages.

    Salem's portal does not reliably expose pagination links to the rendered
    wrapper. Probe the documented/public iCIMS ``pr=`` result pages directly,
    extract only requisition IDs/URLs actually present in those result pages,
    then parse those live details. No numeric requisition-ID scanning is used.
    """
    parsed = urlparse(src["URL"])
    base = f"{parsed.scheme}://{parsed.netloc}"
    details = {}
    pages_checked = 0
    empty_after_results = 0
    found_any = False

    # Salem has historically exposed fewer than 100 current jobs. Twenty-four
    # pages is intentionally bounded while leaving ample room for growth.
    for pr in range(0, 24):
        page_urls = [
            f"{base}/jobs/search?ss=1&searchRelation=keyword_all&pr={pr}&in_iframe=1",
            f"{base}/jobs/search?ss=1&searchRelation=keyword_all&pr={pr}&mobile=true&needsRedirect=false",
        ]
        page_ids = set()
        page_urls_found = set()

        for page_url in page_urls:
            try:
                r = req("GET", page_url)
            except Exception:
                continue
            pages_checked += 1
            final = str(getattr(r, "url", "") or page_url)
            raw = html.unescape(r.text or "").replace("\\/", "/")
            soup = BeautifulSoup(raw, "html.parser")

            # Anchor-based canonical paths.
            for a in soup.find_all("a", href=True):
                u = urljoin(final, a.get("href") or "")
                up = urlparse(u)
                if up.netloc.lower() != parsed.netloc.lower():
                    continue
                m = re.search(r"/jobs/(\d+)(?:/([^/?#]+))?/job(?:[/?#]|$)", u, re.I)
                if m:
                    jid = m.group(1)
                    page_ids.add(jid)
                    q = parse_qs(up.query)
                    q["in_iframe"] = ["1"]
                    nq = urlencode({k: v[-1] for k, v in q.items()})
                    page_urls_found.add(urlunparse((up.scheme, up.netloc, up.path, "", nq, "")))

            # iCIMS can serialize job paths or bare IDs into script state.
            for m in re.finditer(r"/jobs/(\d+)(?:/[^\"'<>/?# ]+)?/job(?:[^\"'<> ]*)?", raw, re.I):
                jid = m.group(1)
                page_ids.add(jid)
                u = urljoin(final, m.group(0).replace("&amp;", "&"))
                up = urlparse(u)
                if up.netloc.lower() == parsed.netloc.lower():
                    q = parse_qs(up.query)
                    q["in_iframe"] = ["1"]
                    nq = urlencode({k: v[-1] for k, v in q.items()})
                    page_urls_found.add(urlunparse((up.scheme, up.netloc, up.path, "", nq, "")))

            # Some Salem/iCIMS result payloads expose the requisition ID without
            # a fully formed href. These IDs still came from the listing page.
            page_ids.update(re.findall(r'"(?:jobId|jobID|job_id|id)"\s*:\s*"?(\d{3,8})"?', raw, re.I))

        if page_ids or page_urls_found:
            found_any = True
            empty_after_results = 0
            for u in page_urls_found:
                m = re.search(r"/jobs/(\d+)/", u, re.I)
                if m:
                    details[m.group(1)] = u
            for jid in page_ids:
                details.setdefault(jid, f"{base}/jobs/{jid}/job?in_iframe=1")
        elif found_any:
            empty_after_results += 1
            # Two consecutive empty result pages means we've passed the live set.
            if empty_after_results >= 2:
                break

    print(f"Salem v75 pagination: {pages_checked} result requests, {len(details)} unique requisitions enumerated")

    out = []
    seen_ids = set()
    state = {}
    try:
        state = load_state()
    except Exception:
        state = {}

    for requested_id, url in sorted(details.items(), key=lambda kv: int(kv[0])):
        try:
            rr = req("GET", url)
        except Exception:
            continue
        final = str(getattr(rr, "url", "") or url)
        raw = rr.text or ""

        # Closed/redirected requisitions must not be retained just because their
        # ID appeared in cached/listing markup.
        final_id = re.search(r"/jobs/(\d+)/", final, re.I)
        if final_id and final_id.group(1) != requested_id:
            continue
        low = clean(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)).lower()
        if any(x in low for x in (
            "job is no longer available", "position is no longer available",
            "job has been filled", "job is no longer open"
        )):
            continue

        j = _job_from_detail(src, final, raw)
        if not j:
            j = _direct_board_job(src, final, raw)
        if not j:
            continue

        # Salem listing presence is authoritative for current liveness. Preserve
        # a reliable employer date where available; otherwise use MJR first-seen.
        pd = _v18_icims_date(raw)
        if not pd:
            candidates = []
            for k in (
                (getattr(j, "url", "") or "").rstrip("/").lower(),
                final.rstrip("/").lower(),
                url.rstrip("/").lower(),
            ):
                if k:
                    candidates.append(k)
            for key in candidates:
                rec = state.get(key, {})
                saved = (rec.get("job") or {}).get("date")
                if saved:
                    try:
                        pd = date.fromisoformat(saved)
                        break
                    except Exception:
                        pass
        if not pd:
            pd = TODAY
        if pd < CUTOFF:
            # A currently enumerated Salem job with an old employer date is still
            # live. Start/retain the MJR visibility window from current discovery
            # rather than silently dropping an open requisition.
            pd = TODAY
        j.date = pd

        live_apply = _icims_canonical_apply_url(final, raw)
        if hasattr(j, "url"):
            j.url = live_apply
        if hasattr(j, "apply_url"):
            j.apply_url = live_apply

        if j.id not in seen_ids:
            seen_ids.add(j.id)
            out.append(j)

    print(f"Salem v75 qualifying live jobs: {len(out)}")
    return out



# ============================================================
# v76 SALEM BROWSER-DRIVEN ENUMERATION
# ============================================================



def _salem_live_detail_job_v77(src, requested_id, final_url, raw, state):
    """Parse a Salem detail page already proven live by v76 browser discovery.

    Salem's iCIMS detail HTML often omits a trustworthy datePosted.  Because
    requested_id came from the live rendered Salem search, do not gate parsing
    on a posting date.  Preserve the first MJR discovery date from state when
    possible; otherwise start the normal MJR lifespan today.
    """
    soup = BeautifulSoup(raw or "", "html.parser")
    txt = clean(soup.get_text(" ", strip=True))
    low = txt.lower()
    if any(x in low for x in (
        "job is no longer available",
        "position is no longer available",
        "job has been filled",
        "job is no longer open",
    )):
        return None

    # Confirm the page is actually the requested Salem requisition.
    page_id = ""
    m = re.search(r"\bID\s*:?\s*(\d{3,8})\b", txt, re.I)
    if m:
        page_id = m.group(1)
    if page_id and page_id != str(requested_id):
        return None

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ") if h1 else "")
    if not title or title.lower() in {"careers", "job search", "search jobs"}:
        return None

    # Require meaningful Salem job content so cookie/error shells are rejected.
    if "overview" not in low and "responsibilities" not in low and len(txt) < 350:
        return None

    # Keep the useful job body while avoiding navigation where possible.
    main = (
        soup.find("main")
        or soup.find(attrs={"class": re.compile(r"(iCIMS_MainWrapper|job.?description|job.?detail|posting)", re.I)})
        or soup
    )
    desc = clean(main.get_text(" ", strip=True))
    if len(desc) < 200:
        desc = txt
    if len(desc) < 200:
        return None

    # Salem exposes Position Type explicitly (e.g. Regular Full-Time).
    pos_type = ""
    for pat in (
        r"Position Type\s*:?\s*(Regular Full[- ]Time|Regular Part[- ]Time|Full[- ]Time|Part[- ]Time|Temporary|Seasonal|Internship|Contract)",
        r"Employment Type\s*:?\s*(Regular Full[- ]Time|Regular Part[- ]Time|Full[- ]Time|Part[- ]Time|Temporary|Seasonal|Internship|Contract)",
    ):
        mm = re.search(pat, txt, re.I)
        if mm:
            pos_type = clean(mm.group(1))
            break

    # Salem location labels commonly look like US-CA-Sacramento.
    loc_raw = ""
    mm = re.search(
        r"Location\s*:?\s*(?:Location\s*)?(US-[A-Z]{2}-[A-Za-z0-9 .'/&()-]+?)(?=\s+(?:Overview|Responsibilities|Qualifications|Options|$))",
        txt, re.I
    )
    if mm:
        loc_raw = clean(mm.group(1))
    if not loc_raw:
        mm = re.search(r"\bUS-([A-Z]{2})-([A-Za-z][A-Za-z0-9 .'/&()-]{1,80})", txt)
        if mm:
            loc_raw = f"US-{mm.group(1)}-{clean(mm.group(2))}"

    city, st = "", ""
    if loc_raw:
        mm = re.match(r"US-([A-Z]{2})-(.+)", loc_raw)
        if mm:
            st = mm.group(1)
            city = clean(mm.group(2).replace("-", " "))
    loc = ", ".join(x for x in (city, st) if x) or loc_raw

    # Prefer an explicit Salem/iCIMS date when it exists, but never require it.
    pd = _v18_icims_date(raw)
    canonical_detail = final_url.split("#", 1)[0]
    live_apply = _icims_canonical_apply_url(canonical_detail, raw)

    if not pd:
        # Exact URL keys first.
        candidate_keys = {
            canonical_detail.rstrip("/").lower(),
            live_apply.rstrip("/").lower(),
        }
        for key in candidate_keys:
            rec = state.get(key, {}) if key else {}
            saved = (rec.get("job") or {}).get("date")
            if saved:
                try:
                    pd = date.fromisoformat(saved)
                    break
                except Exception:
                    pass

    if not pd:
        # Apply URLs can change query strings; recover first-seen by Salem ID.
        rid = str(requested_id)
        for rec in state.values():
            job = (rec or {}).get("job") or {}
            if clean(str(job.get("id") or "")) != rid:
                continue
            saved = job.get("date")
            if saved:
                try:
                    pd = date.fromisoformat(saved)
                    break
                except Exception:
                    pass
    if not pd:
        pd = TODAY

    return Job(
        str(requested_id),
        title,
        src["Company"],
        desc,
        pd,
        jobtype(title, pos_type or txt),
        category(title, desc, src.get("Industry", ""), src["Company"]),
        live_apply,
        src["URL"],
        src["URL"],
        "",
        normalize_work_arrangement(desc, loc or txt),
        city or loc,
        st,
        infer_country(loc or txt, src["Company"], desc),
    )

def salem_icims_v76(src):
    """Enumerate Salem from the live rendered iCIMS portal.

    Unlike v75, this does not assume a ``pr=`` URL contract.  It follows
    pagination/load-more controls the browser actually renders and also mines
    same-host search/network responses for requisition URLs/IDs.  IDs are only
    accepted when observed on Salem's live portal; there is no numeric scanning.
    """
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed")

    parsed = urlparse(src["URL"])
    base = f"{parsed.scheme}://{parsed.netloc}"
    start_url = base + "/jobs/search?ss=1&searchRelation=keyword_all"
    detail_urls = {}
    network_urls = []
    page_log = []
    max_steps = 30

    def add_candidate(u):
        if not u:
            return
        u = html.unescape(str(u)).replace("\\/", "/")
        up = urlparse(urljoin(base, u))
        if up.netloc.lower() != parsed.netloc.lower():
            return
        m = re.search(r"/jobs/(\d+)(?:/([^/?#]+))?/job(?:[/?#]|$)", up.geturl(), re.I)
        if not m:
            return
        jid = m.group(1)
        q = parse_qs(up.query)
        q["in_iframe"] = ["1"]
        nq = urlencode({k: v[-1] for k, v in q.items()})
        detail_urls[jid] = urlunparse((up.scheme, up.netloc, up.path, "", nq, ""))

    def mine_text(raw, base_url):
        raw = html.unescape(raw or "").replace("\\/", "/")
        for m in re.finditer(r"(?:https?://[^\"'<> ]+)?/jobs/(\d+)(?:/[^\"'<>/?# ]+)?/job(?:\?[^\"'<> ]*)?", raw, re.I):
            add_candidate(urljoin(base_url, m.group(0)))
        # Bare IDs are only accepted when they appear next to explicit iCIMS
        # job/requisition field names in a live search/network payload.
        for jid in re.findall(r'"(?:jobId|jobID|job_id|requisitionId|requisitionID)"\s*:\s*"?(\d{3,8})"?', raw, re.I):
            detail_urls.setdefault(jid, f"{base}/jobs/{jid}/job?in_iframe=1")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"])
        context = browser.new_context(
            user_agent=SESSION.headers.get("User-Agent", "MJR-Jobs-Feed/1.0 (+https://www.mediajobsreport.com)"),
            viewport={"width": 1440, "height": 1100},
        )
        page = context.new_page()
        page.set_default_timeout(10000)

        def on_response(resp):
            try:
                ru = resp.url or ""
                up = urlparse(ru)
                if up.netloc.lower() != parsed.netloc.lower():
                    return
                if not any(k in ru.lower() for k in ("/jobs", "search", "requisition", "posting")):
                    return
                network_urls.append(ru)
                ct = (resp.headers or {}).get("content-type", "").lower()
                if any(x in ct for x in ("text", "json", "javascript", "html")):
                    try:
                        mine_text(resp.text(), ru)
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("response", on_response)
        _v28_before_request(start_url)
        page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(1800)

        seen_fingerprints = set()
        for step in range(max_steps):
            page.wait_for_timeout(700)
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            page.wait_for_timeout(500)

            frames = [f for f in page.frames if urlparse(f.url or "").netloc.lower() == parsed.netloc.lower()]
            before = len(detail_urls)
            for frame in frames:
                try:
                    for href in frame.eval_on_selector_all("a[href]", "els => els.map(e => e.href)"):
                        add_candidate(href)
                except Exception:
                    pass
                try:
                    mine_text(frame.content(), frame.url or start_url)
                except Exception:
                    pass

            fingerprint = tuple(sorted(detail_urls))
            page_log.append(f"step={step} frames={len(frames)} jobs={len(detail_urls)} added={len(detail_urls)-before}")
            if fingerprint in seen_fingerprints and step > 0:
                # Still attempt one pagination click below; break only if none exists.
                pass
            seen_fingerprints.add(fingerprint)

            clicked = False
            selectors = [
                'a[rel="next"]',
                'a[aria-label*="next" i]',
                'button[aria-label*="next" i]',
                'a[title*="next" i]',
                'button[title*="next" i]',
                'a:has-text("Next")',
                'button:has-text("Next")',
                'a:has-text("Load More")',
                'button:has-text("Load More")',
                'a:has-text("Show More")',
                'button:has-text("Show More")',
            ]
            # Prefer the innermost search frame, then other same-host frames.
            ordered_frames = sorted(frames, key=lambda f: ("/jobs/search" not in (f.url or "").lower(), -len(f.url or "")))
            for frame in ordered_frames:
                for sel in selectors:
                    try:
                        loc = frame.locator(sel)
                        count = loc.count()
                    except Exception:
                        continue
                    for i in range(min(count, 4)):
                        try:
                            el = loc.nth(i)
                            if not el.is_visible() or not el.is_enabled():
                                continue
                            txt = clean(el.inner_text()) if hasattr(el, "inner_text") else ""
                            href = el.get_attribute("href")
                            if href and href.strip().lower().startswith(("javascript:", "#")):
                                href = None
                            page_log.append(f"click step={step} selector={sel} text={txt!r} href={href!r} frame={frame.url}")
                            el.click(timeout=7000)
                            try:
                                page.wait_for_load_state("networkidle", timeout=7000)
                            except Exception:
                                pass
                            page.wait_for_timeout(1200)
                            clicked = True
                            break
                        except Exception:
                            continue
                    if clicked:
                        break
                if clicked:
                    break

            if not clicked:
                # Some iCIMS portals render numbered paging links without a Next label.
                numbered = []
                for frame in ordered_frames:
                    try:
                        vals = frame.eval_on_selector_all(
                            'a[href]',
                            "els => els.map(e => ({href:e.href, text:(e.innerText||'').trim()}))"
                        )
                    except Exception:
                        vals = []
                    for item in vals:
                        t = (item.get("text") or "").strip()
                        href = item.get("href") or ""
                        if t.isdigit() and 1 <= int(t) <= 100 and "/jobs/search" in href.lower():
                            numbered.append((int(t), href, frame))
                # Navigate to the smallest not-yet-seen explicit result URL.
                used = set(x for x in network_urls if "/jobs/search" in x.lower())
                for _, href, frame in sorted(numbered, key=lambda x: x[0]):
                    if href in used:
                        continue
                    page_log.append(f"navigate numbered href={href}")
                    try:
                        frame.goto(href, wait_until="domcontentloaded", timeout=20000)
                        page.wait_for_timeout(1200)
                        clicked = True
                        break
                    except Exception:
                        continue

            if not clicked:
                break

        # Final harvest after last navigation/click.
        for frame in page.frames:
            if urlparse(frame.url or "").netloc.lower() != parsed.netloc.lower():
                continue
            try:
                mine_text(frame.content(), frame.url or start_url)
                for href in frame.eval_on_selector_all("a[href]", "els => els.map(e => e.href)"):
                    add_candidate(href)
            except Exception:
                pass
        browser.close()

    # Always leave a compact diagnostic in targeted runs.
    if MJR_TEST_COMPANIES:
        diag = Path("mjr-salem-v77-diagnostic.txt")
        diag.write_text(
            "MJR SALEM BROWSER ENUMERATION v77\n"
            + f"start={start_url}\n"
            + f"unique_jobs={len(detail_urls)}\n\n"
            + "=== PAGE LOG ===\n" + "\n".join(page_log[:300])
            + "\n\n=== NETWORK URLS ===\n" + "\n".join(list(dict.fromkeys(network_urls))[:500])
            + "\n\n=== JOB IDS ===\n" + "\n".join(sorted(detail_urls, key=lambda x: int(x)))
            + "\n",
            encoding="utf-8",
        )

    print(f"Salem v77 browser enumeration: {len(detail_urls)} unique requisitions observed")

    out = []
    seen_ids = set()
    state = {}
    try:
        state = load_state()
    except Exception:
        pass

    for requested_id, url in sorted(detail_urls.items(), key=lambda kv: int(kv[0])):
        try:
            rr = req("GET", url)
        except Exception:
            continue
        final = str(getattr(rr, "url", "") or url)
        raw = rr.text or ""
        fid = re.search(r"/jobs/(\d+)/", final, re.I)
        if fid and fid.group(1) != requested_id:
            continue
        low = clean(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)).lower()
        if any(x in low for x in ("job is no longer available", "position is no longer available", "job has been filled", "job is no longer open")):
            continue

        # v77: this requisition was observed on Salem's live rendered search.
        # Parse it with Salem's non-date-gated detail parser so missing/stale
        # iCIMS datePosted metadata cannot discard an otherwise-live job.
        j = _salem_live_detail_job_v77(src, requested_id, final, raw, state)
        if not j:
            continue

        if j.id not in seen_ids:
            seen_ids.add(j.id)
            out.append(j)

    print(f"Salem v77 qualifying live jobs: {len(out)}")
    return out

def salem_icims_v74(src):
    """v75 Salem collector: explicit pagination first, older fallbacks only on zero."""
    try:
        jobs = salem_icims_v75(src)
        if jobs:
            return jobs
    except Exception as e:
        print(f"Salem v75 pagination collector failed: {e}")
    try:
        jobs = salem_icims_rendered_v30(src)
        if jobs:
            return jobs
    except Exception as e:
        print(f"Salem rendered fallback failed: {e}")
    return salem_icims_v29(src)

def audacy_raw_diagnostic_v41(src):
    """
    Save the raw Audacy iCIMS search response before any browser-side redirect.
    """
    path = Path("mjr-audacy-raw-response.txt")
    lines = ["MJR AUDACY RAW RESPONSE DIAGNOSTIC v41"]

    urls = [
        "https://careers-audacy.icims.com/jobs/search?ss=1&searchRelation=keyword_all&pr=0&in_iframe=1",
        "https://careers-audacy.icims.com/jobs/search?ss=1&searchRelation=keyword_all&in_iframe=1",
        "https://careers-audacy.icims.com/jobs/search?ss=1",
    ]

    for url in urls:
        lines.append("")
        lines.append("=" * 72)
        lines.append(url)
        lines.append("=" * 72)
        try:
            rr = req("GET", url)
            lines.append(f"status={getattr(rr, 'status_code', '?')}")
            lines.append(f"final_url={getattr(rr, 'url', '')}")
            text = rr.text or ""
            lines.append(f"chars={len(text)}")
            lines.append(text[:120000])
        except Exception as e:
            lines.append(f"ERROR {repr(e)}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Audacy raw diagnostic written: {path}")
    return str(path)


def audacy_render_diagnostics_v38(src):
    """
    v39 Audacy diagnostic.
    Tries multiple direct iCIMS entry points and keeps inspecting the page
    even when navigation times out. Captures partial DOM, frames, URLs and
    relevant network activity instead of failing at page.goto().
    """
    diag_path = Path("mjr-audacy-diagnostic.txt")
    html_dir = Path("mjr-audacy-frames")
    html_dir.mkdir(exist_ok=True)
    lines = ["MJR AUDACY DIAGNOSTIC v39"]

    if sync_playwright is None:
        diag_path.write_text(
            "MJR AUDACY DIAGNOSTIC v39\nPlaywright unavailable\n",
            encoding="utf-8",
        )
        return str(diag_path)

    parsed = urlparse(src["URL"])
    base = f"{parsed.scheme}://{parsed.netloc}"

    entry_urls = [
        src["URL"],
        base + "/jobs/intro",
        base + "/jobs/search?ss=1",
        base + "/jobs/search?ss=1&in_iframe=1",
    ]

    # De-duplicate while preserving order.
    entry_urls = list(dict.fromkeys(entry_urls))

    requests_seen = []
    responses_seen = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent=SESSION.headers.get(
                    "User-Agent",
                    "MJR-Jobs-Feed/1.0 (+https://www.mediajobsreport.com)",
                ),
                viewport={"width": 1440, "height": 1100},
            )

            for entry_index, entry_url in enumerate(entry_urls):
                lines.append("")
                lines.append("=" * 72)
                lines.append(f"ENTRY {entry_index}: {entry_url}")
                lines.append("=" * 72)

                page = context.new_page()
                page.set_default_timeout(15000)

                page.on(
                    "request",
                    lambda req_: requests_seen.append(req_.url)
                    if any(k in req_.url.lower() for k in ("job", "icims", "search", "api"))
                    else None,
                )
                page.on(
                    "response",
                    lambda resp: responses_seen.append(f"{resp.status} {resp.url}")
                    if any(k in resp.url.lower() for k in ("job", "icims", "search", "api"))
                    else None,
                )

                _v28_before_request(entry_url)

                nav_error = None
                try:
                    page.goto(entry_url, wait_until="commit", timeout=15000)
                except Exception as e:
                    nav_error = repr(e)
                    lines.append(f"goto_error={nav_error}")

                # Give scripts/frames a bounded opportunity to populate.
                try:
                    page.wait_for_timeout(5000)
                except Exception:
                    pass

                lines.append(f"Current page URL: {page.url}")
                try:
                    lines.append(f"Title: {page.title()}")
                except Exception as e:
                    lines.append(f"title_error={repr(e)}")

                frames = page.frames
                lines.append(f"Frame count: {len(frames)}")

                all_ids = set()
                all_links = []

                for i, frame in enumerate(frames):
                    fu = frame.url or ""
                    lines.append("")
                    lines.append(f"--- ENTRY {entry_index} FRAME {i} ---")
                    lines.append(f"URL: {fu}")

                    try:
                        html = frame.content()
                    except Exception as e:
                        html = ""
                        lines.append(f"content_error={repr(e)}")

                    frame_file = html_dir / f"entry-{entry_index}-frame-{i}.html"
                    frame_file.write_text(html[:500000], encoding="utf-8")
                    lines.append(f"HTML chars captured: {min(len(html), 500000)}")

                    ids = set(re.findall(r"/jobs/(\d+)/", html, re.I))
                    ids.update(re.findall(r'"jobId"\s*:\s*"?(\d+)"?', html, re.I))
                    ids.update(re.findall(r'job(?:Id|ID|id)[=:\s"\']+(\d+)', html, re.I))
                    all_ids.update(ids)

                    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
                    interesting = []
                    for href in hrefs:
                        lh = href.lower()
                        if any(k in lh for k in ("job", "search", "icims", "apply", "requisition")):
                            interesting.append(urljoin(fu or entry_url, href))
                    interesting = list(dict.fromkeys(interesting))
                    all_links.extend(interesting)

                    lines.append(f"Job-like IDs: {len(ids)}")
                    for job_id in sorted(ids)[:200]:
                        lines.append(f"ID {job_id}")

                    lines.append(f"Interesting hrefs: {len(interesting)}")
                    for h in interesting[:200]:
                        lines.append(h)

                    # Compact marker snippets.
                    low = html.lower()
                    snips = 0
                    for marker in ("job", "requisition", "apply", "icims", "data-", "onclick", "iframe"):
                        pos = 0
                        while snips < 50:
                            j = low.find(marker, pos)
                            if j < 0:
                                break
                            a = max(0, j - 140)
                            b = min(len(html), j + 340)
                            lines.append(
                                f"[{marker}] " + re.sub(r"\s+", " ", html[a:b])
                            )
                            pos = j + len(marker)
                            snips += 1

                lines.append("")
                lines.append(f"ENTRY {entry_index} UNIQUE IDS: {len(all_ids)}")
                for x in sorted(all_ids)[:300]:
                    lines.append(x)

                lines.append("")
                lines.append(f"ENTRY {entry_index} UNIQUE LINKS: {len(set(all_links))}")
                for h in list(dict.fromkeys(all_links))[:300]:
                    lines.append(h)

                try:
                    page.close()
                except Exception:
                    pass

            lines.append("")
            lines.append("=== ALL NETWORK REQUESTS ===")
            for u in list(dict.fromkeys(requests_seen))[:500]:
                lines.append(u)
            if not requests_seen:
                lines.append("(none)")

            lines.append("")
            lines.append("=== ALL NETWORK RESPONSES ===")
            for u in list(dict.fromkeys(responses_seen))[:500]:
                lines.append(u)
            if not responses_seen:
                lines.append("(none)")

            browser.close()

    except Exception as e:
        lines.append("")
        lines.append("=== DIAGNOSTIC FATAL ERROR ===")
        lines.append(repr(e))

    finally:
        diag_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Audacy diagnostic written: {diag_path}")

    return str(diag_path)

# ============================================================
# v31 SALEM RENDERED DIAGNOSTICS
# ============================================================

def salem_render_diagnostics_v31(src):
    """
    v35 Salem iframe diagnostics.
    Always writes a compact diagnostic plus a bounded HTML capture of the
    rendered iCIMS results frame.
    """
    diag_path = Path(os.getenv("MJR_DIAGNOSTIC", "mjr-salem-diagnostic.txt"))
    html_path = Path("mjr-salem-iframe.html")
    lines = ["MJR SALEM DIAGNOSTIC v35"]

    try:
        if sync_playwright is None:
            raise RuntimeError("Playwright unavailable")

        parsed = urlparse(src["URL"])
        base = f"{parsed.scheme}://{parsed.netloc}"
        start_url = base + "/jobs/search?ss=1&searchRelation=keyword_all"
        lines.append(f"Start URL: {start_url}")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent=SESSION.headers.get(
                    "User-Agent",
                    "MJR-Jobs-Feed/1.0 (+https://www.mediajobsreport.com)",
                ),
                viewport={"width": 1440, "height": 1000},
            )
            page = context.new_page()
            page.set_default_timeout(20000)

            _v28_before_request(start_url)
            page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(2200)

            frames = []
            for frame in page.frames:
                fu = frame.url or ""
                up = urlparse(fu)
                if (
                    up.netloc.lower() == parsed.netloc.lower()
                    and "/jobs/search" in up.path.lower()
                ):
                    frames.append(frame)

            lines.append(f"Matching search frames: {len(frames)}")
            for i, frame in enumerate(frames):
                lines.append(f"Frame {i}: {frame.url}")

            target = None
            for frame in frames:
                if "in_iframe=1" in (frame.url or ""):
                    target = frame
                    break
            if target is None and frames:
                target = frames[-1]

            if target is None:
                raise RuntimeError("No Salem iCIMS search-results frame found")

            html = target.content()
            max_chars = 500000
            html_path.write_text(html[:max_chars], encoding="utf-8")
            lines.append(f"Captured iframe HTML chars: {min(len(html), max_chars)}")
            lines.append(f"Full rendered iframe HTML chars: {len(html)}")

            ids = set()
            patterns = [
                r"/jobs/(\d+)",
                r"job(?:Id|ID|id)[=:\s\"']+(\d+)",
                r"req(?:Id|ID|id)[=:\s\"']+(\d+)",
                r"data-[^=]*job[^=]*=[\"']?(\d+)",
                r"value=[\"']?(\d{3,})[\"']?",
            ]
            for pat in patterns:
                for match in re.findall(pat, html, re.I):
                    ids.add(str(match))

            lines.append("")
            lines.append("=== NUMERIC JOB-LIKE IDS ===")
            lines.extend(sorted(ids)[:300] or ["(none)"])

            actions = re.findall(
                r"<form[^>]+action=[\"']([^\"']+)[\"']",
                html,
                re.I,
            )
            lines.append("")
            lines.append("=== FORM ACTIONS ===")
            lines.extend(
                [urljoin(target.url, a) for a in list(dict.fromkeys(actions))[:100]]
                or ["(none)"]
            )

            attrs = re.findall(
                r"(data-[a-z0-9_-]*(?:job|req|requisition)[a-z0-9_-]*=[\"'][^\"']+[\"'])",
                html,
                re.I,
            )
            lines.append("")
            lines.append("=== JOB/REQ DATA ATTRIBUTES ===")
            lines.extend(list(dict.fromkeys(attrs))[:200] or ["(none)"])

            hrefs = re.findall(r"href=[\"']([^\"']+)[\"']", html, re.I)
            interesting = []
            for href in hrefs:
                lh = href.lower()
                if any(k in lh for k in ("job", "search", "icims", "requisition", "posting")):
                    interesting.append(urljoin(target.url, href))
            lines.append("")
            lines.append("=== INTERESTING HREFS ===")
            lines.extend(list(dict.fromkeys(interesting))[:200] or ["(none)"])

            low = html.lower()
            lines.append("")
            lines.append("=== MARKER SNIPPETS ===")
            count = 0
            for marker in ("job", "requisition", "posting", "icims", "data-", "onclick", "form"):
                pos = 0
                while count < 80:
                    idx = low.find(marker, pos)
                    if idx < 0:
                        break
                    a = max(0, idx - 180)
                    b = min(len(html), idx + 420)
                    snippet = re.sub(r"\s+", " ", html[a:b])
                    lines.append(f"[{marker}] {snippet}")
                    pos = idx + len(marker)
                    count += 1
            if count == 0:
                lines.append("(none)")

            browser.close()

    except Exception as e:
        lines.append("")
        lines.append("=== DIAGNOSTIC ERROR ===")
        lines.append(repr(e))

    finally:
        diag_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if not html_path.exists():
            html_path.write_text(
                "<!-- Salem iframe HTML was not captured. See diagnostic file. -->\n",
                encoding="utf-8",
            )
        print(f"Salem diagnostic written: {diag_path}")
        print(f"Salem iframe capture written: {html_path}")

    return str(diag_path)


def voiceover_sources_test():
    """Targeted test of public, direct voiceover job sources.

    Only genuine voice-performance titles in the United States or Canada are
    accepted. Speech testing, transcription, validation and generic AI data
    collection roles are intentionally excluded. Source wording is retained.
    """
    collected = []
    audit_rows = []
    title_pattern = re.compile(
        r"\b(voice[ -]?over artist|voice actor|voice actress|voice talent|"
        r"audiobook narrator|audio book narrator|dubbing artist|dubbing actor|"
        r"narration artist|professional narrator)\b",
        re.I,
    )

    def allowed_country(location, description=""):
        country = infer_country(location, "", description)
        low = clean(location).lower()
        explicitly_foreign = any(
            marker in low
            for marker in (
                "australia", "united kingdom", " uk", "new zealand", "singapore",
                "south africa", "portugal", "belgium", "switzerland", "germany",
                "france", "italy", "spain", "indonesia", "vietnam", "qatar",
                "saudi arabia", "united arab emirates", "laos", "taiwan",
            )
        )
        return country in {"US", "CA"} and not explicitly_foreign

    # TSMG exposes a public Lever feed. Query its audio team first to avoid
    # downloading its very large unrelated global board.
    try:
        endpoint = "https://api.lever.co/v0/postings/tsmg"
        r = req(
            "GET",
            endpoint,
            params={
                "mode": "json",
                "team": "Audio Data Collection",
                "limit": "100",
            },
        )
        rows = r.json()
        if not isinstance(rows, list):
            rows = []
        matched = 0
        stale = 0
        for row in rows:
            title = clean(row.get("text", ""))
            if not title_pattern.search(title):
                continue
            categories = row.get("categories") or {}
            location = clean(categories.get("location", ""))
            plain = clean(row.get("descriptionPlain", ""))
            if not allowed_country(location, plain):
                continue
            created_ms = row.get("createdAt")
            try:
                posted = datetime.fromtimestamp(
                    float(created_ms) / 1000.0,
                    tz=ZoneInfo("UTC"),
                ).date()
            except Exception:
                posted = None
            if not posted or posted < CUTOFF:
                stale += 1
                continue

            parts = []
            if row.get("description"):
                parts.append(str(row.get("description")))
            elif plain:
                parts.append(f"<p>{html.escape(plain)}</p>")
            for block in row.get("lists") or []:
                heading = clean(block.get("text", ""))
                content = str(block.get("content") or "")
                if heading:
                    parts.append(f"<h3>{html.escape(heading)}</h3>")
                if content:
                    parts.append(f"<ul>{content}</ul>" if "<li" in content.lower() else content)
            if row.get("additional"):
                parts.append(str(row.get("additional")))
            description = format_description("".join(parts))
            if len(strip_html(description)) < 200:
                continue

            apply_url = clean(row.get("hostedUrl") or row.get("applyUrl") or "")
            if not apply_url:
                continue
            commitment = clean(categories.get("commitment", ""))
            role_type = "Contract" if re.search(r"\b(project|contract)\b", f"{commitment} {plain}", re.I) else jobtype(title, commitment)
            collected.append(
                Job(
                    clean(row.get("id", "")) or hashlib.sha1(apply_url.encode()).hexdigest()[:16],
                    title,
                    "TSMG",
                    description,
                    posted,
                    role_type,
                    "Voiceover",
                    apply_url,
                    "https://jobs.lever.co/tsmg",
                    "https://thesocialmediagroup.com/",
                    "",
                    "Remote" if re.search(r"\bremote\b", location, re.I) else normalize_work_arrangement(description, location),
                    location,
                    "",
                    infer_country(location, "TSMG", description),
                    None,
                )
            )
            matched += 1
        audit_rows.append([
            "TSMG",
            "Lever",
            "https://jobs.lever.co/tsmg",
            "ok" if matched else "zero_or_no_fresh_voiceover_jobs",
            matched,
            f"voiceover_matches={matched}; stale_matches={stale}; rows_checked={len(rows)}",
        ])
    except Exception as e:
        audit_rows.append(["TSMG", "Lever", "https://jobs.lever.co/tsmg", "error", 0, repr(e)])

    # Filmless has a genuine direct voiceover posting on SmartRecruiters. Its
    # released date is honored, so an old evergreen ad is audited but does not
    # enter MJR's time-limited feed.
    try:
        endpoint = "https://api.smartrecruiters.com/v1/companies/Filmless/postings"
        r = req("GET", endpoint, params={"limit": "100"})
        payload = r.json()
        rows = payload.get("content", []) if isinstance(payload, dict) else []
        matched = 0
        stale = 0
        for summary in rows:
            title = clean(summary.get("name", ""))
            if not title_pattern.search(title):
                continue
            detail_url = clean(summary.get("ref", ""))
            detail = req("GET", detail_url).json() if detail_url else summary
            location_obj = detail.get("location") or {}
            location = clean(location_obj.get("fullLocation") or ", ".join(
                x for x in [location_obj.get("city"), location_obj.get("region")] if x
            ))
            if not allowed_country(location):
                continue
            posted = pdate(detail.get("releasedDate"))
            if not posted or posted < CUTOFF:
                stale += 1
                continue
            sections = ((detail.get("jobAd") or {}).get("sections") or {})
            description = format_description("".join(
                str(section.get("text") or "")
                for section in sections.values()
                if isinstance(section, dict)
            ))
            apply_url = clean(detail.get("applyUrl") or "")
            if len(strip_html(description)) < 200 or not apply_url:
                continue
            employment = clean((detail.get("typeOfEmployment") or {}).get("label", ""))
            collected.append(Job(
                clean(detail.get("id", "")) or hashlib.sha1(apply_url.encode()).hexdigest()[:16],
                title, "Filmless", description, posted, jobtype(title, employment),
                "Voiceover", apply_url, "https://jobs.smartrecruiters.com/Filmless",
                "https://www.filmless.com/", "", "Remote" if location_obj.get("remote") else "On-Site",
                location, clean(location_obj.get("region", "")), "US", None,
            ))
            matched += 1
        audit_rows.append([
            "Filmless", "SmartRecruiters", "https://jobs.smartrecruiters.com/Filmless",
            "ok" if matched else "zero_or_no_fresh_voiceover_jobs", matched,
            f"voiceover_matches={matched}; stale_matches={stale}; rows_checked={len(rows)}",
        ])
    except Exception as e:
        audit_rows.append(["Filmless", "SmartRecruiters", "https://jobs.smartrecruiters.com/Filmless", "error", 0, repr(e)])

    # Appen's board is checked because it carries speech/recording projects,
    # but only genuine voice-performance titles may pass this category gate.
    try:
        endpoint = "https://api.lever.co/v0/postings/appen"
        r = req("GET", endpoint, params={"mode": "json"})
        rows = r.json()
        if not isinstance(rows, list):
            rows = []
        candidates = [row for row in rows if title_pattern.search(clean(row.get("text", "")))]
        audit_rows.append([
            "CrowdGen/Appen", "Lever", "https://jobs.lever.co/appen",
            "candidate_titles_found" if candidates else "no_qualifying_voiceover_titles",
            0,
            f"genuine_voiceover_titles={len(candidates)}; rows_checked={len(rows)}; speech testing and validation excluded",
        ])
    except Exception as e:
        audit_rows.append(["CrowdGen/Appen", "Lever", "https://jobs.lever.co/appen", "error", 0, repr(e)])

    print(f"Voiceover source test: {len(collected)} fresh qualifying jobs")
    return collected, audit_rows


def careeronestop_townsquare_test():
    """Controlled CareerOneStop job-search test for Townsquare Media.

    CareerOneStop fields are retained as supplied. MJR category, job type and
    work arrangement are separate feed metadata. The employer page is consulted
    only when the API listing does not include a usable job description.
    """
    user_id = clean(os.getenv("CAREERONESTOP_USER_ID", ""))
    token = clean(os.getenv("CAREERONESTOP_API_TOKEN", ""))
    if not user_id or not token:
        raise RuntimeError(
            "CareerOneStop test requested but CAREERONESTOP_USER_ID or "
            "CAREERONESTOP_API_TOKEN is missing"
        )

    segments = [
        user_id,
        "Townsquare Media",  # required nationwide keyword
        "US",                # nationwide search
        "0",                 # radius is ignored for US searches
        "0",                 # relevance sort
        "0",                 # default sort order
        "0",                 # first record
        "250",               # documented maximum page size
        "30",                # postings acquired during the last 30 days
    ]
    endpoint = "https://api.careeronestop.org/v1/jobsearch/" + "/".join(
        quote(str(value), safe="") for value in segments
    )
    api_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    try:
        response = req(
            "GET",
            endpoint,
            params={"companyName": "Townsquare Media", "showFilters": "false"},
            headers=api_headers,
        )
    except requests.HTTPError as exc:
        # CareerOneStop has used both IIS route forms. Retry the documented
        # trailing-slash variant only for a route-level 404.
        if getattr(exc.response, "status_code", None) != 404:
            raise
        response = req(
            "GET",
            endpoint + "/",
            params={"companyName": "Townsquare Media", "showFilters": "false"},
            headers=api_headers,
        )
    payload = response.json()

    # Save a credential-free diagnostic so the first test is easy to verify.
    diagnostic = payload
    if isinstance(payload, dict):
        diagnostic = dict(payload)
        for key in list(diagnostic):
            if str(key).lower() in {"token", "authorization", "userid", "user_id"}:
                diagnostic[key] = "[redacted]"
    CAREERONESTOP_DIAGNOSTIC.write_text(
        json.dumps(diagnostic, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    rows = []
    if isinstance(payload, dict):
        for key in ("Jobs", "jobs", "Results", "results", "JobResults", "jobResults"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        if not rows:
            for value in payload.values():
                if isinstance(value, dict):
                    for key in ("Jobs", "jobs", "Results", "results"):
                        if isinstance(value.get(key), list):
                            rows = value[key]
                            break
                if rows:
                    break
    elif isinstance(payload, list):
        rows = payload

    def value(row, *names):
        if not isinstance(row, dict):
            return ""
        lowered = {str(k).lower(): v for k, v in row.items()}
        for name in names:
            found = row.get(name, lowered.get(name.lower(), ""))
            if found not in (None, "", [], {}):
                return found
        return ""

    out = []
    seen = set()
    for row in rows:
        company = clean(str(value(row, "Company", "CompanyName", "Employer") or ""))
        if "townsquare" not in company.lower():
            continue

        title = clean(str(value(row, "JobTitle", "Title", "PositionTitle") or ""))
        url = clean(str(value(row, "URL", "JobUrl", "JobURL", "ApplyURL") or ""))
        jid = clean(str(value(row, "JvId", "JobId", "JobID", "Id") or ""))
        location = clean(str(value(row, "Location", "JobLocation") or ""))
        desc_raw = value(row, "JobDescription", "Description", "JobDesc")
        description = format_description(str(desc_raw or ""))

        if not title or not url:
            continue

        # Some List Jobs responses are summaries. Use the linked employer page
        # for the description while leaving CareerOneStop-supplied fields intact.
        if len(strip_html(description)) < 200:
            try:
                detail_response = req("GET", url)
                detail_source = {
                    "Company": company,
                    "Industry": "Radio",
                    "URL": url,
                }
                detail_job = _job_from_detail(
                    detail_source,
                    str(getattr(detail_response, "url", "") or url),
                    detail_response.text,
                )
                if detail_job:
                    description = detail_job.description
            except Exception:
                pass

        if len(strip_html(description)) < 200:
            continue

        posted = pdate(str(value(
            row, "Date", "PostedDate", "DatePosted", "AcquisitionDate",
        ) or "")) or TODAY
        if posted < CUTOFF:
            continue

        city = location
        state = ""
        match = re.match(r"^(.+?),\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?$", location)
        if match:
            city, state = clean(match.group(1)), match.group(2)

        jid = jid or hashlib.sha1(url.encode()).hexdigest()[:16]
        key = (jid, url.lower())
        if key in seen:
            continue
        seen.add(key)

        out.append(Job(
            f"cos-{jid}",
            title,
            company,
            description,
            posted,
            jobtype(title, description),
            category(title, description, "Radio", company),
            url,
            CAREERONESTOP_SOURCE,
            CAREERONESTOP_SOURCE,
            "",
            normalize_work_arrangement(description, location, title),
            city,
            state,
            "US",
        ))

    print(f"CareerOneStop Townsquare Media test: {len(rows)} API rows, {len(out)} qualifying jobs")
    return out

def main():
    with SOURCES_FILE.open(
        newline="",
        encoding="utf-8-sig",
    ) as f:
        sources = list(csv.DictReader(f))

    # v54: normalize any legacy Cox Radio source row before filtering/crawling.
    # This prevents an older CSV row or stale branch copy from forcing the
    # radio-only CMG search back into the feed.
    for row in sources:
        company = clean(row.get("Company", "")).lower()
        url = str(row.get("URL", "") or "").lower()
        if company == "cox radio" or (
            "careers.cmg.com" in url and "q=radio" in url
        ):
            row["Industry"] = row.get("Industry") or "Broadcast Media"
            row["Company"] = "Cox Media Group"
            row["ATS"] = "SAP SuccessFactors"
            row["URL"] = "https://careers.cmg.com/go/All-Jobs/9298500/"
            row["Active"] = "True"

    # Also accept the legacy test name if it is ever entered manually.
    if "cox radio" in MJR_TEST_COMPANIES:
        MJR_TEST_COMPANIES.discard("cox radio")
        MJR_TEST_COMPANIES.add("cox media group")

    careeronestop_test = "careeronestop" in MJR_TEST_COMPANIES
    voiceover_test = "voiceover" in MJR_TEST_COMPANIES
    # Include verified Voiceover sources in every normal full crawl. Keep them
    # out of unrelated targeted company tests so those reports remain isolated.
    voiceover_enabled = voiceover_test or not MJR_TEST_COMPANIES
    careeronestop_enabled = (
        os.getenv("CAREERONESTOP_ENABLED", "false").lower() in {"1", "true", "yes"}
        or careeronestop_test
    )

    if MJR_TEST_COMPANIES:
        sources = [s for s in sources if _v28_source_enabled(s)]
        print("TEST MODE companies:", ", ".join(sorted(MJR_TEST_COMPANIES)))
        print(f"TEST MODE source rows: {len(sources)}")

    jobs = []
    audit = []

    for s in sources:
        if str(s.get("Active", "True")).lower() in ("false", "0", "no"):
            continue

        try:
            a = s["ATS"].lower()

            company_key = clean(s.get("Company", "")).lower()
            company_route_key = _company_test_key(company_key)


            got = (
                associated_press(s)
                if company_key in {"associated press", "associated press (ap)"}
                else connoisseur_paycor(s)
                if company_key == "connoisseur media"
                else midwest_family_direct(s)
                if company_key == "mid-west family of companies"
                else saga_distributed_direct(s)
                if company_key == "saga communications"
                else renda_media_direct(s)
                if company_key == "renda media"
                else ashby(s)
                if "ashby" in a or "ashbyhq.com" in s.get("URL", "").lower()
                else []
                if company_key == "audacy"
                else nrg_paylocity(s)
                if company_key == "nrg media"
                else paylocity_v18(s)
                if company_key in {
                    "dick broadcasting company",
                    "hope media group",
                    "weigel",
                }
                else siriusxm_v17(s)
                if company_key == "siriusxm"
                else townsquare_greenhouse(s)
                if company_route_key == "townsquare"
                else nbcuniversal_v17(s)
                if company_key == "nbcuniversal"
                else greenhouse(s)
                if company_key == "tegna"
                else cumulus_v17(s)
                if company_key == "cumulus media"
                else cox_successfactors(s)
                if company_key in {"cox media group", "cox radio"}
                else paramount_successfactors(s)
                if company_route_key == "paramount"
                else fox_public(s)
                if company_route_key == "fox"
                else disney_public(s)
                if company_route_key in {"disney/abc", "espn"}
                else wbd_phenom(s)
                if company_key == "cnn"
                else gray_direct(s)
                if company_key == "gray media"
                else workday(s)
                if "workday" in a
                else greenhouse(s)
                if "greenhouse" in a
                else paylocity(s)
                if "paylocity" in a
                else hubbard_adp_cx(s)
                if company_key == "hubbard broadcasting"
                else adp(s)
                if "adp" in a
                else dayforce(s)
                if "dayforce" in a
                else salem_icims_v76(s)
                if company_key == "salem media group"
                else icims(s)
                if "icims" in a
                else ukg(s)
                if "ukg" in a or "ultipro" in a
                else oracle_recruiting(s)
                if "oracle recruiting" in a or "oracle cloud" in a
                else paycom(s)
                if "paycom" in a
                else federated_media(s)
                if clean(s.get("Company", "")).lower() == "federated media"
                else connoisseur_media(s)
                if clean(s.get("Company", "")).lower() == "connoisseur media"
                else isolved(s)
                if "isolved" in a or "ourcareerpages" in s.get("URL", "").lower()
                else batch_direct_board(s)
                if clean(s.get("Company", "")).lower() in BATCH_DIRECT_COMPANIES
                else betterteam_active_board(s)
                if _ats_family(s) == "betterteam"
                else jazzhr_active_board(s)
                if _ats_family(s) == "jazzhr"
                else ats_html(s)
                if _ats_family(s)
                else generic(s)
            )

            icims_enumerated = 0
            public_board_enumerated = company_key in PUBLIC_BOARD_ENUMERATION

            # v40: Audacy uses direct iCIMS enumeration with strict
            # ID/title/apply-link validation. Do not use the old wrapper path.
            if not got and company_key == "audacy":
                got, icims_enumerated = collect_audacy_v40(s)

            # v37: Generic modern-iCIMS recovery. Only runs when the normal
            # collector returned zero. This is now the preferred path for
            # Audacy, EMF/K-LOVE, Salem, and other zero-result iCIMS portals.
            if not got and company_key != "audacy" and (
                "icims" in a
                or company_key == "educational media foundation"
                or "careers-kloveair1.icims.com" in s.get("URL", "").lower()
            ):
                generic_icims_jobs, icims_enumerated = collect_icims_rendered_generic(s)
                if generic_icims_jobs:
                    got = generic_icims_jobs

            # v38: targeted Audacy diagnostics when enumeration still fails.
            # v42: no automatic Audacy diagnostics here. Enumeration and
            # fresh-job validation are now the test; avoid consuming the
            # per-domain request budget with duplicate diagnostic fetches.

            # Keep Salem diagnostics available only when specifically tested.
            if not got and company_key == "salem media group" and MJR_TEST_COMPANIES:
                try:
                    salem_render_diagnostics_v31(s)
                except Exception as e:
                    print(f"Salem diagnostic failed: {e}")

            if not got and not public_board_enumerated:
                got = structured_jobs_v27(s)

            if not got and not public_board_enumerated and company_key in V25_FAST_RADIO_TARGETS:
                if company_key == "educational media foundation":
                    got = emf_v26_narrow(s)
                else:
                    got = radio_direct_v25(s)

            if not got and not public_board_enumerated and company_key in V23_RADIO_TARGETS:
                got = radio_targeted_v23(s)

            if not got and not public_board_enumerated and company_key in RADIO_RECOVERY_COMPANIES:
                got = radio_recovery(s)

            scope_rejected = []
            if company_key in {"disney / abc", "espn", "meruelo media"}:
                got, scope_rejected = apply_company_scope_filters(got)
                if scope_rejected:
                    print(
                        f"{s['Company']} scope filter rejected "
                        f"{len(scope_rejected)} non-media jobs"
                    )
                    for rejected_job, reason in scope_rejected[:20]:
                        print(f"  REJECTED: {rejected_job.title} — {reason}")

            jobs += got

            eligibility_notes = []
            for _j in got:
                _reasons = []
                if not _j.url:
                    _reasons.append("missing_url")
                if len(_j.description or "") < 200:
                    _reasons.append("description_lt_200")
                if not job_is_fresh(_j):
                    _reasons.append("not_fresh")
                if _j.category not in APPROVED:
                    _reasons.append("unapproved_category=" + str(_j.category))
                if _reasons:
                    eligibility_notes.append(
                        clean(_j.title) + ":" + "|".join(_reasons)
                    )

            audit.append(
                [
                    s["Company"],
                    s["ATS"],
                    s["URL"],
                    (
                        "ok"
                        if got
                        else "enumerated_no_fresh_jobs"
                        if icims_enumerated or public_board_enumerated
                        else "zero_or_not_enumerable"
                    ),
                    len(got),
                    "; ".join(
                        part for part in [
                            f"enumerated_jobs={PUBLIC_BOARD_ENUMERATION[company_key]}"
                            if public_board_enumerated else (
                                f"enumerated_jobs={icims_enumerated}"
                                if company_key == "audacy"
                                else f"enumerated_detail_urls={icims_enumerated}"
                            ) if icims_enumerated else "",
                            f"non_media_scope_rejected={len(scope_rejected)}"
                            if scope_rejected else "",
                            ("feed_filter_rejected=" + " ; ".join(eligibility_notes[:12]))
                            if eligibility_notes else "",
                        ]
                        if part
                    ),
                ]
            )

        except Exception as e:
            audit.append(
                [
                    s["Company"],
                    s["ATS"],
                    s["URL"],
                    "error",
                    0,
                    repr(e),
                ]
            )

    if careeronestop_enabled:
        try:
            cos_jobs = careeronestop_townsquare_test()
            jobs += cos_jobs
            audit.append([
                "Townsquare Media",
                "CareerOneStop Web API",
                CAREERONESTOP_SOURCE,
                "ok" if cos_jobs else "zero_or_not_enumerable",
                len(cos_jobs),
                "Controlled CareerOneStop test",
            ])
        except Exception as e:
            error_text = repr(e)
            # requests includes the requested URL in HTTP errors. CareerOneStop
            # places the API user ID in that path, so redact both credentials
            # before the audit is printed or uploaded.
            for secret in (
                os.getenv("CAREERONESTOP_USER_ID", ""),
                os.getenv("CAREERONESTOP_API_TOKEN", ""),
            ):
                if secret:
                    error_text = error_text.replace(secret, "[redacted]")
                    error_text = error_text.replace(quote(secret, safe=""), "[redacted]")
            audit.append([
                "Townsquare Media",
                "CareerOneStop Web API",
                CAREERONESTOP_SOURCE,
                "error",
                0,
                error_text,
            ])

    if voiceover_enabled:
        voiceover_jobs, voiceover_audit = voiceover_sources_test()
        jobs += voiceover_jobs
        audit.extend(voiceover_audit)

    ded = {
        j.url.rstrip("/").lower(): j
        for j in jobs
        if j.url
        and len(j.description) >= 200
        and job_is_fresh(j)
        and j.category in APPROVED
    }

    jobs = stateful(list(ded.values()))

    # Final defense after state reconciliation: no previously cached or newly
    # collected out-of-scope conglomerate job may reach the XML.
    jobs, final_scope_rejected = apply_company_scope_filters(jobs)
    if final_scope_rejected:
        print(f"Final scope filter rejected {len(final_scope_rejected)} non-media jobs")

    ded = {
        j.url.rstrip("/").lower(): j
        for j in jobs
    }

    jobs = list(ded.values())
    jobs, recovered_locations, duplicate_warnings, arrangement_corrections = finalize_jobs(jobs)

    write_xml(jobs)
    write_quality_report(
        jobs,
        recovered_locations,
        duplicate_warnings,
        arrangement_corrections,
    )

    with AUDITFILE.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        w = csv.writer(f)
        w.writerow(
            [
                "company",
                "ats",
                "url",
                "status",
                "jobs_collected",
                "error",
            ]
        )
        w.writerows(audit)

    print("Wrote", len(jobs), "jobs")


if __name__ == "__main__":
    main()
