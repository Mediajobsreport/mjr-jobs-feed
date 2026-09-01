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
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None



TODAY = date.today()
WINDOW_DAYS = int(os.getenv("MJR_WINDOW_DAYS", "21"))
CUTOFF = TODAY - timedelta(days=WINDOW_DAYS)

SOURCES_FILE = Path(os.getenv("MJR_SOURCES", "mjr-ats-sources.csv"))
OUTFILE = Path(os.getenv("MJR_OUTPUT", "mjr-jboard-master.xml"))
AUDITFILE = Path(os.getenv("MJR_AUDIT", "mjr-ats-audit.csv"))
STATE_FILE = Path(os.getenv("MJR_STATE", "mjr-job-state.json"))

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
}


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def strip_html(s):
    return clean(BeautifulSoup(s or "", "html.parser").get_text(" "))


def pdate(v):
    if not v:
        return None

    s = clean(v)
    m = re.search(r"(\d+)\s+days?\s+ago", s, re.I)
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
    for n in range(4):
        try:
            r = SESSION.request(method, url, timeout=15, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2**n)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException:
            if n == 3:
                raise
            time.sleep(2**n)




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
        except Exception as e:
            last = e
            msg = str(e).lower()
            transient = any(x in msg for x in (
                "429","timed out","timeout","temporarily unavailable",
                "connection reset","502","503","504"
            ))
            if transient and attempt < 2:
                time.sleep(_v28_backoff_seconds(attempt))
                continue
            raise
    if last:
        raise last
@dataclass
class Job:
    id: str
    title: str
    company: str
    description: str
    date: date
    jobtype: str
    category: str
    url: str
    source: str
    company_website: str = ""
    logo: str = ""
    work_arrangement: str = "Not Specified"
    city: str = ""
    state: str = ""
    country: str = "US"
    employer_deadline: date | None = None

    @property
    def expiration(self):
        life = 30 if self.jobtype == "Internship" else 21
        days = max(1, life - (TODAY - self.date).days)

        if self.employer_deadline:
            d = (self.employer_deadline - TODAY).days
            if 1 <= d < days:
                days = d

        return days


def jobtype(title, text=""):
    t = clean(title).lower()
    s = (title + " " + text).lower()

    # Internship must be indicated by the job title itself. Employer boilerplate
    # often mentions interns/internships and must not reclassify normal jobs.
    if re.search(
        r"\b(intern|internship|internships|student intern|summer intern|"
        r"fall intern|spring intern|co-op intern)\b",
        t,
        re.I,
    ):
        return "Internship"
    if "part" in s and "time" in s:
        return "Part Time"
    if "temporary" in s or " temp " in " " + s:
        return "Temporary"
    if "contract" in s:
        return "Contract"

    return "Full Time"


def category(title, desc, industry, company):
    """
    MJR job-category classifier.

    Rule:
      1. Classify by the actual job function, led by the title.
      2. Use a small amount of description context only when a title is vague.
      3. Never classify a job as Radio or Television simply because its employer
         or source list is labeled Radio or Television.
      4. Employer/source context is reserved for narrow final fallbacks such as
         Music Industry and Public Media / Higher Ed.
    """

    t = clean(title).lower()
    d = clean(desc).lower()
    c = clean(company).lower()
    ind = clean(industry).lower()

    # A short description slice is enough for fallback context without allowing
    # generic employer boilerplate to dominate the classification.
    d_short = d[:2500]
    td = f" {t} {d_short} "

    # ------------------------------------------------------------------
    # 1) INTERNSHIPS
    # ------------------------------------------------------------------
    if re.search(r"\b(intern|internship|trainee program|summer trainee|rotation trainee|praktikant|becario)\b", t):
        return "Internships"

    # ------------------------------------------------------------------
    # 2) ENGINEERING / IT / SOFTWARE / PROGRAMMING / TECHNICAL SYSTEMS
    #
    # MJR rule: software engineering, computer programming/development, IT,
    # infrastructure, systems, networking, cybersecurity, technical support,
    # data engineering and broadcast engineering all belong in Engineering.
    # This block intentionally runs before Sales, Business Office and Digital.
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"software engineer|software engineering|software developer|software development|"
        r"application developer|applications developer|web developer|frontend developer|"
        r"front-end developer|backend developer|back-end developer|full stack developer|"
        r"full-stack developer|mobile developer|computer programmer|software programmer|programmer|"
        r"developer advocate|development engineer|qa engineer|quality assurance engineer|"
        r"data engineer|data scientist|machine learning engineer|ml engineer|ai engineer|"
        r"devops|site reliability|site reliability engineer|sre|"
        r"cybersecurity|cyber security|information security|application security|app security|"
        r"security engineer|security analyst|security architect|security operations|soc analyst|"
        r"cloud engineer|cloud architect|cloud infrastructure|"
        r"ai architect|ai lead architect|artificial intelligence architect|machine learning architect|"
        r"technology architect|platform architect|software and platforms architecture|"
        r"solutions architect|solution architect|software architect|platform architect|"
        r"enterprise architect|data architect|technical architect|systems architect|"
        r"information technology|information systems|\bit manager\b|\bit director\b|"
        r"it engineer|it technician|it support|desktop support|help desk|service desk|"
        r"systems engineer|system engineer|systems administrator|system administrator|"
        r"network engineer|network administrator|network operations|networking engineer|"
        r"infrastructure engineer|infrastructure manager|infrastructure architect|"
        r"database administrator|database engineer|dba|"
        r"salesforce developer|salesforce administrator|salesforce admin|salesforce engineer|"
        r"salesforce business analyst|business systems analyst|systems analyst|technical analyst|"
        r"technology innovation|agile technical coach|agile delivery lead|"
        r"broadcast engineer|chief engineer|maintenance engineer|rf engineer|audio engineer|"
        r"video engineer|studio engineer|field engineer|transmission engineer|"
        r"broadcast technician|studio technician|maintenance technician|maintenance tech|"
        r"audio technician|broadcast audio|technical maintenance|technical director|"
        r"technical operations|master control|master control operator|transmission|transmitter|"
        r"systems technician|av technician|audiovisual technician|electronics technician"
        r")\b",
        t,
    ):
        return "Engineering"

    # Any engineer/engineering title is Engineering under MJR's category rules.
    if re.search(r"\bengineer(?:ing)?\b", t):
        return "Engineering"

    # Vague IT/technical titles can use a small amount of description context.
    if re.search(r"\b(technician|analyst|administrator|architect|specialist|manager|director|scrum master|agile coach)\b", t) and re.search(
        r"\b("
        r"information technology|information systems|software development|software engineering|"
        r"computer programming|network infrastructure|networking|cybersecurity|cyber security|"
        r"cloud infrastructure|systems administration|technical support|help desk|service desk|"
        r"broadcast systems|broadcasting systems|production systems|transmitter|transmission|"
        r"database administration|application development|systems engineering|"
        r"technology innovation|enterprise applications|technical architecture|"
        r"cloud-native|cloud native|devops|kubernetes|terraform|software platforms?"
        r")\b",
        d_short,
    ):
        return "Engineering"

    # ------------------------------------------------------------------
    # 2) SALES / MARKETING / PROMOTIONS / REVENUE

    # Strong title-first Sales & Marketing rules.
    # "Digital" does not override the core function when the job is sales,
    # advertising, marketing, account management, partnerships or revenue.
    if re.search(
        r"\b("
        r"digital sales|digital media sales|digital advertising sales|digital ad sales|"
        r"digital sales consultant|digital sales manager|digital sales executive|"
        r"digital marketing consultant|digital marketing manager|digital marketing coordinator|"
        r"digital marketing specialist|digital marketing strategist|"
        r"digital account executive|digital account manager|digital client partner|"
        r"integrated marketing|integrated sales|media sales|advertising sales|ad sales|"
        r"account executive|account manager|sales executive|sales manager|sales consultant|"
        r"sales coordinator|account coordinator|business development|revenue|"
        r"sponsorship sales|sponsorships|partnership sales|client partner|client services|"
        r"customer success|brand marketing|performance marketing|paid media|"
        r"advertising operations|ad operations|commercial sales|commercial partnerships"
        r")\b",
        t,
    ):
        return "Sales & Marketing"

    # Sales-function titles must resolve before generic coordinator/admin rules.
    # Handles both "Sales Coordinator" and ATS-style "Coordinator, Sales" titles.
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"account executive|account manager|account coordinator|"
        r"sales executive|sales manager|sales director|sales assistant|"
        r"sales representative|sales consultant|sales coordinator|sales development representative|"
        r"sales operations analyst|head of ad sales|podcast ad sales|paid media partnerships|advertising operations|ad product commercialization|"
        r"physical sales coordinator|media consultant|marketing consultant|"
        r"advertising consultant|advertising coordinator|ad sales coordinator|"
        r"media sales|advertising sales|digital sales|local sales|national sales|"
        r"business development|business development coordinator|"
        r"revenue manager|revenue director|revenue operations|revenue coordinator|"
        r"marketing|growth marketing|performance marketing|product marketing|"
        r"marketing manager|marketing director|marketing coordinator|"
        r"marketing specialist|marketing assistant|brand marketing|brand manager|"
        r"brand ambassador|promotions assistant|promotions coordinator|"
        r"promotions manager|promotion coordinator|promotion manager|"
        r"event marketing|event marketing coordinator|field marketing|"
        r"affiliate sales|partnership sales|partnerships coordinator|"
        r"sponsorship sales|sponsorship manager|sponsorship coordinator|"
        r"client services|client services coordinator|client success|"
        r"customer success|customer success coordinator|digital client partner|"
        r"visual sales lead|gerente comercial ventas|accounts executive"
        r")\b",
        t,
    ):
        return "Sales & Marketing"

    # ATS titles frequently put "Coordinator" first, followed by the function.
    if re.search(
        r"\bcoordinator\s*,?\s*("
        r"sales|physical sales|marketing|digital marketing|advertising|"
        r"business development|revenue|promotions?|sponsorships?|"
        r"partnerships?|client services|customer success|account"
        r")\b",
        t,
    ):
        return "Sales & Marketing"

    # ------------------------------------------------------------------
    # 3) BUSINESS OFFICE / ADMIN / FINANCE / HR / LEGAL / SCHEDULING
    # ------------------------------------------------------------------
    # People/HR functions should never fall through to Journalism just because
    # their descriptions discuss communications, engagement, or internal content.
    if re.search(
        r"\b("
        r"organizational development|organisation development|employee engagement|"
        r"people & culture|people and culture|total rewards|compensation and benefits|"
        r"benefits manager|benefits specialist|hr specialist|human resources specialist|"
        r"talent management|workforce planning|learning and development|learning & development"
        r")\b",
        t,
    ):
        return "Business Office"

    # Physical/corporate security belongs in Business Office. Cyber/information
    # security is already captured by the Engineering block above.
    if re.search(
        r"\b(security officer|security guard|corporate security|physical security|security supervisor)\b",
        t,
    ):
        return "Business Office"

    # Broadcast commercial traffic/continuity is a business-office function.
    # Do not use the word 'traffic' alone: Traffic Anchor/Reporter is handled
    # later as an on-air Radio role.
    if re.search(
        r"\b("
        r"traffic assistant|traffic coordinator|traffic co-ordinator|traffic director|"
        r"traffic specialist|continuity assistant|continuity coordinator|"
        r"continuity co-ordinator|continuity director|continuity specialist"
        r")\b",
        t,
    ):
        return "Business Office"

    if re.search(
        r"\b("
        r"accountant|accounting|accounts payable|accounts receivable|"
        r"finance|financial|controller|payroll|bookkeeper|treasury|tax|"
        r"human resources|hr coordinator|hr manager|hr business partner|"
        r"people operations|people partner|talent acquisition|recruiter|recruiting|"
        r"administrative assistant|administrative coordinator|administrator|"
        r"executive assistant|office assistant|office coordinator|office manager|"
        r"legal|attorney|counsel|paralegal|business affairs|contracts|compliance|"
        r"procurement|purchasing|facilities|receptionist|billing|credit|collections|audit,? risk|risk and advisory|"
        r"operations coordinator|business operations|"
        r"assignment coordinator|assignment co-ordinator|"
        r"scheduling coordinator|schedule coordinator|scheduler|"
        r"resource coordinator|program coordinator|project coordinator"
        r")\b",
        t,
    ):
        return "Business Office"

    # ------------------------------------------------------------------
    # Engineering/IT classification is handled above before Sales/Business Office.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 5) PUBLIC RELATIONS / CORPORATE COMMUNICATIONS
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"public relations|publicist|publicity|media relations|press relations|"
        r"corporate communications|communications manager|communications director|"
        r"communications specialist|communications coordinator|"
        r"communications officer|public affairs|press secretary|"
        r"external communications|internal communications"
        r")\b",
        t,
    ):
        return "Public Relations"

    # ------------------------------------------------------------------
    # 6) DIGITAL / PRODUCT / UX / DIGITAL CONTENT
    #
    # Technical IT, software, computer programming, cybersecurity, systems
    # and engineering roles are handled above as Engineering.
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"product manager|product owner|digital product|technology product|"
        r"ux|ui|user experience|user interface|"
        r"digital programming|youtube programming|digital insights|digital business|"
        r"label analytics|crm manager|digital analyst|video partnerships|youtube"
        r")\b",
        t,
    ):
        return "Digital"

    # Traffic Anchor/Reporter is an on-air role and must resolve before the
    # generic Journalism reporter rule. Use posting context to distinguish TV
    # from radio; default to Radio when no television evidence is present.
    if re.search(r"\b(traffic anchor|traffic reporter)\b", t):
        if re.search(
            r"\b(television|tv station|tv newscast|newscast|on camera|on-camera|video broadcast)\b",
            d_short,
        ):
            return "Television"
        return "Radio"

    # Exact newsroom photography title; avoids treating product photography as news.
    if t == "photographer":
        return "Journalism"

    # 7) JOURNALISM / NEWS / EDITORIAL
    # Place Journalism before platform production so photojournalists,
    # assignment editors, managing editors, etc. do not become Television.
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"reporter|journalist|news anchor|anchor/reporter|anchor reporter|"
        r"multimedia journalist|mmj|correspondent|investigative reporter|"
        r"investigative journalist|bureau chief|assignment editor|assignment manager|"
        r"news editor|managing editor|copy editor|editorial editor|"
        r"news writer|newsroom editor|news director|digital journalist|"
        r"breaking news|photojournalist|news photographer|sports reporter|"
        r"sports anchor|weather anchor|meteorologist|weather reporter|"
        r"fact checker|fact-checker|contributing editor|photo editor|editorial page assistant editor|senior editor"
        r")\b",
        t,
    ):
        return "Journalism"

    # ------------------------------------------------------------------
    # 7) RADIO / ON-AIR / RADIO PROGRAMMING
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"board operator|on[- ]air|air personality|air talent|"
        r"radio host|radio personality|radio announcer|announcer operator|"
        r"talk host|talk/news host|news/talk host|talk show host|"
        r"morning host|morning show host|afternoon host|midday host|night host|"
        r"music director|radio program director|assistant program director|"
        r"radio producer|radio news host|radio news anchor|"
        r"traffic reporter|traffic anchor|traffic producer|update anchor|part[- ]time talent|play[- ]by[- ]play|sports talk host|"
        r"disc jockey|radio dj|radio presenter"
        r")\b",
        t,
    ):
        return "Radio"

    # If the title simply says "host", require radio/audio evidence.
    if re.search(r"\bhost\b", t) and re.search(
        r"\b(radio|audio program|on air|on-air|broadcast radio|radio program)\b",
        td,
    ):
        return "Radio"

    # ------------------------------------------------------------------
    # 8) TELEVISION / VIDEO PRODUCTION / STUDIO PRODUCTION
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"tv producer|television producer|newscast producer|executive producer|"
        r"associate producer|show producer|line producer|segment producer|"
        r"technical producer|studio manager|studio crew|camera operator|"
        r"videographer|director of photography|video editor|"
        r"production assistant|production coordinator|production manager|"
        r"broadcast director|newscast director|floor director|"
        r"graphics operator|character generator|cg operator|"
        r"control room operator|studio operator"
        r")\b",
        t,
    ):
        return "Television"

    # ------------------------------------------------------------------
    # 9) DIGITAL / WEB / SOCIAL / DESIGN / PRODUCT / PODCAST / STREAMING
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"digital producer|digital editor|digital content|digital writer|"
        r"web producer|web editor|web developer|web designer|"
        r"social media|social producer|social editor|audience development|"
        r"seo|newsletter|podcast producer|podcast editor|podcast manager|"
        r"streaming producer|streaming editor|streaming manager|"
        r"product manager|product owner|mobile product|app product|"
        r"ux|ui|content strategist|digital strategist|ecommerce|e-commerce|"
        r"graphic designer|graphics designer|visual designer|motion designer|"
        r"digital designer|creative designer|multimedia designer|"
        r"content designer|interactive designer|website manager"
        r")\b",
        t,
    ):
        return "Digital"

    # Legacy safety net; software/data engineering should already resolve to Engineering above.
    if re.search(
        r"\b("
        r"software engineer|software developer|data engineer|data scientist|"
        r"machine learning engineer|frontend engineer|front-end engineer|"
        r"backend engineer|back-end engineer|full stack engineer|full-stack engineer|"
        r"mobile engineer|web engineer"
        r")\b",
        t,
    ):
        return "Digital"

    # ------------------------------------------------------------------
    # 10) MUSIC INDUSTRY FUNCTIONS
    # Specific music-business functions should beat generic management.
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"a&r|artist relations|artist development|artist services|"
        r"record label|label manager|label operations|music publishing|"
        r"publishing administration|music licensing|sync licensing|"
        r"rights management|rights administration|royalties|royalty|"
        r"repertoire|catalog manager|catalog management|music supervisor|"
        r"music coordinator|music operations|music partnerships|"
        r"songwriter relations|writer relations|creative music"
        r")\b",
        t,
    ):
        return "Music Industry"

    # ------------------------------------------------------------------
    # 11) MANAGEMENT
    # Only after functional VP/director titles have had their chance above.
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"general manager|market manager|station manager|regional manager|"
        r"president|chief executive officer|ceo|chief operating officer|coo|"
        r"chief content officer|cco|chief revenue officer|cro|"
        r"chief financial officer|cfo|chief marketing officer|cmo|"
        r"vice president|vp|senior vice president|svp|"
        r"executive vice president|evp|head of"
        r")\b",
        t,
    ):
        return "Management"

    # ------------------------------------------------------------------
    # 12) DESCRIPTION FALLBACKS FOR VAGUE TITLES
    # These are intentionally ordered by job function and kept narrow.
    # ------------------------------------------------------------------

    # Finance/HR/admin/scheduling.
    if re.search(
        r"\b("
        r"accounts payable|accounts receivable|financial reporting|payroll|"
        r"human resources|talent acquisition|recruiting|administrative support|"
        r"weekly schedules?|staff scheduling|scheduling conflicts|"
        r"commercial inventory|spot placement|commercial logs?|continuity|affidavits?|"
        r"contracts administration|legal services|procurement"
        r")\b",
        d_short,
    ):
        return "Business Office"

    # Engineering/technical systems.
    if re.search(
        r"\b("
        r"broadcasting systems?|broadcast systems?|production systems?|"
        r"transmitter|transmission systems?|master control|technical systems?|"
        r"networking|electronics|audiovisual|telecommunications|"
        r"studio equipment|technical infrastructure"
        r")\b",
        d_short,
    ) and re.search(
        r"\b(maintain|maintenance|repair|troubleshoot|install|commission|technical|technician)\b",
        d_short,
    ):
        return "Engineering"

    # Sales/marketing/promotions.
    if re.search(
        r"\b("
        r"sales revenue|sales cycle|new business development|advertising sales|"
        r"marketing campaigns?|brand activations?|promotional material|"
        r"community events?|sponsorship sales|client relationships?"
        r")\b",
        d_short,
    ):
        return "Sales & Marketing"

    # Journalism.
    if re.search(
        r"\b("
        r"reporting|journalism|journalistic|newsroom|newsgathering|"
        r"investigative reporting|editorial judgment|fact-checking|"
        r"writes? news|news coverage|reporter"
        r")\b",
        d_short,
    ):
        return "Journalism"

    # Radio.
    if re.search(
        r"\b("
        r"radio program|radio station|on-air host|on air host|"
        r"radio news|control board|broadcast automation|playlist|"
        r"audio programming|radio programming"
        r")\b",
        d_short,
    ):
        return "Radio"

    # Television/video production.
    if re.search(
        r"\b("
        r"television production|tv production|newscast production|"
        r"control room|camera operation|video production|studio production|"
        r"direct live broadcasts?|television studio"
        r")\b",
        d_short,
    ):
        return "Television"

    # Digital/web/social/design.
    if re.search(
        r"\b("
        r"digital content|social media|website content|web publishing|"
        r"streaming content|podcast production|seo strategy|"
        r"graphic design|visual design|user experience|digital product"
        r")\b",
        d_short,
    ):
        return "Digital"

    # PR / communications.
    if re.search(
        r"\b("
        r"media relations|press releases?|public relations|"
        r"corporate communications|external communications|public affairs"
        r")\b",
        d_short,
    ):
        return "Public Relations"

    # ------------------------------------------------------------------
    # 13) NARROW EMPLOYER / SOURCE FALLBACKS
    # No generic Radio or Television fallback. That was the source of the
    # CBC/Radio-Canada misclassifications in the previous feed.
    # ------------------------------------------------------------------

    music_company = any(
        x in c
        for x in [
            "sony music",
            "warner music",
            "universal music",
            "atlantic records",
            "republic records",
            "capitol records",
            "columbia records",
            "rca records",
            "epic records",
            "interscope",
            "def jam",
            "motown",
            "ascap",
            "bmi",
            "sesac",
            "onerpm",
            "reservoir media",
            "recording academy",
            "music group",
            "records",
            "music entertainment",
        ]
    )

    if music_company or ind == "music industry":
        return "Music Industry"

    public_media = any(
        x in c
        for x in [
            "npr",
            "pbs",
            "american public media",
            "public broadcasting",
            "public radio",
            "public media",
            "university",
            "college",
            "state university",
        ]
    )

    if public_media or ind in {
        "public media",
        "higher ed",
        "public media / higher ed",
    }:
        return "Public Media / Higher Ed"

    # Journalism and Digital source labels are less platform-specific than
    # Radio/Television and are acceptable only as cautious final fallbacks.
    if ind == "journalism":
        return "Journalism"

    if ind == "digital":
        return "Digital"

    # IMPORTANT:
    # Do not use Radio or Television source-family fallback.
    # A broadcaster employs finance, design, engineering, HR, sales, etc.
    return "Business Office"


def infer_country(location, company="", description=""):
    """
    Infer country conservatively from explicit location text.
    Default remains US, but Canadian province/territory abbreviations and
    well-known Canadian place names are mapped to CA.
    """
    loc = clean(location)
    s = f" {loc.lower()} {clean(description)[:1200].lower()} "

    canadian_abbr = re.search(
        r"(?:^|[, /-])(?:ab|bc|mb|nb|nl|ns|nt|nu|on|pe|qc|sk|yt)(?:$|[, /-])",
        loc.lower(),
    )

    canadian_names = any(
        name in s
        for name in [
            "alberta",
            "british columbia",
            "manitoba",
            "new brunswick",
            "newfoundland",
            "labrador",
            "nova scotia",
            "northwest territories",
            "nunavut",
            "ontario",
            "prince edward island",
            "quebec",
            "saskatchewan",
            "yukon",
            "montreal",
            "montréal",
            "toronto",
            "ottawa",
            "vancouver",
            "calgary",
            "edmonton",
            "winnipeg",
            "halifax",
            "regina",
            "saskatoon",
            "sherbrooke",
            "moncton",
            "rankin inlet",
        ]
    )

    if canadian_abbr or canadian_names:
        return "CA"

    return "US"


def normalize_work_arrangement(description, location):
    """
    Conservative MJR work-arrangement classifier.

    Remote only when THIS JOB is explicitly remote, or a structured location
    value itself is Remote. Hybrid only when THIS JOB is explicitly hybrid or
    requires a recurring office/remote mix. Otherwise default On-Site.
    """
    desc = clean(description).lower()
    loc = clean(location).lower()
    s = f" {desc} "

    negative_remote_patterns = [
        r"\bnot (?:a )?remote (?:role|position|job)\b",
        r"\bnot remote\b",
        r"\bno remote\b",
        r"\bremote work (?:is )?not (?:available|offered|permitted|allowed)\b",
        r"\bremote (?:work|option|arrangement) (?:is )?not (?:available|offered|permitted|allowed)\b",
        r"\bmust work (?:on[- ]?site|onsite|in[- ]person)\b",
        r"\brequired to work (?:on[- ]?site|onsite|in[- ]person)\b",
        r"\bmust be (?:on[- ]?site|onsite|in[- ]person)\b",
        r"\bno (?:work from home|work[- ]from[- ]home|telework|telecommuting)\b",
    ]
    if any(re.search(p, s) for p in negative_remote_patterns):
        return "On-Site"

    onsite_patterns = [
        r"\brequires? full[- ]time on[- ]site presence\b",
        r"\brequires? full time on site presence\b",
        r"\bthis role requires? full[- ]time on[- ]site\b",
        r"\bthis role requires? full time on site\b",
        r"\bthis (?:position|role|job) is (?:an? )?on[- ]site\b",
        r"\bthis (?:position|role|job) is onsite\b",
        r"\bon[- ]site role\b",
        r"\bon site role\b",
        r"\bonsite role\b",
        r"\bin[- ]person role\b",
        r"\bin person role\b",
        r"\bwork(?:ing)? on[- ]site\b",
        r"\bwork(?:ing)? onsite\b",
        r"\breport(?:s|ing)? (?:daily )?to (?:our|the) .{0,80}(?:office|studio|station|facility)\b",
    ]
    if any(re.search(p, s) for p in onsite_patterns):
        return "On-Site"

    loc_compact = re.sub(r"\s+", " ", loc).strip()
    if re.search(
        r"^(?:remote|remote[- /](?:us|usa|united states)|us[- /]remote|"
        r"united states[- /]remote|remote,?\s*(?:us|usa|united states))$",
        loc_compact,
        re.I,
    ):
        return "Remote"

    if re.search(r"^(?:hybrid|hybrid[- /].+|.+[- /]hybrid)$", loc_compact, re.I):
        return "Hybrid"

    hybrid_patterns = [
        r"\bthis (?:role|position|job) is hybrid\b",
        r"\bhybrid (?:role|position|job|schedule|arrangement)\b",
        r"\btelework/hybrid\b",
        r"\bhybrid/telework\b",
        r"\bhybrid work (?:model|schedule|arrangement)\b",
        r"\bmix of in[- ]office and remote work\b",
        r"\bcombining remote work and office presence\b",
        r"\bcombination of remote work and office presence\b",
        r"\bpartly remote\b",
        r"\bpartially remote\b",
        r"\b\d+\s+days? per week in (?:the )?office\b",
        r"\b\d+\s+days? a week in (?:the )?office\b",
        r"\b(?:two|three|four)\s+days? per week in (?:the )?office\b",
        r"\b(?:two|three|four)\s+days? a week in (?:the )?office\b",
    ]
    if any(re.search(p, s) for p in hybrid_patterns):
        return "Hybrid"

    remote_patterns = [
        r"\bthis (?:role|position|job) is (?:fully |100% )?remote\b",
        r"\bthis is (?:a )?(?:fully |100% )?remote (?:role|position|job)\b",
        r"\b(?:role|position|job) is (?:fully |100% )?remote\b",
        r"\b(?:fully |100% )?remote (?:role|position|job)\b",
        r"\bcan be performed (?:fully )?remotely\b",
        r"\bmay be performed (?:fully )?remotely\b",
        r"\bwill be performed (?:fully )?remotely\b",
        r"\bwork remotely from\b",
        r"\bworking remotely from\b",
        r"\bremote[- ]based (?:role|position|job)\b",
        r"\bhome[- ]based (?:role|position|job)\b",
        r"\bthis (?:role|position|job) (?:allows|offers) (?:full[- ]time )?remote work\b",
        r"\beligible to work fully remotely\b",
    ]
    if any(re.search(p, s) for p in remote_patterns):
        return "Remote"

    return "On-Site"


def _discover_workday_endpoint(src_url):
    """Return (host, tenant, site) from a Workday public career URL.

    Handles ordinary tenant hosts plus `myworkdaycenter` aliases by inspecting
    the public landing page for the actual /wday/cxs/{tenant}/{site}/ endpoint.
    """
    u = urlparse(src_url)
    host = u.netloc
    parts = [
        p for p in u.path.split("/")
        if p and p not in ("en-US", "en-CA", "en-GB", "jobs", "details")
    ]
    site = parts[0] if parts else ""
    tenant = host.split(".")[0] if host else ""

    # Normal Workday tenant host.
    if tenant and tenant != "myworkdaycenter" and site:
        return host, tenant, site

    # Alias hosts such as Tribune's myworkdaycenter do not expose the tenant
    # in the hostname. The real cxs tenant is commonly embedded in page state.
    try:
        r = req("GET", src_url)
        raw = html.unescape(r.text or "").replace("\\/", "/")
        pats = [
            r"/wday/cxs/([^/\"'<>\s]+)/([^/\"'<>\s]+)/jobs",
            r'["\']tenant["\']\s*:\s*["\']([^"\']+)["\']',
        ]
        m = re.search(pats[0], raw, re.I)
        if m:
            return host, clean(m.group(1)), clean(m.group(2))

        tm = re.search(pats[1], raw, re.I)
        if tm and site:
            return host, clean(tm.group(1)), site
    except Exception:
        pass

    return host, tenant, site


def _workday_hosts(host):
    """Candidate Workday public hosts for site migrations."""
    out = [host]
    # PBS has moved between wd5 and wd115 while keeping PBSCareers. Trying the
    # sibling host is safe because the tenant/site is still validated by cxs.
    if ".wd115.myworkdayjobs.com" in host:
        out.append(host.replace(".wd115.myworkdayjobs.com", ".wd5.myworkdayjobs.com"))
    elif ".wd5.myworkdayjobs.com" in host:
        out.append(host.replace(".wd5.myworkdayjobs.com", ".wd115.myworkdayjobs.com"))
    return list(dict.fromkeys(out))


def workday(src):
    host, tenant, site = _discover_workday_endpoint(src["URL"])

    if not host or not site:
        raise RuntimeError("Workday tenant/site not inferable")

    # For alias hosts, if tenant could not be discovered, try a small set of
    # source-specific known tenant aliases before declaring the source invalid.
    tenant_candidates = [tenant] if tenant and tenant != "myworkdaycenter" else []
    company = clean(src.get("Company", "")).lower()
    if company == "tribune":
        tenant_candidates += ["tribpub", "tpco", "tribunepublishing"]
    if company == "pbs":
        tenant_candidates += ["vhr-pbs", "pbs"]
    tenant_candidates = list(dict.fromkeys(x for x in tenant_candidates if x))

    last_error = None
    chosen = None

    for h in _workday_hosts(host):
        for ten in tenant_candidates:
            ep = f"https://{h}/wday/cxs/{ten}/{site}/jobs"
            try:
                probe = req(
                    "POST",
                    ep,
                    json={
                        "appliedFacets": {},
                        "limit": 20,
                        "offset": 0,
                        "searchText": "",
                    },
                    headers={"Content-Type": "application/json"},
                ).json()
                if isinstance(probe, dict) and ("jobPostings" in probe or "total" in probe):
                    chosen = (h, ten, ep, probe)
                    break
            except Exception as e:
                last_error = e
        if chosen:
            break

    if not chosen:
        # A valid Workday landing page that no longer exposes a working cxs
        # endpoint should not take down the entire feed.
        if company in {"tribune", "pbs"}:
            return generic(src)
        if last_error:
            raise last_error
        raise RuntimeError("Workday tenant/site not inferable")

    host, tenant, ep, first_payload = chosen
    out = []
    offset = 0
    payload = first_payload

    while True:
        d = payload if offset == 0 else req(
            "POST",
            ep,
            json={
                "appliedFacets": {},
                "limit": 20,
                "offset": offset,
                "searchText": "",
            },
            headers={"Content-Type": "application/json"},
        ).json()

        posts = d.get("jobPostings") or []
        if not posts:
            break

        for p in posts:
            ext = p.get("externalPath") or ""
            if not ext:
                continue

            try:
                info = req(
                    "GET",
                    f"https://{host}/wday/cxs/{tenant}/{site}{ext}",
                ).json().get("jobPostingInfo", {})
            except Exception:
                continue

            pd = pdate(info.get("postedOn") or p.get("postedOn"))
            if not pd or pd < CUTOFF:
                continue

            title = clean(info.get("title") or p.get("title"))
            desc = strip_html(info.get("jobDescription"))
            loc = clean(info.get("location") or p.get("locationsText"))
            url = info.get("externalUrl") or f"https://{host}/{site}{ext}"

            out.append(
                Job(
                    info.get("jobReqId") or hashlib.sha1(url.encode()).hexdigest()[:16],
                    title,
                    src["Company"],
                    desc,
                    pd,
                    jobtype(title, info.get("timeType", "")),
                    category(title, desc, src["Industry"], src["Company"]),
                    url,
                    src["URL"],
                    src["URL"],
                    "",
                    normalize_work_arrangement(desc, loc),
                    loc,
                    "",
                    infer_country(loc, src["Company"], desc),
                )
            )

        offset += len(posts)
        if offset >= int(d.get("total") or offset):
            break

    return out

def greenhouse(src):
    parts = [p for p in urlparse(src["URL"]).path.split("/") if p]
    board = parts[0] if parts else ""

    if not board:
        raise RuntimeError("Greenhouse board missing")

    d = req(
        "GET",
        f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
    ).json()

    out = []

    for p in d.get("jobs", []):
        pd = pdate(p.get("created_at")) or pdate(p.get("updated_at"))
        if not pd or pd < CUTOFF:
            continue

        title = clean(p.get("title"))
        desc = strip_html(p.get("content"))
        loc = clean((p.get("location") or {}).get("name"))
        url = p.get("absolute_url")

        out.append(
            Job(
                str(p.get("id")),
                title,
                src["Company"],
                desc,
                pd,
                jobtype(title),
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
                normalize_work_arrangement(desc, loc),
                loc,
                "",
                infer_country(loc, src["Company"], desc),
            )
        )

    return out




def _deep_values(obj, keys):
    """Yield values for matching dict keys anywhere in a JSON-like object."""
    wanted = {k.lower() for k in keys}
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).lower() in wanted:
                    yield v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)


def _first_deep(obj, keys, default=""):
    for v in _deep_values(obj, keys):
        if v not in (None, "", [], {}):
            if isinstance(v, dict):
                # ADP frequently wraps text in codeValue/shortName/longName.
                for k in ("longName", "shortName", "codeValue", "name", "value"):
                    if v.get(k) not in (None, ""):
                        return v.get(k)
            if not isinstance(v, (dict, list)):
                return v
    return default


def _adp_location(obj):
    """Best-effort public ADP location extraction."""
    locs = []
    for v in _deep_values(obj, {"requisitionLocations", "locations", "jobLocations"}):
        if not isinstance(v, list):
            continue
        for loc in v:
            if not isinstance(loc, dict):
                continue
            parts = []
            # Common ADP public API structures include nameCode and address.
            nc = loc.get("nameCode")
            if isinstance(nc, dict):
                parts.append(clean(nc.get("longName") or nc.get("shortName") or nc.get("codeValue") or ""))
            elif nc:
                parts.append(clean(str(nc)))
            for k in ("name", "locationName", "shortName"):
                if loc.get(k):
                    parts.append(clean(str(loc[k])))
            addr = loc.get("address") or loc.get("physicalAddress")
            if isinstance(addr, dict):
                for k in ("cityName", "city", "stateProvinceName", "stateProvinceCode", "countryName", "countryCode"):
                    if addr.get(k):
                        val = addr[k]
                        if isinstance(val, dict):
                            val = val.get("codeValue") or val.get("shortName") or val.get("longName") or ""
                        parts.append(clean(str(val)))
            text = ", ".join(dict.fromkeys(x for x in parts if x))
            if text:
                locs.append(text)
    if locs:
        return " / ".join(dict.fromkeys(locs))

    # Fallbacks used by some public career-center payload variants.
    return clean(str(_first_deep(obj, {
        "location", "locationName", "workLocation", "requisitionLocation",
        "formattedAddress", "addressLineOne"
    }, "")))


def _adp_description(obj):
    candidates = []
    for v in _deep_values(obj, {
        "requisitionDescription", "jobDescription", "description",
        "requisitionDescriptionText", "jobDescriptionText",
        "postingDescription", "externalDescription"
    }):
        if isinstance(v, str):
            t = strip_html(v)
            if len(t) > len(candidates[0]) if candidates else True:
                candidates.insert(0, t)
        elif isinstance(v, dict):
            for k in ("longName", "shortName", "codeValue", "text", "value"):
                if isinstance(v.get(k), str):
                    t = strip_html(v[k])
                    if t:
                        candidates.append(t)
    return max(candidates, key=len, default="")


def _adp_apply_url(src_url, job_id, detail=None):
    """Create a public ADP job-specific career-center URL without inventing a host."""
    u = urlparse(src_url)
    q = parse_qs(u.query, keep_blank_values=True)
    q["jobId"] = [str(job_id)]
    # Keep the external career-center identity already supplied by the employer.
    q.setdefault("ccId", ["19000101_000001"])
    q.setdefault("lang", ["en_US"])
    q.setdefault("type", ["JS"])

    # Some ADP payloads expose a job-worker/career-center posting id. Preserve it
    # when available because ADP often emits it as jwId on canonical detail URLs.
    if detail:
        jw = _first_deep(detail, {"jwId", "jobWorkerID", "jobWorkerId", "jobPostingID", "jobPostingId"}, "")
        if jw:
            q["jwId"] = [str(jw)]

    query = urlencode([(k, x) for k, vals in q.items() for x in vals])
    # Always point at ADP's public recruitment shell for the same tenant path.
    path = u.path
    if "/mdf/recruitment/" not in path:
        path = "/mascsr/default/mdf/recruitment/recruitment.html"
    return urlunparse((u.scheme or "https", u.netloc or "workforcenow.adp.com", path, "", query, ""))


def adp(src):
    """Dedicated ADP Workforce Now public career-center collector.

    ADP's public recruitment UI is JavaScript-rendered, but the career center
    exposes public staffing/v1/job-requisitions endpoints keyed by the `cid`
    already present in employer career URLs. This collector enumerates those
    requisitions, fetches details, and emits job-specific public apply URLs.
    """
    u = urlparse(src["URL"])
    qs = parse_qs(u.query)
    cid = clean((qs.get("cid") or [""])[0])
    if not cid:
        # myjobs.adp.com and branded ADP pages occasionally carry the career
        # center id in initial application state rather than the URL.
        landing = req("GET", src["URL"])
        text = html.unescape(landing.text or "").replace("\\/", "/")
        m = re.search(r'(?i)["\']?cid["\']?\s*[:=]\s*["\']([0-9a-f-]{20,})', text)
        if not m:
            m = re.search(r'(?i)[?&]cid=([0-9a-f-]{20,})', text)
        if m:
            cid = m.group(1)
    if not cid:
        # Newer branded ADP CX sites such as Hubbard use
        # myjobs.adp.com/{tenant}/cx/job-listing and do not expose a Workforce
        # Now `cid`. Use strict public-page discovery rather than treating the
        # employer as a crawler error.
        if "myjobs.adp.com" in u.netloc.lower():
            return generic(src)
        raise RuntimeError("ADP career-center cid not inferable")

    # Workforce Now public endpoint. The endpoint is public and distinct from
    # ADP's authenticated developer APIs used for internal HR integrations.
    api_host = "https://workforcenow.adp.com"
    base = api_host + "/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
    out = []
    skip = 1
    top = 20
    seen_ids = set()

    while skip <= 5000:
        payload = req("GET", base, params={"cid": cid, "$skip": skip, "$top": top}).json()
        posts = payload.get("jobRequisitions") or payload.get("requisitions") or []
        if not posts:
            break

        new_count = 0
        for p in posts:
            if not isinstance(p, dict):
                continue
            jid = clean(str(p.get("itemID") or p.get("jobId") or p.get("clientRequisitionID") or ""))
            if not jid or jid in seen_ids:
                continue
            seen_ids.add(jid)
            new_count += 1

            pd = pdate(p.get("postDate") or p.get("postingDate") or p.get("datePosted") or _first_deep(p, {"postDate", "postingDate", "datePosted"}, ""))
            # Fetching old details is unnecessary and materially slows the feed.
            if pd and pd < CUTOFF:
                continue

            try:
                detail = req("GET", f"{base}/{jid}", params={"cid": cid}).json()
            except Exception:
                detail = p

            pd = pd or pdate(_first_deep(detail, {"postDate", "postingDate", "datePosted", "postedDate"}, ""))
            if not pd or pd < CUTOFF:
                continue

            title = clean(str(
                p.get("requisitionTitle")
                or p.get("title")
                or _first_deep(detail, {"requisitionTitle", "jobTitle", "title"}, "")
            ))
            if not title:
                continue

            desc = _adp_description(detail)
            if len(desc) < 200:
                # Some detail variants keep the richer text in the list payload.
                desc = max(desc, _adp_description(p), key=len)
            if len(desc) < 200:
                continue

            loc = _adp_location(detail) or _adp_location(p)
            work_level = clean(str(
                _first_deep(detail, {"workLevelCode", "employmentType", "workerType", "timeType"}, "")
                or _first_deep(p, {"workLevelCode", "employmentType", "workerType", "timeType"}, "")
            ))
            url = _adp_apply_url(src["URL"], jid, detail)

            out.append(Job(
                jid,
                title,
                src["Company"],
                desc,
                pd,
                jobtype(title, work_level),
                category(title, desc, src["Industry"], src["Company"]),
                url,
                src["URL"],
                src["URL"],
                "",
                normalize_work_arrangement(desc, loc),
                loc,
                "",
                infer_country(loc, src["Company"], desc),
            ))

        total = payload.get("meta", {}).get("totalNumber") if isinstance(payload.get("meta"), dict) else None
        if total is not None:
            try:
                if skip - 1 + len(posts) >= int(total):
                    break
            except Exception:
                pass
        if len(posts) < top or new_count == 0:
            break
        skip += len(posts)

    return out


def _paylocity_board_guid(src_url):
    """Infer the Paylocity public job-feed GUID from a board/detail URL."""
    m = re.search(
        r"(?i)/recruiting/jobs/(?:all|list)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)",
        src_url,
    )
    if m:
        return m.group(1)

    # Some master rows point at a single Details page. Paylocity commonly
    # exposes the parent All/List board URL in the rendered HTML or script state.
    landing = req("GET", src_url)
    raw = html.unescape(landing.text or "").replace("\\/", "/")
    patterns = [
        r"(?i)/recruiting/jobs/(?:all|list)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|[?\"'])",
        r"(?i)[\"'](?:guid|jobFeedGuid|jobBoardGuid|careerSiteGuid)[\"']\s*[:=]\s*[\"']([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    ]
    for pat in patterns:
        m = re.search(pat, raw)
        if m:
            return m.group(1)
    return ""


def _paylocity_jobtype(title, item):
    vals = item.get("jobTypesArray") or item.get("JobTypesArray") or []
    if isinstance(vals, str):
        vals = [vals]
    joined = " ".join(str(x) for x in vals if x)
    joined += " " + clean(str(item.get("jobTypes") or item.get("JobTypes") or ""))
    return jobtype(title, joined)


def _paylocity_detail_job(src, detail_url, posted_date=None):
    """Parse one public Paylocity Details page into an MJR Job."""
    r = req("GET", detail_url)
    soup = BeautifulSoup(r.text, "html.parser")
    txt = clean(soup.get_text(" "))

    # Paylocity detail URLs contain the stable numeric requisition ID.
    m_id = re.search(r"(?i)/Jobs/Details/(\d+)(?:/|$)", detail_url)
    jid = m_id.group(1) if m_id else hashlib.sha1(detail_url.encode()).hexdigest()[:16]

    # Prefer headings, then URL slug as a last resort.
    headings = [clean(h.get_text(" ")) for tag in ("h2", "h3", "h1") for h in soup.find_all(tag)]
    headings = [h for h in headings if h and h.lower() not in {"apply", "description", "requirements", "job type"}]
    title = ""
    company_norm = re.sub(r"[^a-z0-9]+", " ", src["Company"].lower()).strip()
    for h in headings:
        h_norm = re.sub(r"[^a-z0-9]+", " ", h.lower()).strip()
        # Skip obvious employer/location headings. Prefer the job-title heading.
        if (company_norm and (company_norm in h_norm or h_norm in company_norm)):
            continue
        if h_norm in {"apply", "description", "requirements", "job type"}:
            continue
        if len(h) <= 180:
            title = h
            break
    if not title:
        parts = [p for p in urlparse(detail_url).path.split("/") if p]
        if parts:
            title = clean(parts[-1].replace("-", " "))
    if not title:
        return None

    # Job-posting date is most reliable on the board listing. If a direct
    # Details source is used, accept an explicit date from the detail page.
    pd = posted_date
    if not pd:
        m = re.search(
            r"(?i)(?:post(?:ed)?\s*date|date\s*posted|posted)\s*[:\-]?\s*"
            r"([A-Za-z]+\s+\d{1,2},\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2})",
            txt,
        )
        pd = pdate(m.group(1)) if m else None
    if not pd or pd < CUTOFF:
        return None

    # Prefer a semantic main/article container. Paylocity pages expose the
    # description and requirements in ordinary rendered HTML.
    main = soup.find("main") or soup.find("article") or soup
    desc = clean(main.get_text(" "))
    if len(desc) < 200:
        return None

    # Location commonly sits near the title and is also visible in page text.
    city = state = ""
    loc = ""
    loc_match = None
    for piece in soup.stripped_strings:
        piece = clean(piece)
        mloc = re.fullmatch(
            r"([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3}),\s*([A-Z]{2})",
            piece,
        )
        if mloc:
            loc_match = mloc
            break
    if loc_match:
        city, state = clean(loc_match.group(1)), loc_match.group(2)
        loc = f"{city}, {state}"

    jt_text = ""
    jt_match = re.search(
        r"(?i)job\s*type\s*(?:[:\-])?\s*"
        r"(full[- ]?time|part[- ]?time|internship|temporary|contract)",
        txt,
    )
    if jt_match:
        jt_text = jt_match.group(1)

    return Job(
        jid,
        title,
        src["Company"],
        desc,
        pd,
        jobtype(title, jt_text),
        category(title, desc, src["Industry"], src["Company"]),
        detail_url,
        src["URL"],
        src["URL"],
        "",
        normalize_work_arrangement(desc, loc),
        city or loc,
        state,
        infer_country(loc, src["Company"], desc),
    )


def _paylocity_board_jobs(src, start_url=None):
    """Enumerate published jobs from Paylocity's public rendered board.

    The public All/List board exposes job detail links and posted dates even
    when the optional Job Feed API key is not available to us.
    """
    start_url = start_url or src["URL"]
    r = req("GET", start_url)
    soup = BeautifulSoup(r.text, "html.parser")

    # If the configured source is one Details page (Hope Media currently is),
    # follow Paylocity's "View All Jobs"/List/All link first.
    if re.search(r"(?i)/Jobs/Details/\d+", start_url):
        board_url = ""
        for a in soup.find_all("a", href=True):
            h = urljoin(start_url, a["href"])
            label = clean(a.get_text(" ")).lower()
            if re.search(r"(?i)/recruiting/jobs/(?:all|list)/", h) or "view all jobs" in label:
                board_url = h
                break
        if board_url:
            r = req("GET", board_url)
            soup = BeautifulSoup(r.text, "html.parser")
            start_url = board_url

    candidates = {}
    date_re = re.compile(r"\b(\d{1,2}/\d{1,2}/20\d{2})\b")

    for a in soup.find_all("a", href=True):
        h = urljoin(start_url, a["href"])
        if not re.search(r"(?i)/recruiting/jobs/details/\d+", h):
            continue

        # Locate the smallest nearby container that includes the board's posted
        # date. This pairs each detail URL with its listing date.
        pd = None
        node = a
        for _ in range(7):
            node = getattr(node, "parent", None)
            if node is None:
                break
            chunk = clean(node.get_text(" "))
            dm = date_re.search(chunk)
            if dm:
                pd = pdate(dm.group(1))
                break

        # Avoid pulling old jobs when the board provides a date.
        if pd and pd < CUTOFF:
            continue
        candidates[h] = pd

    # Script/application state sometimes contains detail URLs even when links
    # are dynamically inserted. Add those as candidates too.
    raw = html.unescape(r.text or "").replace("\\/", "/")
    for m in re.finditer(
        r'https?://recruiting\.paylocity\.com/recruiting/jobs/details/\d+/[^"\'<>\\s]+',
        raw,
        re.I,
    ):
        candidates.setdefault(m.group(0).rstrip(".,;)"), None)

    out = []
    for detail_url, pd in list(candidates.items())[:500]:
        try:
            j = _paylocity_detail_job(src, detail_url, pd)
            if j:
                out.append(j)
        except Exception:
            continue

    # Direct Details source with no discoverable board: at least parse that job.
    if not out and re.search(r"(?i)/Jobs/Details/\d+", src["URL"]):
        try:
            j = _paylocity_detail_job(src, src["URL"], None)
            if j:
                out.append(j)
        except Exception:
            pass

    return out


def paylocity(src):
    """Paylocity collector: feed when available, rendered public board fallback."""
    guid = _paylocity_board_guid(src["URL"])
    feed_posts = []

    # The documented Job Feed requires a GUID API key. Some public board GUIDs
    # happen to work, others do not, so treat the feed as an optimization only.
    if guid:
        endpoints = [
            f"https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{guid}",
            f"https://recruiting.paylocity.com/recruiting/api/feed/jobs/{guid}",
        ]
        for endpoint in endpoints:
            try:
                r = req("GET", endpoint, headers={"Accept": "application/json"})
                payload = r.json()
                if isinstance(payload, dict):
                    feed_posts = payload.get("jobs") or payload.get("Jobs") or []
                elif isinstance(payload, list):
                    feed_posts = payload
                if feed_posts:
                    break
            except Exception:
                continue

    out = []
    for p in feed_posts:
        if not isinstance(p, dict):
            continue
        pd = pdate(p.get("publishedDate") or p.get("PublishedDate") or p.get("createdUtc") or p.get("CreatedUtc"))
        if not pd or pd < CUTOFF:
            continue
        jid = clean(str(p.get("jobId") or p.get("JobId") or ""))
        title = clean(str(p.get("title") or p.get("Title") or ""))
        if not jid or not title:
            continue
        desc = strip_html(str(p.get("description") or p.get("Description") or ""))
        requirements = strip_html(str(p.get("requirements") or p.get("Requirements") or ""))
        if requirements and requirements.lower() not in desc.lower():
            desc = clean(desc + " Requirements: " + requirements)
        if len(desc) < 200:
            continue
        jl = p.get("jobLocation") or p.get("JobLocation") or {}
        if not isinstance(jl, dict):
            jl = {}
        city = clean(str(jl.get("city") or jl.get("City") or ""))
        state = clean(str(jl.get("state") or jl.get("State") or ""))
        loc_name = clean(str(jl.get("locationDisplayName") or jl.get("LocationDisplayName") or jl.get("name") or jl.get("Name") or ""))
        loc = ", ".join(x for x in [city, state] if x) or loc_name
        detail_url = clean(str(p.get("displayUrl") or p.get("DisplayUrl") or ""))
        apply_url = clean(str(p.get("applyUrl") or p.get("ApplyUrl") or ""))
        url = detail_url or apply_url
        if not url:
            continue
        out.append(Job(
            jid, title, src["Company"], desc, pd, _paylocity_jobtype(title, p),
            category(title, desc, src["Industry"], src["Company"]), url,
            src["URL"], src["URL"], "", normalize_work_arrangement(desc, loc),
            city or loc, state, infer_country(loc, src["Company"], desc),
        ))

    if out:
        return out
    return _paylocity_board_jobs(src)


def _dayforce_candidates(src_url):
    """Infer Dayforce tenant/company identifiers and career-site board code."""
    u = urlparse(src_url)
    host = (u.netloc or "").lower()
    parts = [p for p in u.path.split("/") if p]

    tenants = []
    board = ""

    # Legacy tenant hosts such as can241.dayforcehcm.com are often the API
    # CompanyName even when the public career-site path contains another slug.
    if host.endswith(".dayforcehcm.com"):
        sub = host.split(".")[0]
        if sub not in {"www", "jobs", "careers"}:
            tenants.append(sub)

    # New career URLs normally look like:
    # /en-US/<tenant>/<board> or /<tenant>/<board>
    locale_re = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
    for i, part in enumerate(parts):
        if locale_re.match(part) and i + 1 < len(parts):
            tenants.append(parts[i + 1])
            if i + 2 < len(parts) and parts[i + 2].lower() != "site":
                board = parts[i + 2]
            break

    # Older CandidatePortal form:
    # /CandidatePortal/en-US/<tenant>/Site/<board>
    for i, part in enumerate(parts):
        if part.lower() == "candidateportal" and i + 2 < len(parts):
            maybe_locale = parts[i + 1]
            if locale_re.match(maybe_locale) and i + 2 < len(parts):
                tenants.append(parts[i + 2])
        if part.lower() == "site" and i + 1 < len(parts):
            board = parts[i + 1]

    # Common current form without a locale prefix.
    if not tenants and len(parts) >= 2:
        if parts[0].lower() not in {"candidateportal", "en-us", "fr-ca"}:
            tenants.append(parts[0])
            board = board or parts[1]

    # Board code can usually be read from the last path segment.
    if not board and parts:
        tail = parts[-1]
        if tail.lower() not in {"candidateportal", "jobs", "site"}:
            board = tail

    ded = []
    for t in tenants:
        t = clean(t)
        if t and t.lower() not in {x.lower() for x in ded}:
            ded.append(t)
    return ded, clean(board)


def _dayforce_payload_rows(r):
    """Parse Dayforce JobFeeds JSON or XML response into dictionaries."""
    ctype = (r.headers.get("Content-Type") or "").lower()
    text = r.text or ""

    if "json" in ctype or text.lstrip().startswith(("[", "{")):
        data = r.json()
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("JobPostings", "jobPostings", "Jobs", "jobs", "Items", "items"):
                v = data.get(key)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
            return [data] if data else []

    rows = []
    try:
        root = ET.fromstring(text)
    except Exception:
        return rows

    # Dayforce XML feeds have historically used JobPosting elements, with or
    # without namespaces. Convert each element's direct children to a dict.
    for elem in root.iter():
        tag = elem.tag.split("}")[-1].lower()
        if tag not in {"jobposting", "job", "item"}:
            continue
        row = {}
        for child in list(elem):
            k = child.tag.split("}")[-1]
            row[k] = "".join(child.itertext()).strip()
        if row:
            rows.append(row)
    return rows


def _dayforce_get(d, *names):
    if not isinstance(d, dict):
        return ""
    lower = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, ""):
            return v
    return ""


def _dayforce_jobtype(title, item):
    raw = clean(str(_dayforce_get(
        item, "EmploymentIndicator", "EmploymentType", "JobType", "PayClass", "PayType"
    )))
    s = f" {title} {raw} ".lower()
    if "intern" in s:
        return "Internship"
    if re.search(r"\bpart[- ]?time\b", s):
        return "Part Time"
    if re.search(r"\b(temp|temporary|seasonal)\b", s):
        return "Temporary"
    if re.search(r"\b(contract|contractor)\b", s):
        return "Contract"
    return "Full Time"


def dayforce(src):
    """Dedicated Dayforce collector using its anonymous external JobFeeds API."""
    tenants, board = _dayforce_candidates(src["URL"])
    if not tenants:
        raise RuntimeError("Dayforce tenant not inferable")

    rows = []
    last_error = None
    used_tenant = ""

    # Official Dayforce external job-board feed. Try inferred tenant candidates
    # because legacy branded URLs can expose both a host tenant and site slug.
    for tenant in tenants:
        endpoint = f"https://www.dayforcehcm.com/api/{tenant}/V1/JobFeeds"
        param_sets = []
        if board:
            param_sets.append({
                "includeActivePostingOnly": "true",
                "internalJobBoardCode": board,
            })
        param_sets.append({"includeActivePostingOnly": "true"})

        for params in param_sets:
            try:
                r = req(
                    "GET",
                    endpoint,
                    params=params,
                    headers={"Accept": "application/json, application/xml, text/xml;q=0.9"},
                )
                candidate_rows = _dayforce_payload_rows(r)
                if candidate_rows:
                    rows = candidate_rows
                    used_tenant = tenant
                    break
            except Exception as e:
                last_error = e
                continue
        if rows:
            break

    # If the external feed is disabled for a customer, preserve the existing
    # structured HTML/JSON-LD fallback rather than aborting the source.
    if not rows:
        try:
            fallback = ats_html(src)
            if fallback:
                return fallback
        except Exception:
            pass
        if last_error:
            return []
        return []

    out = []
    seen = set()
    for item in rows:
        pd = pdate(_dayforce_get(item, "DatePosted", "PostedDate", "DateCreated", "LastUpdated"))
        if not pd or pd < CUTOFF:
            continue

        title = clean(str(_dayforce_get(item, "Title", "JobTitle")))
        if not title:
            continue

        desc_raw = str(_dayforce_get(item, "Description", "JobDescription", "PostingDescription"))
        desc = strip_html(desc_raw)
        if len(desc) < 80:
            continue

        city = clean(str(_dayforce_get(item, "City")))
        state = clean(str(_dayforce_get(item, "State", "StateProvince")))
        country = clean(str(_dayforce_get(item, "Country")))
        postal = clean(str(_dayforce_get(item, "PostalCode", "ZipCode")))
        loc = ", ".join(x for x in [city, state] if x)
        if not loc:
            loc = clean(str(_dayforce_get(item, "Location", "LocationName")))

        details_url = clean(str(_dayforce_get(item, "JobDetailsUrl", "JobDetailUrl", "DisplayUrl")))
        apply_url = clean(str(_dayforce_get(item, "ApplyUrl", "ApplicationUrl")))

        # Never emit the localhost placeholder shown in Dayforce's sample data.
        url = details_url if details_url and "localhost" not in details_url.lower() else apply_url
        if not url:
            continue
        if url.startswith("/"):
            url = urljoin(src["URL"], url)

        jid = clean(str(_dayforce_get(
            item, "ReferenceNumber", "JobPostingId", "JobId", "RequisitionId", "ParentRequisitionCode"
        )))
        if not jid:
            m = re.search(r"(?i)(?:jobId=|/Posting/View/|/jobs?/)([A-Za-z0-9_-]+)", url)
            jid = m.group(1) if m else hashlib.sha1(url.encode()).hexdigest()[:16]

        ded_key = (jid, url)
        if ded_key in seen:
            continue
        seen.add(ded_key)

        jt = _dayforce_jobtype(title, item)
        country_out = country or infer_country(loc, src["Company"], desc)

        out.append(Job(
            jid,
            title,
            src["Company"],
            desc,
            pd,
            jt,
            category(title, desc, src["Industry"], src["Company"]),
            url,
            src["URL"],
            src["URL"],
            "",
            normalize_work_arrangement(desc, loc),
            city or loc,
            state,
            country_out,
        ))

    return out

def _jsonld_jobs(soup):
    """Return JobPosting JSON-LD objects found on a detail page."""
    found = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list):
                stack.extend(obj)
                continue
            if not isinstance(obj, dict):
                continue
            graph = obj.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)
            typ = obj.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if any(str(x).lower() == "jobposting" for x in types if x):
                found.append(obj)
    return found


def _location_from_jsonld(jp):
    locs = jp.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    parts = []
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        addr = loc.get("address") or {}
        if not isinstance(addr, dict):
            continue
        bits = [
            clean(addr.get("addressLocality")),
            clean(addr.get("addressRegion")),
            clean(addr.get("addressCountry")),
        ]
        val = ", ".join(x for x in bits if x)
        if val:
            parts.append(val)
    if parts:
        return " / ".join(dict.fromkeys(parts))

    applicant = jp.get("applicantLocationRequirements")
    if isinstance(applicant, dict):
        applicant = [applicant]
    if isinstance(applicant, list):
        vals = []
        for x in applicant:
            if isinstance(x, dict):
                vals.append(clean(x.get("name")))
        vals = [x for x in vals if x]
        if vals:
            return " / ".join(vals)
    return ""


def _employment_text(jp):
    et = jp.get("employmentType") or ""
    if isinstance(et, list):
        et = " ".join(str(x) for x in et)
    return clean(str(et))


def _job_from_detail(src, url, html_text):
    """Parse a detail page, preferring schema.org JobPosting."""
    soup = BeautifulSoup(html_text, "html.parser")
    json_jobs = _jsonld_jobs(soup)

    if json_jobs:
        jp = json_jobs[0]
        title = clean(jp.get("title") or "")
        desc_html = jp.get("description") or ""
        desc = strip_html(desc_html)
        pd = pdate(jp.get("datePosted"))
        valid_through = pdate(jp.get("validThrough"))
        loc = _location_from_jsonld(jp)
        canonical = clean(jp.get("url") or "")
        apply_url = canonical if canonical.startswith("http") else url
        ident = jp.get("identifier") or {}
        jid = ""
        if isinstance(ident, dict):
            jid = clean(str(ident.get("value") or ident.get("name") or ""))
        elif ident:
            jid = clean(str(ident))

        if not title:
            h1 = soup.find("h1")
            title = clean(h1.get_text(" ") if h1 else "")
        if not desc:
            main = soup.find("main") or soup.find("article") or soup
            desc = clean(main.get_text(" "))

        if pd and pd >= CUTOFF and title and len(desc) >= 200:
            return Job(
                jid or hashlib.sha1(apply_url.encode()).hexdigest()[:16],
                title,
                src["Company"],
                desc,
                pd,
                jobtype(title, _employment_text(jp)),
                category(title, desc, src["Industry"], src["Company"]),
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

    # HTML fallback for ATS pages that do not expose JSON-LD.
    txt = clean(soup.get_text(" "))
    h1 = soup.find("h1")
    title = clean(h1.get_text(" ") if h1 else (soup.title.get_text(" ") if soup.title else ""))

    date_patterns = [
        r"(?:date posted|posted date|posted|posting date|published)\s*:?\s*"
        r"([A-Za-z]+\s+\d{1,2},\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2}|"
        r"\d{4}-\d{2}-\d{2}|\d+\s+days?\s+ago)",
    ]
    pd = None
    for pat in date_patterns:
        m = re.search(pat, txt, re.I)
        if m:
            pd = pdate(m.group(1))
            if pd:
                break
    if not pd or pd < CUTOFF:
        return None

    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find(attrs={"class": re.compile(r"(job.?description|job.?detail|posting)", re.I)})
        or soup
    )
    desc = clean(main.get_text(" "))
    if not title or len(desc) < 200:
        return None

    return Job(
        hashlib.sha1(url.encode()).hexdigest()[:16],
        title,
        src["Company"],
        desc,
        pd,
        jobtype(title, txt),
        category(title, desc, src["Industry"], src["Company"]),
        url,
        src["URL"],
        src["URL"],
        "",
        normalize_work_arrangement(desc, txt),
        "",
        "",
        infer_country(txt, src["Company"], desc),
    )


ATS_JOB_HINTS = {
    "paylocity": [
        r"/recruiting/jobs/details/\d+", r"/Recruiting/Jobs/Details/\d+",
        r"/recruiting/jobs/\d+", r"jobId=\d+",
    ],
    "icims": [
        r"/jobs/\d+/", r"/jobs/\d+$", r"/jobs/\d+/.+?/job",
        r"jobid=\d+",
    ],
    "jazzhr": [
        r"/apply/[A-Za-z0-9_-]+/",
        r"/apply/[A-Za-z0-9_-]+$",
    ],
    "applytojob": [
        r"/apply/[A-Za-z0-9_-]+/",
        r"/apply/[A-Za-z0-9_-]+$",
    ],
    "dayforce": [
        r"/job/", r"/jobs/", r"/CandidatePortal/.+?/jobs/",
        r"/candidateportal/.+?/job/", r"jobPostingId=",
    ],
    "ukg": [
        r"/JobBoard/.+?/OpportunityDetail\?opportunityId=",
        r"/JobBoard/.+?/JobDetail/", r"opportunityId=[0-9a-f-]+",
        r"/job/",
    ],
    "ultipro": [
        r"/JobBoard/.+?/OpportunityDetail\?opportunityId=",
        r"/JobBoard/.+?/JobDetail/",
        r"/job/",
    ],
    "adp": [
        r"/cx/job-details", r"/job-details", r"/cx/job-detail",
        r"reqId=[A-Za-z0-9_-]+", r"requisitionId=[A-Za-z0-9_-]+",
        r"[?&]r=\d+",
    ],
    "oracle": [
        r"/job/", r"/jobs/", r"/requisitions/",
        r"/sites/.+?/job/", r"/sites/.+?/requisitions/",
        r"requisitionId=",
    ],
    "taleo": [
        r"jobdetail",
        r"job=",
        r"/job/",
    ],
    "breezy": [
        r"/p/[A-Za-z0-9_-]+/",
        r"/p/[A-Za-z0-9_-]+$",
    ],
    "paycom": [
        r"/jobs/\d+",
        r"jobid=\d+",
        r"job=\d+",
    ],
    "rippling": [
        r"/jobs/",
        r"/job/",
    ],
    "jobscore": [
        r"/jobs/",
        r"/job/",
    ],
    "paycor": [
        r"career/JobIntroduction\.action",
        r"jobId=",
        r"/job/",
    ],
    "isolved": [
        r"/jobs/\d+",
        r"/job/",
    ],
    "betterteam": [
        r"/jobs/",
        r"/job/",
    ],
}


def _ats_family(src):
    a = clean(src.get("ATS", "")).lower()
    u = clean(src.get("URL", "")).lower()
    joined = a + " " + u
    if "paylocity" in joined:
        return "paylocity"
    if "icims" in joined:
        return "icims"
    if "applytojob" in joined or "jazzhr" in joined:
        return "jazzhr"
    if "dayforce" in joined:
        return "dayforce"
    if "ultipro" in joined or "ukg" in joined:
        return "ukg"
    if "adp" in joined:
        return "adp"
    if "oracle" in joined:
        return "oracle"
    if "taleo" in joined:
        return "taleo"
    if "breezy" in joined:
        return "breezy"
    if "paycom" in joined:
        return "paycom"
    if "rippling" in joined:
        return "rippling"
    if "jobscore" in joined:
        return "jobscore"
    if "paycor" in joined:
        return "paycor"
    if "isolved" in joined:
        return "isolved"
    if "betterteam" in joined:
        return "betterteam"
    return ""


KNOWN_ATS_HOST_TOKENS = (
    "adp.com", "paylocity.com", "dayforcehcm.com", "dayforce.com",
    "icims.com", "ultipro.com", "ukg.com", "oraclecloud.com",
    "taleo.net", "breezy.hr", "applytojob.com", "jazz.co",
    "paycomonline.net", "rippling-ats.com", "jobscore.com",
    "recruitingbypaycor.com", "isolvedhire.com", "betterteam.com",
    "myworkdayjobs.com", "greenhouse.io",
)


def _known_ats_url(url):
    host = (urlparse(url).netloc or "").lower()
    return any(tok in host for tok in KNOWN_ATS_HOST_TOKENS)


def _candidate_urls_from_text(base, raw, patterns):
    """Extract absolute and escaped/relative ATS job URLs embedded in scripts."""
    found = set()
    text = html.unescape(raw or "").replace("\\/", "/")

    # Absolute URLs, including JSON-escaped strings after slash normalization.
    for m in re.finditer(r'https?://[^"\'<>\s]+', text):
        u = m.group(0).rstrip("\\,;)")
        if any(re.search(p, u, re.I) for p in patterns):
            found.add(u)

    # Relative URLs are common in React/Angular state blobs.
    for m in re.finditer(r'(?P<q>["\'])(?P<u>/[^"\']{4,500})(?P=q)', text):
        rel = m.group("u")
        u = urljoin(base, rel)
        if any(re.search(p, u, re.I) for p in patterns):
            found.add(u)

    return found


def _listing_next_links(page, soup):
    out = set()
    for a in soup.find_all("a", href=True):
        h = urljoin(page, a["href"])
        label = clean(a.get_text(" ")).lower()
        low = h.lower()
        if (
            re.search(r"\b(next|more jobs|load more|older)\b", label)
            or re.search(r"[?&](page|p|start|offset|from)=\d+", low)
            or re.search(r"/page/\d+", low)
        ):
            out.add(h)
    return out



def _icims_date_from_html(soup, raw):
    """Find an explicit iCIMS posting date without guessing from requisition ID."""
    # Structured/meta attributes used by different iCIMS career-center themes.
    attrs_to_check = (
        ("meta", {"itemprop": re.compile(r"datePosted", re.I)}, "content"),
        ("meta", {"property": re.compile(r"datePosted|published_time", re.I)}, "content"),
        ("meta", {"name": re.compile(r"datePosted|date_posted|posted", re.I)}, "content"),
        ("time", {"datetime": True}, "datetime"),
    )
    for tag, attrs, attr in attrs_to_check:
        for node in soup.find_all(tag, attrs=attrs):
            val = clean(node.get(attr) or node.get_text(" "))
            d = pdate(val)
            if d:
                return d

    # Hydrated application state / inline scripts can expose the date even when
    # the visible iCIMS template does not print it.
    normalized = html.unescape(raw or "").replace("\\/", "/")
    patterns = [
        r'["\'](?:datePosted|date_posted|postedDate|postingDate|createdDate)["\']\s*:\s*["\']([^"\']+)["\']',
        r'(?:Date Posted|Posted Date|Posting Date)\s*</?[^>]*>?\s*:?\s*'
        r'([A-Za-z]+\s+\d{1,2},\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2}|\d{4}-\d{2}-\d{2})',
    ]
    for pat in patterns:
        for m in re.finditer(pat, normalized, re.I):
            d = pdate(m.group(1))
            if d:
                return d
    return None


def _icims_detail(src, url, raw):
    """Parse an iCIMS detail page, requiring an explicit qualifying post date."""
    # First use the shared schema.org parser. Many iCIMS tenants expose
    # JobPosting JSON-LD even when the date is not visible in page text.
    j = _job_from_detail(src, url, raw)
    if j:
        return j

    soup = BeautifulSoup(raw, "html.parser")
    pd = _icims_date_from_html(soup, raw)
    if not pd or pd < CUTOFF:
        return None

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ") if h1 else "")
    if not title:
        title_node = soup.find(attrs={"class": re.compile(r"(iCIMS_Header|job.?title|title)", re.I)})
        title = clean(title_node.get_text(" ") if title_node else "")
    if not title:
        return None

    # iCIMS commonly labels location/type/ID in the header/profile fields.
    txt = clean(soup.get_text(" "))
    loc = ""
    loc_node = soup.find(string=re.compile(r"Job Locations?", re.I))
    if loc_node:
        parent = loc_node.parent
        if parent:
            block = clean(parent.parent.get_text(" ") if parent.parent else parent.get_text(" "))
            m = re.search(r"Job Locations?\s+(.+?)(?:\s+ID\b|\s+Category\b|\s+Type\b|$)", block, re.I)
            if m:
                loc = clean(m.group(1))

    jid = ""
    m = re.search(r"\bID\s+((?:20\d{2}-)?\d{3,})\b", txt, re.I)
    if m:
        jid = clean(m.group(1))
    if not jid:
        m = re.search(r"/jobs/(\d+)", url, re.I)
        if m:
            jid = m.group(1)

    main = (
        soup.find(id=re.compile(r"(iCIMS_JobContent|job.?content|job.?description)", re.I))
        or soup.find(attrs={"class": re.compile(r"(iCIMS_JobContent|job.?description|job.?detail)", re.I)})
        or soup.find("main")
        or soup
    )
    desc = clean(main.get_text(" "))
    if len(desc) < 200:
        return None

    canonical = url.split("#", 1)[0]
    return Job(
        jid or hashlib.sha1(canonical.encode()).hexdigest()[:16],
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


def icims(src):
    """Dedicated public iCIMS Career Center collector.

    iCIMS search pages are server-rendered and paginated with `pr=`. We
    enumerate those pages directly, then parse each real /jobs/<id>/.../job
    detail page. We never infer a posting date from the requisition number.
    """
    start = src["URL"]
    parsed = urlparse(start)
    base = f"{parsed.scheme}://{parsed.netloc}"
    # Force the public search view while preserving tenant-specific query args.
    if "/jobs/search" not in parsed.path.lower():
        start = base + "/jobs/search?ss=1"

    queue = [start]
    seen_pages = set()
    links = set()

    while queue and len(seen_pages) < 60 and len(links) < 2500:
        page = queue.pop(0)
        if page in seen_pages:
            continue
        seen_pages.add(page)

        try:
            r = req("GET", page)
        except Exception:
            # A broken iSolved board must not create a crawler-wide error.
            continue

        final_url = str(getattr(r, "url", "") or page)
        final_host = urlparse(final_url).netloc.lower()
        final_path = urlparse(final_url).path.lower()

        # Some retired/public iSolved boards redirect crawlers to the admin
        # portal or /Help. Those destinations are not job boards and should be
        # treated as non-enumerable rather than followed or surfaced as errors.
        if (
            "admin.isolvedhire.com" in final_host
            or final_path.startswith("/help")
            or "/help/" in final_path
        ):
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        for a in soup.find_all("a", href=True):
            h = urljoin(page, a["href"])
            hp = urlparse(h)
            if hp.netloc.lower() != parsed.netloc.lower():
                continue
            if re.search(r"/jobs/\d+/.+?/job(?:[/?#]|$)", h, re.I):
                links.add(h.split("#", 1)[0])
                continue
            # iCIMS pagination uses pr=0, pr=1, ... rather than page=.
            if "/jobs/search" in hp.path.lower() and re.search(r"[?&]pr=\d+", h, re.I):
                if h not in seen_pages:
                    queue.append(h)

        # Some tenants hydrate links into scripts.
        links.update(
            u for u in _candidate_urls_from_text(
                page, r.text, [r"/jobs/\d+/.+?/job(?:[/?#]|$)"]
            )
            if urlparse(u).netloc.lower() == parsed.netloc.lower()
        )

    out = []
    for url in sorted(links):
        try:
            rr = req("GET", url)
            j = _icims_detail(src, url, rr.text)
            if j:
                out.append(j)
        except Exception:
            # One closed iCIMS requisition must not abort the employer.
            continue
    return out


def _ukg_parts(url):
    """Return (base, tenant, board) for a public UKG/UltiPro Recruiting board."""
    p = urlparse(url)
    m = re.search(
        r"^/([^/]+)/JobBoard/([0-9a-f-]{36})(?:/|$)",
        p.path,
        re.I,
    )
    if not m:
        return None
    base = f"{p.scheme or 'https'}://{p.netloc}"
    return base, m.group(1), m.group(2)


def _ukg_posted_date(raw, soup=None):
    """Extract UKG's explicit Posted date; never substitute Updated."""
    s = html.unescape(raw or "").replace("\\/", "/")
    patterns = [
        r"Opportunity\.OpportunityDetail\.PostedLabel\s*:?\s*"
        r"(?:</?[^>]+>\s*)*"
        r"([A-Za-z]+\s+\d{1,2},\s+20\d{2})",
        r"(?:Posted|Date Posted|Posted Date)\s*:?\s*"
        r"(?:</?[^>]+>\s*)*"
        r"([A-Za-z]+\s+\d{1,2},\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2}|\d{4}-\d{2}-\d{2})",
        r'["\'](?:postedDate|datePosted|postingDate)["\']\s*:\s*["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, s, re.I)
        if m:
            d = pdate(strip_html(m.group(1)))
            if d:
                return d

    if soup:
        # Some tenants render the resource key as a label followed by a sibling.
        for node in soup.find_all(string=re.compile(r"PostedLabel|Date Posted|Posted Date", re.I)):
            parent = node.parent
            if not parent:
                continue
            candidates = []
            if parent.next_sibling:
                candidates.append(str(parent.next_sibling))
            if parent.parent:
                candidates.append(parent.parent.get_text(" "))
            for val in candidates:
                m = re.search(
                    r"([A-Za-z]+\s+\d{1,2},\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2}|\d{4}-\d{2}-\d{2})",
                    clean(val),
                    re.I,
                )
                if m:
                    d = pdate(m.group(1))
                    if d:
                        return d
    return None


def _ukg_location(text):
    """Best-effort location from UKG's Locations block."""
    t = clean(text)
    # Prefer a conventional city/state pair from the location block.
    m = re.search(r"\b([A-Za-z .'\-]+),\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\b", t)
    if m:
        return clean(f"{m.group(1)}, {m.group(2)}")
    m = re.search(r"\b([A-Za-z .'\-]+),\s*([A-Z]{2})\b", t)
    if m:
        return clean(f"{m.group(1)}, {m.group(2)}")
    if re.search(r"\bRemote\b", t, re.I):
        return "Remote"
    if re.search(r"\bNationwide\b", t, re.I):
        return "Nationwide"
    return ""


def _ukg_detail(src, url, raw):
    """Parse one public UKG Pro Recruiting OpportunityDetail page."""
    soup = BeautifulSoup(raw, "html.parser")
    pd = _ukg_posted_date(raw, soup)
    if not pd or pd < CUTOFF:
        return None

    # UKG detail pages put the actual title in H1.
    h1 = soup.find("h1")
    title = clean(h1.get_text(" ") if h1 else "")
    if not title:
        title_node = soup.find(attrs={"class": re.compile(r"(job.?title|opportunity.?title|title)", re.I)})
        title = clean(title_node.get_text(" ") if title_node else "")
    if not title or "pagetitle" in title.lower():
        # Resource-key templates sometimes leave the H1 untranslated in raw HTML.
        m = re.search(r'<h1[^>]*>\s*([^<]{3,200})\s*</h1>', raw, re.I | re.S)
        title = clean(strip_html(m.group(1))) if m else title
    if not title or "pagetitle" in title.lower():
        return None

    full_text = clean(soup.get_text(" "))

    # Requisition number is stable and preferable to the opportunity GUID.
    jid = ""
    m = re.search(
        r"Opportunity\.Opportunities\.RequisitionNumber\s*:?\s*"
        r"([A-Z0-9_-]{4,})",
        full_text,
        re.I,
    )
    if m:
        jid = clean(m.group(1))
    if not jid:
        q = parse_qs(urlparse(url).query)
        jid = clean((q.get("opportunityId") or [""])[0])
    if not jid:
        jid = hashlib.sha1(url.encode()).hexdigest()[:16]

    # Description is usually under JobDetails/Description; use the relevant
    # content container when identifiable and otherwise the main page.
    main = (
        soup.find(attrs={"class": re.compile(r"(opportunity.?detail|job.?details|job.?description)", re.I)})
        or soup.find(id=re.compile(r"(opportunity.?detail|job.?details|job.?description)", re.I))
        or soup.find("main")
        or soup
    )
    desc = clean(main.get_text(" "))
    if len(desc) < 200:
        return None

    # Pull location from the Locations block when possible, then fall back.
    loc_text = ""
    loc_key = soup.find(string=re.compile(r"CompanyInformation\.Locations|Job Locations?", re.I))
    if loc_key and loc_key.parent and loc_key.parent.parent:
        loc_text = clean(loc_key.parent.parent.get_text(" "))
    loc = _ukg_location(loc_text or full_text)

    jt_context = full_text
    canonical = url.split("#", 1)[0]
    return Job(
        jid,
        title,
        src["Company"],
        desc,
        pd,
        jobtype(title, jt_context),
        category(title, desc, src["Industry"], src["Company"]),
        canonical,
        src["URL"],
        src["URL"],
        "",
        normalize_work_arrangement(desc, loc or jt_context),
        loc,
        "",
        infer_country(loc or jt_context, src["Company"], desc),
    )


def ukg(src):
    """Dedicated UKG Pro Recruiting / UltiPro public-board collector.

    Public UKG boards use:
      /{tenant}/JobBoard/{board-guid}/OpportunityDetail?opportunityId={guid}

    The listing is JavaScript-heavy, but the opportunity URLs are exposed in
    anchors/application state on public board pages. Enumerate those URLs,
    follow listing pagination/search links, then parse each real detail page.
    """
    parts = _ukg_parts(src["URL"])
    if not parts:
        return ats_html(src)

    base, tenant, board = parts
    board_root = f"{base}/{tenant}/JobBoard/{board}"
    start = board_root + "/?q=&o=postedDateDesc&w=&wc=&we=&wpst="

    queue = [start]
    seen_pages = set()
    detail_urls = set()

    detail_pat = (
        rf"/{re.escape(tenant)}/JobBoard/{re.escape(board)}/"
        r"OpportunityDetail\?[^\"'<>\s]*opportunityId=[0-9a-f-]{36}"
    )

    while queue and len(seen_pages) < 80 and len(detail_urls) < 4000:
        page = queue.pop(0)
        key = page.rstrip("/")
        if key in seen_pages:
            continue
        seen_pages.add(key)

        r = req("GET", page)
        raw = html.unescape(r.text or "").replace("\\/", "/")
        soup = BeautifulSoup(r.text, "html.parser")

        # Visible anchors.
        for a in soup.find_all("a", href=True):
            h = urljoin(page, a["href"])
            hp = urlparse(h)
            if hp.netloc.lower() != urlparse(base).netloc.lower():
                continue
            if "opportunitydetail" in hp.path.lower():
                q = parse_qs(hp.query)
                oid = clean((q.get("opportunityId") or [""])[0])
                if re.fullmatch(r"[0-9a-f-]{36}", oid, re.I):
                    detail_urls.add(h.split("#", 1)[0])
                    continue

            # Follow UKG board-page controls/pagination while staying on this board.
            if hp.path.rstrip("/").lower() == urlparse(board_root).path.rstrip("/").lower():
                if h.rstrip("/") not in seen_pages:
                    queue.append(h)

        # Hydrated state/scripts can contain escaped OpportunityDetail URLs.
        for m in re.finditer(detail_pat, raw, re.I):
            detail_urls.add(urljoin(base, m.group(0)))

        # Also recover bare opportunity IDs paired with this board in embedded JSON.
        for m in re.finditer(
            r'["\'](?:opportunityId|OpportunityId)["\']\s*:\s*["\']([0-9a-f-]{36})["\']',
            raw,
            re.I,
        ):
            oid = m.group(1)
            detail_urls.add(f"{board_root}/OpportunityDetail?opportunityId={oid}")

        # Generic next/pagination controls, limited to the same board.
        for h in _listing_next_links(page, soup):
            hp = urlparse(h)
            if (
                hp.netloc.lower() == urlparse(base).netloc.lower()
                and hp.path.rstrip("/").lower() == urlparse(board_root).path.rstrip("/").lower()
                and h.rstrip("/") not in seen_pages
            ):
                queue.append(h)

    out = []
    seen_ids = set()
    for url in sorted(detail_urls):
        try:
            rr = req("GET", url)
            j = _ukg_detail(src, url, rr.text)
            if j and j.id not in seen_ids:
                seen_ids.add(j.id)
                out.append(j)
        except Exception:
            # One stale/closed opportunity should never abort the employer.
            continue
    return out


def _oracle_parts(url):
    """Return (origin, siteNumber) for an Oracle Fusion Candidate Experience URL."""
    p = urlparse(url)
    m = re.search(
        r"/hcmUI/CandidateExperience/(?:[a-z]{2}(?:-[A-Z]{2})?/)?sites/([^/]+)/",
        p.path,
        re.I,
    )
    if not m:
        m = re.search(
            r"/hcmUI/CandidateExperience/(?:[a-z]{2}(?:-[A-Z]{2})?/)?sites/([^/?#]+)",
            p.path,
            re.I,
        )
    if not m:
        return None
    origin = f"{p.scheme or 'https'}://{p.netloc}"
    return origin, m.group(1)


def _oracle_items(payload):
    """Yield requisition rows across the common Oracle CE response shapes."""
    if not isinstance(payload, dict):
        return []

    rows = []

    # Common response: items[0].requisitionList[]
    for top in payload.get("items") or []:
        if not isinstance(top, dict):
            continue
        rl = top.get("requisitionList")
        if isinstance(rl, list):
            rows.extend(x for x in rl if isinstance(x, dict))
        elif isinstance(rl, dict):
            rows.extend(x for x in (rl.get("items") or []) if isinstance(x, dict))

        # Some releases expose the requisition object itself as an item.
        if any(
            k in top
            for k in (
                "Id",
                "RequisitionId",
                "RequisitionNumber",
                "Title",
                "ExternalTitle",
                "PostedDate",
                "PostedDateTime",
            )
        ):
            rows.append(top)

    # Defensive fallback for alternate wrappers.
    for key in ("requisitionList", "RequisitionList", "results", "jobs"):
        val = payload.get(key)
        if isinstance(val, list):
            rows.extend(x for x in val if isinstance(x, dict))
        elif isinstance(val, dict):
            rows.extend(x for x in (val.get("items") or []) if isinstance(x, dict))

    # De-dupe by likely Oracle job ID while preserving first occurrence.
    out = []
    seen = set()
    for row in rows:
        rid = clean(
            str(
                row.get("Id")
                or row.get("id")
                or row.get("SearchId")
                or row.get("RequisitionId")
                or row.get("requisitionId")
                or row.get("RequisitionNumber")
                or row.get("requisitionNumber")
                or ""
            )
        )
        key = rid or hashlib.sha1(
            json.dumps(row, sort_keys=True, default=str).encode()
        ).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _oracle_value(row, *names):
    """First non-empty Oracle field, case-insensitive."""
    if not isinstance(row, dict):
        return ""
    lower = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        v = row.get(name)
        if v not in (None, "", [], {}):
            return v
        v = lower.get(name.lower())
        if v not in (None, "", [], {}):
            return v
    return ""


def _oracle_date(row):
    """Oracle public CE posting date. Do not substitute unrelated update dates."""
    for name in (
        "PostedDate",
        "postedDate",
        "PostedDateTime",
        "postedDateTime",
        "PostingStartDate",
        "postingStartDate",
        "ExternalPostedStartDate",
        "externalPostedStartDate",
    ):
        d = pdate(str(_oracle_value(row, name) or ""))
        if d:
            return d
    return None


def _oracle_location(row):
    for name in (
        "PrimaryLocation",
        "primaryLocation",
        "PrimaryLocationName",
        "primaryLocationName",
        "Location",
        "location",
        "LocationName",
        "locationName",
    ):
        val = _oracle_value(row, name)
        if isinstance(val, dict):
            for k in ("Name", "name", "DisplayName", "displayName"):
                if val.get(k):
                    return clean(str(val[k]))
        if val:
            return clean(str(val))

    # Some Oracle responses expose city/state/country as independent fields.
    city = clean(str(_oracle_value(row, "City", "city") or ""))
    state = clean(str(_oracle_value(row, "State", "state", "Region", "region") or ""))
    country = clean(str(_oracle_value(row, "Country", "country", "CountryName", "countryName") or ""))
    return clean(", ".join(x for x in (city, state, country) if x))


def _oracle_description(row):
    parts = []
    for name in (
        "ExternalDescriptionStr",
        "externalDescriptionStr",
        "ExternalDescription",
        "externalDescription",
        "Description",
        "description",
        "ShortDescription",
        "shortDescription",
        "Responsibilities",
        "responsibilities",
        "Qualifications",
        "qualifications",
    ):
        val = _oracle_value(row, name)
        if not val:
            continue
        if isinstance(val, (dict, list)):
            val = json.dumps(val, ensure_ascii=False)
        s = strip_html(str(val))
        if s and s not in parts:
            parts.append(s)
    return clean(" ".join(parts))


def _oracle_detail_api(origin, site, rid):
    """Fetch a public Oracle CE detail object when available."""
    rid = clean(str(rid))
    if not rid:
        return {}

    endpoints = [
        (
            f"{origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
            {
                "expand": "all",
                "onlyData": "true",
                "finder": f'ById;Id="{rid}",siteNumber={site}',
            },
        ),
        (
            f"{origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitions/{rid}",
            {"onlyData": "true"},
        ),
    ]

    for url, params in endpoints:
        try:
            r = req(
                "GET",
                url,
                params=params,
                headers={
                    "Accept": "application/json, application/vnd.oracle.adf.resourcecollection+json",
                    "Ora-Irc-Language": "en",
                },
            )
            data = r.json()
            if isinstance(data, dict):
                items = data.get("items")
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    return items[0]
                return data
        except Exception:
            continue
    return {}


def _oracle_job(src, origin, site, row):
    rid = clean(
        str(
            _oracle_value(
                row,
                "Id",
                "id",
                "SearchId",
                "searchId",
                "RequisitionId",
                "requisitionId",
                "RequisitionNumber",
                "requisitionNumber",
            )
            or ""
        )
    )
    if not rid:
        return None

    pd = _oracle_date(row)
    detail = {}

    # Listing rows may be intentionally compact. Fetch detail only when needed.
    title = clean(
        str(
            _oracle_value(
                row,
                "Title",
                "title",
                "ExternalTitle",
                "externalTitle",
                "RequisitionTitle",
                "requisitionTitle",
            )
            or ""
        )
    )
    desc = _oracle_description(row)
    loc = _oracle_location(row)

    if not pd or not title or len(desc) < 200:
        detail = _oracle_detail_api(origin, site, rid)
        if detail:
            if not pd:
                pd = _oracle_date(detail)
            if not title:
                title = clean(
                    str(
                        _oracle_value(
                            detail,
                            "Title",
                            "title",
                            "ExternalTitle",
                            "externalTitle",
                            "RequisitionTitle",
                            "requisitionTitle",
                        )
                        or ""
                    )
                )
            if len(desc) < 200:
                desc = _oracle_description(detail)
            if not loc:
                loc = _oracle_location(detail)

    if not pd or pd < CUTOFF or not title:
        return None

    combined = dict(row)
    if isinstance(detail, dict):
        combined.update({k: v for k, v in detail.items() if v not in (None, "", [], {})})

    # If API detail still lacks prose, use the real public job page as a final
    # structured fallback. This remains the canonical apply/detail URL.
    job_url = f"{origin}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}"
    if len(desc) < 200:
        try:
            rr = req("GET", job_url)
            soup = BeautifulSoup(rr.text, "html.parser")
            jld = _job_from_detail(src, job_url, rr.text)
            if jld:
                # Preserve Oracle's explicit listing/API posting date when present.
                jld.date = pd
                return jld
            main = soup.find("main") or soup
            page_desc = clean(main.get_text(" "))
            if len(page_desc) >= 200:
                desc = page_desc
        except Exception:
            pass

    if len(desc) < 200:
        return None

    reqnum = clean(
        str(
            _oracle_value(
                combined,
                "RequisitionNumber",
                "requisitionNumber",
                "JobNumber",
                "jobNumber",
            )
            or ""
        )
    )
    jid = reqnum or rid

    jt_text = " ".join(
        clean(str(_oracle_value(combined, x) or ""))
        for x in (
            "JobType",
            "jobType",
            "WorkerType",
            "workerType",
            "RegularOrTemporary",
            "regularOrTemporary",
            "FullPartTime",
            "fullPartTime",
            "WorkplaceType",
            "workplaceType",
        )
    )

    workplace = clean(
        str(_oracle_value(combined, "WorkplaceType", "workplaceType") or "")
    )
    arrangement_context = clean(f"{workplace} {loc} {desc}")

    country_hint = clean(
        str(
            _oracle_value(
                combined,
                "Country",
                "country",
                "CountryName",
                "countryName",
                "CountryCode",
                "countryCode",
            )
            or ""
        )
    )

    return Job(
        jid,
        title,
        src["Company"],
        desc,
        pd,
        jobtype(title, jt_text),
        category(title, desc, src["Industry"], src["Company"]),
        job_url,
        src["URL"],
        src["URL"],
        "",
        normalize_work_arrangement(desc, arrangement_context),
        loc,
        "",
        infer_country(country_hint or loc, src["Company"], desc),
    )


def oracle_recruiting(src):
    """Dedicated Oracle Fusion Recruiting Candidate Experience collector.

    Oracle's public CE job site calls recruitingCEJobRequisitions with the
    findReqs finder. `expand=requisitionList` is required to receive the actual
    posting rows. We page by limit/offset and construct the real Candidate
    Experience job URL from the returned requisition ID.
    """
    parts = _oracle_parts(src["URL"])
    if not parts:
        return ats_html(src)

    origin, site = parts
    endpoint = f"{origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

    limit = 200
    offset = 0
    max_pages = 25
    rows = []

    headers = {
        "Accept": "application/json, application/vnd.oracle.adf.resourcecollection+json",
        "Ora-Irc-Language": "en",
    }

    for _ in range(max_pages):
        params = {
            "onlyData": "true",
            "expand": "requisitionList",
            "finder": f"findReqs;siteNumber={site},limit={limit},offset={offset}",
        }

        try:
            r = req("GET", endpoint, params=params, headers=headers)
            payload = r.json()
        except Exception:
            # Some Oracle releases accept limit/offset only as regular query args.
            params = {
                "onlyData": "true",
                "expand": "requisitionList",
                "finder": f"findReqs;siteNumber={site}",
                "limit": limit,
                "offset": offset,
            }
            r = req("GET", endpoint, params=params, headers=headers)
            payload = r.json()

        page_rows = _oracle_items(payload)
        if not page_rows:
            break

        rows.extend(page_rows)

        # Oracle can return total count in several locations.
        total = None
        candidates = [payload]
        if isinstance(payload, dict):
            candidates += [
                x for x in (payload.get("items") or []) if isinstance(x, dict)
            ]
        for obj in candidates:
            for key in (
                "TotalJobsCount",
                "totalJobsCount",
                "TotalResults",
                "totalResults",
                "count",
            ):
                try:
                    val = int(obj.get(key))
                    if val >= 0:
                        total = val
                        break
                except Exception:
                    pass
            if total is not None:
                break

        offset += limit
        if total is not None and offset >= total:
            break
        if len(page_rows) < limit:
            # Some Oracle responses wrap all rows inside one requisitionList
            # item, so only stop here when there is no indication of more data.
            has_more = bool(payload.get("hasMore")) if isinstance(payload, dict) else False
            if not has_more:
                break

    out = []
    seen = set()
    for row in rows:
        try:
            j = _oracle_job(src, origin, site, row)
            if j and j.id not in seen:
                seen.add(j.id)
                out.append(j)
        except Exception:
            # One stale or malformed Oracle requisition must not abort employer.
            continue
    return out


def _paycom_parts(url):
    """Return (origin, clientkey) for public Paycom ATS URLs."""
    p = urlparse(url)
    q = parse_qs(p.query)
    ck = clean((q.get("clientkey") or q.get("clientKey") or [""])[0])
    if not ck:
        m = re.search(r"[?&]clientkey=([A-F0-9]+)", url, re.I)
        if m:
            ck = m.group(1)
    if not ck:
        return None
    return f"{p.scheme or 'https'}://{p.netloc}", ck


def _paycom_date_from_text(raw):
    s = html.unescape(raw or "").replace("\\/", "/")
    patterns = [
        r"(?:Posted|Date Posted|Posting Date)\s*:?\s*"
        r"([A-Za-z]+\s+\d{1,2},\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2}|\d{4}-\d{2}-\d{2})",
        r'["\'](?:datePosted|postedDate|postingDate|createdDate)["\']\s*:\s*["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        for m in re.finditer(pat, s, re.I):
            d = pdate(strip_html(m.group(1)))
            if d:
                return d
    return None


def _paycom_job_links(base_url, raw):
    """Recover real Paycom job-detail links from HTML/scripts."""
    raw2 = html.unescape(raw or "").replace("\\/", "/")
    out = set()

    # Common Paycom detail/link shapes.
    pats = [
        r'https?://[^"\'<>\s]+paycomonline\.net[^"\'<>\s]+',
        r'/(?:v4/)?ats/web\.php/jobs/ViewJobDetails\?[^"\'<>\s]+',
        r'/(?:v4/)?ats/web\.php/jobs\?[^"\'<>\s]*(?:job|jpt|id)=[^"\'<>\s&]+[^"\'<>\s]*',
    ]

    for pat in pats:
        for m in re.finditer(pat, raw2, re.I):
            u = urljoin(base_url, m.group(0))
            if "paycomonline.net" in urlparse(u).netloc.lower():
                out.add(u.split("#",1)[0])

    return out


def _paycom_detail(src, url, raw):
    # Prefer schema.org when present.
    j = _job_from_detail(src, url, raw)
    if j:
        return j

    soup = BeautifulSoup(raw, "html.parser")
    pd = _paycom_date_from_text(raw)
    if not pd or pd < CUTOFF:
        return None

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ") if h1 else "")
    if not title:
        title_node = soup.find(attrs={"class": re.compile(r"(job.?title|position.?title|title)", re.I)})
        title = clean(title_node.get_text(" ") if title_node else "")
    if not title:
        return None

    full_text = clean(soup.get_text(" "))
    loc = ""
    m = re.search(
        r"(?:Location|Job Location)\s*:?\s*([A-Za-z0-9 .,'\-/]+?)(?:\s{2,}|Department|Job Type|Category|$)",
        full_text,
        re.I,
    )
    if m:
        loc = clean(m.group(1))

    jid = ""
    q = parse_qs(urlparse(url).query)
    for k in ("job", "jobid", "jobId", "id", "jpt"):
        if q.get(k):
            jid = clean(q[k][0])
            break
    if not jid:
        m = re.search(r"\b(?:Job ID|Requisition)\s*:?\s*([A-Z0-9_-]{3,})", full_text, re.I)
        if m:
            jid = m.group(1)
    if not jid:
        jid = hashlib.sha1(url.encode()).hexdigest()[:16]

    main = (
        soup.find(attrs={"class": re.compile(r"(job.?description|job.?details|position.?details)", re.I)})
        or soup.find(id=re.compile(r"(job.?description|job.?details|position.?details)", re.I))
        or soup.find("main")
        or soup
    )
    desc = clean(main.get_text(" "))
    if len(desc) < 200:
        return None

    canonical = url.split("#",1)[0]
    return Job(
        jid,
        title,
        src["Company"],
        desc,
        pd,
        jobtype(title, full_text),
        category(title, desc, src["Industry"], src["Company"]),
        canonical,
        src["URL"],
        src["URL"],
        "",
        normalize_work_arrangement(desc, loc or full_text),
        loc,
        "",
        infer_country(loc or full_text, src["Company"], desc),
    )


def paycom(src):
    """Dedicated Paycom public ATS collector.

    Paycom boards are server-rendered enough to expose job links either as
    anchors or hydrated script URLs. Enumerate the board, follow bounded
    pagination, and parse each actual job-detail page. Posting dates must be
    explicit and within the MJR window.
    """
    parts = _paycom_parts(src["URL"])
    if not parts:
        return ats_html(src)

    origin, clientkey = parts
    start = src["URL"]

    queue = [start]
    seen_pages = set()
    details = set()

    while queue and len(seen_pages) < 80 and len(details) < 3000:
        page = queue.pop(0)
        key = page.rstrip("/")
        if key in seen_pages:
            continue
        seen_pages.add(key)

        r = req("GET", page)
        soup = BeautifulSoup(r.text, "html.parser")

        # Visible job links.
        for a in soup.find_all("a", href=True):
            h = urljoin(page, a["href"])
            hp = urlparse(h)
            if "paycomonline.net" not in hp.netloc.lower():
                continue

            q = parse_qs(hp.query)
            if (
                "viewjobdetails" in hp.path.lower()
                or any(k.lower() in ("job", "jobid", "id", "jpt") for k in q)
            ):
                details.add(h.split("#",1)[0])
                continue

            # Follow same-client listing/pagination links.
            hq = parse_qs(hp.query)
            hck = clean((hq.get("clientkey") or [""])[0])
            if hck and hck.lower() == clientkey.lower():
                if h.rstrip("/") not in seen_pages:
                    queue.append(h)

        # Hydrated/scripted links.
        details.update(_paycom_job_links(page, r.text))

        # Generic pagination controls constrained to Paycom/clientkey.
        for h in _listing_next_links(page, soup):
            hp = urlparse(h)
            if "paycomonline.net" not in hp.netloc.lower():
                continue
            hq = parse_qs(hp.query)
            hck = clean((hq.get("clientkey") or [""])[0])
            if hck and hck.lower() == clientkey.lower() and h.rstrip("/") not in seen_pages:
                queue.append(h)

    out = []
    seen_ids = set()
    for url in sorted(details):
        try:
            rr = req("GET", url)
            j = _paycom_detail(src, url, rr.text)
            if j and j.id not in seen_ids:
                seen_ids.add(j.id)
                out.append(j)
        except Exception:
            continue
    return out

def ats_html(src):
    """Enhanced multi-ATS public-page adapter.

    Enumerates job-detail URLs from anchors, script/application state, JSON-LD,
    and server-side pagination. It deliberately keeps the final apply URL on
    the actual discovered job-detail page and still requires a qualifying
    recent posting date when the detail page is parsed.
    """
    family = _ats_family(src)
    patterns = ATS_JOB_HINTS.get(family, [])
    if not patterns:
        return generic(src)

    start = src["URL"]
    start_host = (urlparse(start).netloc or "").lower()
    queue = [start]
    seen_pages = set()
    job_links = set()

    while queue and len(seen_pages) < 40 and len(job_links) < 2000:
        page = queue.pop(0)
        key = page.rstrip("/")
        if key in seen_pages:
            continue
        seen_pages.add(key)

        r = req("GET", page)
        soup = BeautifulSoup(r.text, "html.parser")

        # Some source rows are already a job detail.
        if any(re.search(p, page, re.I) for p in patterns):
            job_links.add(page)

        # Normal links. Permit a same-host URL or another recognized ATS host;
        # company career sites frequently redirect/link to a separate ATS host.
        for a in soup.find_all("a", href=True):
            h = urljoin(page, a["href"])
            host = (urlparse(h).netloc or "").lower()
            if any(re.search(p, h, re.I) for p in patterns):
                if host == start_host or _known_ats_url(h):
                    job_links.add(h)
                    continue

        # URLs inside script state / hydrated JSON.
        for h in _candidate_urls_from_text(page, r.text, patterns):
            host = (urlparse(h).netloc or "").lower()
            if host == start_host or _known_ats_url(h):
                job_links.add(h)

        # JSON-LD often exposes the canonical detail URL even when the visible
        # listing is rendered client-side.
        for jp in _jsonld_jobs(soup):
            h = clean(jp.get("url") or jp.get("sameAs") or "")
            if h:
                h = urljoin(page, h)
                if any(re.search(p, h, re.I) for p in patterns) or _known_ats_url(h):
                    job_links.add(h)

        # Follow bounded pagination/search result pages.
        for h in _listing_next_links(page, soup):
            host = (urlparse(h).netloc or "").lower()
            if host == start_host and h.rstrip("/") not in seen_pages:
                queue.append(h)

    out = []
    seen_job_urls = set()
    for url in list(job_links)[:2000]:
        canon = url.split("#", 1)[0]
        if canon in seen_job_urls:
            continue
        seen_job_urls.add(canon)
        try:
            rr = req("GET", canon)
            j = _job_from_detail(src, canon, rr.text)
            if j:
                out.append(j)
        except Exception:
            continue

    return out


def federated_media(src):
    """Federated Media job fallback using its WordPress job-listing content.

    Their public careers landing page can redirect inconsistently, while actual
    job posts live under /job/{slug}/. Try WordPress REST and feeds first,
    preserving only explicit recent postings.
    """
    bases = [
        "https://federatedmedia.com",
        "https://www.federatedmedia.com",
    ]
    detail_urls = set()

    # WP Job Manager commonly exposes a job_listing post type.
    api_paths = [
        "/wp-json/wp/v2/job_listing?per_page=100&page=1",
        "/wp-json/wp/v2/jobs?per_page=100&page=1",
    ]

    for base in bases:
        for api_path in api_paths:
            try:
                r = req("GET", base + api_path)
                data = r.json()
                if not isinstance(data, list):
                    continue
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    link = clean(str(item.get("link") or ""))
                    if link and "/job/" in link:
                        detail_urls.add(link)
            except Exception:
                pass

        # RSS/feed fallback.
        for feed_url in (
            base + "/feed/?post_type=job_listing",
            base + "/careers/feed/",
        ):
            try:
                r = req("GET", feed_url)
                raw = html.unescape(r.text or "")
                for m in re.finditer(r"https?://[^<\"'\s]+/job/[^<\"'\s]+", raw, re.I):
                    detail_urls.add(m.group(0).rstrip(".,)"))
            except Exception:
                pass

    out = []
    seen = set()
    for url in sorted(detail_urls):
        try:
            rr = req("GET", url)
            soup = BeautifulSoup(rr.text, "html.parser")
            txt = clean(soup.get_text(" "))

            # Prefer schema.org JobPosting if provided.
            j = _job_from_detail(src, url, rr.text)
            if j:
                if j.url not in seen:
                    seen.add(j.url)
                    out.append(j)
                continue

            pd = None
            m = re.search(
                r"(?:Posted|Date Posted|Posted Date)\s*:?\s*"
                r"([A-Za-z]+\s+\d{1,2},\s+20\d{2}|"
                r"\d{1,2}/\d{1,2}/20\d{2}|"
                r"\d+\s+days?\s+ago|today|yesterday)",
                txt,
                re.I,
            )
            if m:
                pd = pdate(m.group(1))
            if not pd or pd < CUTOFF:
                continue

            h1 = soup.find("h1")
            title = clean(h1.get_text(" ") if h1 else "")
            main = soup.find("main") or soup.find("article") or soup
            desc = clean(main.get_text(" "))
            if not title or len(desc) < 250:
                continue

            loc = ""
            mloc = re.search(
                r"(?:Location|Job Location)\s*:?\s*"
                r"([A-Za-z .,'/-]+?)(?:\s+Posted|\s+Full Time|\s+Part Time|$)",
                txt,
                re.I,
            )
            if mloc:
                loc = clean(mloc.group(1))

            jid = hashlib.sha1(url.encode()).hexdigest()[:16]
            out.append(Job(
                jid,
                title,
                src["Company"],
                desc,
                pd,
                jobtype(title, txt),
                category(title, desc, src["Industry"], src["Company"]),
                url,
                src["URL"],
                src["URL"],
                "",
                normalize_work_arrangement(desc, loc or txt),
                loc,
                "",
                infer_country(loc or txt, src["Company"], desc),
            ))
            seen.add(url)
        except Exception:
            continue

    return out


def connoisseur_media(src):
    """Direct collector for Connoisseur Media's current WordPress careers site.

    The employer now publishes openings at /career-opportunity/ with individual
    /career-opportunity/{slug}/ detail/application pages. These pages are the
    canonical application destinations, so do not route applicants through an
    obsolete third-party ATS URL.
    """
    listing_urls = [
        "https://connoisseurmedia.com/career-opportunity/",
        "https://connoisseurmedia.com/careers/",
    ]

    detail_urls = set()

    for listing_url in listing_urls:
        try:
            r = req("GET", listing_url)
            soup = BeautifulSoup(r.text, "html.parser")

            for a in soup.find_all("a", href=True):
                h = urljoin(listing_url, a["href"])
                hp = urlparse(h)
                if hp.netloc.lower().replace("www.", "") != "connoisseurmedia.com":
                    continue
                path = hp.path.rstrip("/") + "/"
                if path.startswith("/career-opportunity/") and path != "/career-opportunity/":
                    detail_urls.add(h.split("#", 1)[0])

            # WordPress/script state can contain cards not rendered as anchors.
            raw = html.unescape(r.text or "").replace("\\/", "/")
            for m in re.finditer(
                r'https?://(?:www\.)?connoisseurmedia\.com/career-opportunity/[^"\'<>\s?#]+/?',
                raw,
                re.I,
            ):
                h = m.group(0)
                if h.rstrip("/") != "https://connoisseurmedia.com/career-opportunity":
                    detail_urls.add(h)
        except Exception:
            continue

    out = []
    seen = set()

    for url in sorted(detail_urls):
        try:
            rr = req("GET", url)
            soup = BeautifulSoup(rr.text, "html.parser")
            txt = clean(soup.get_text(" "))

            h1 = soup.find("h1")
            title = clean(h1.get_text(" ") if h1 else "")
            if not title:
                continue

            # Do not infer a posting date from unrelated page/news timestamps.
            # First prefer JobPosting JSON-LD if the employer publishes one.
            schema_job = _job_from_detail(src, url, rr.text)
            if schema_job:
                if schema_job.date >= CUTOFF and schema_job.url not in seen:
                    seen.add(schema_job.url)
                    out.append(schema_job)
                continue

            pd = None

            # Look only for explicit job-posting date labels in visible text.
            patterns = [
                r"(?:Date Posted|Posted Date|Posted)\s*:?\s*"
                r"([A-Za-z]+\s+\d{1,2},\s+20\d{2})",
                r"(?:Date Posted|Posted Date|Posted)\s*:?\s*"
                r"(\d{1,2}/\d{1,2}/20\d{2})",
                r"(?:Date Posted|Posted Date|Posted)\s*:?\s*"
                r"(20\d{2}-\d{2}-\d{2})",
            ]
            for pat in patterns:
                m = re.search(pat, txt, re.I)
                if m:
                    pd = pdate(m.group(1))
                    if pd:
                        break

            # WordPress may expose the actual publish date in article metadata.
            # Accept it only when this is clearly a career-opportunity post.
            if not pd:
                for meta in soup.find_all("meta"):
                    prop = (meta.get("property") or meta.get("name") or "").lower()
                    if prop in {
                        "article:published_time",
                        "date",
                        "datepublished",
                    }:
                        d = pdate(meta.get("content") or "")
                        if d:
                            pd = d
                            break

            if not pd or pd < CUTOFF:
                continue

            # Prefer the central content area and strip application boilerplate
            # only by choosing the article/main container, not by truncating text.
            main = (
                soup.find("main")
                or soup.find("article")
                or soup.find(attrs={"class": re.compile(r"(entry-content|post-content|job-content)", re.I)})
                or soup
            )
            desc = clean(main.get_text(" "))
            if len(desc) < 250:
                continue

            # Infer location only from explicit location labels when available.
            loc = ""
            for pat in (
                r"(?:Job Location|Location)\s*:?\s*([A-Za-z0-9 .,'/\-&]+?)(?=\s+(?:Job Type|Employment Type|Category|Apply|Description)\b|$)",
                r"\b([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b",
            ):
                m = re.search(pat, txt)
                if m:
                    loc = clean(m.group(1))
                    break

            # The page itself contains "Apply for this position", so the detail
            # URL is also the live application URL.
            canonical = url.split("#", 1)[0]
            jid = hashlib.sha1(canonical.encode()).hexdigest()[:16]

            out.append(
                Job(
                    jid,
                    title,
                    src["Company"],
                    desc,
                    pd,
                    jobtype(title, txt),
                    category(title, desc, src["Industry"], src["Company"]),
                    canonical,
                    canonical,
                    "https://connoisseurmedia.com/",
                    "",
                    normalize_work_arrangement(desc, loc or txt),
                    loc,
                    "",
                    infer_country(loc or txt, src["Company"], desc),
                )
            )
            seen.add(canonical)

        except Exception:
            continue

    return out


def _isolved_parts(url):
    """Return (origin, company slug) for common iSolved Hire URLs."""
    p = urlparse(url)
    host = p.netloc.lower()
    parts = [x for x in p.path.split("/") if x]

    # Common patterns:
    # https://jobs.ourcareerpages.com/job/...  (iSolved legacy)
    # https://recruiting2.ultipro... is not iSolved and should not match.
    if "ourcareerpages.com" in host:
        slug = ""
        q = parse_qs(p.query)
        for key in ("ccp", "company", "client", "cid"):
            if q.get(key):
                slug = clean(q[key][0])
                break
        return f"{p.scheme or 'https'}://{p.netloc}", slug

    if "isolvedhire.com" in host or "isolved.com" in host:
        slug = parts[0] if parts else ""
        return f"{p.scheme or 'https'}://{p.netloc}", slug

    return None


def _isolved_date(raw):
    s = html.unescape(raw or "").replace("\\/", "/")
    for pat in (
        r"(?:Posted|Date Posted|Posting Date)\s*:?\s*"
        r"([A-Za-z]+\s+\d{1,2},\s+20\d{2})",
        r"(?:Posted|Date Posted|Posting Date)\s*:?\s*"
        r"(\d{1,2}/\d{1,2}/20\d{2})",
        r'["\'](?:datePosted|postedDate|postingDate)["\']\s*:\s*["\']([^"\']+)["\']',
    ):
        m = re.search(pat, s, re.I)
        if m:
            d = pdate(strip_html(m.group(1)))
            if d:
                return d
    return None


def _isolved_job_links(base_url, raw):
    raw2 = html.unescape(raw or "").replace("\\/", "/")
    out = set()
    patterns = [
        r'https?://[^"\'<>\s]+ourcareerpages\.com/job/[^"\'<>\s]+',
        r'https?://[^"\'<>\s]+isolvedhire\.com/[^"\'<>\s]+',
        r'/job/[^"\'<>\s]+',
    ]
    for pat in patterns:
        for m in re.finditer(pat, raw2, re.I):
            u = urljoin(base_url, m.group(0))
            host = urlparse(u).netloc.lower()
            if "ourcareerpages.com" in host or "isolvedhire.com" in host:
                out.add(u.split("#",1)[0])
    return out


def _isolved_detail(src, url, raw):
    # Prefer structured JobPosting when available.
    j = _job_from_detail(src, url, raw)
    if j:
        return j

    soup = BeautifulSoup(raw, "html.parser")
    txt = clean(soup.get_text(" "))

    pd = _isolved_date(raw)
    if not pd or pd < CUTOFF:
        return None

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ") if h1 else "")
    if not title:
        node = soup.find(attrs={"class": re.compile(r"(job.?title|position.?title)", re.I)})
        title = clean(node.get_text(" ") if node else "")
    if not title:
        return None

    main = (
        soup.find(attrs={"class": re.compile(r"(job.?description|job.?details|position.?details)", re.I)})
        or soup.find("main")
        or soup.find("article")
        or soup
    )
    desc = clean(main.get_text(" "))
    if len(desc) < 200:
        return None

    loc = ""
    for pat in (
        r"(?:Location|Job Location)\s*:?\s*([A-Za-z0-9 .,'/\-&]+?)(?=\s+(?:Job Type|Employment Type|Category|Department|Posted|$))",
        r"\b([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b",
    ):
        m = re.search(pat, txt)
        if m:
            loc = clean(m.group(1))
            break

    jid = ""
    q = parse_qs(urlparse(url).query)
    for k in ("jobid", "jobId", "id", "job"):
        if q.get(k):
            jid = clean(q[k][0])
            break
    if not jid:
        m = re.search(r"/job/([^/?#]+)", url, re.I)
        if m:
            jid = clean(m.group(1))
    if not jid:
        jid = hashlib.sha1(url.encode()).hexdigest()[:16]

    canonical = url.split("#",1)[0]

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


def isolved(src):
    """Dedicated iSolved/OurCareerPages collector.

    Enumerates public job-detail URLs from listing pages and embedded state,
    follows same-host pagination, and only emits jobs with an explicit posting
    date inside the MJR window.
    """
    parts = _isolved_parts(src["URL"])
    if not parts:
        return ats_html(src)

    origin, slug = parts
    queue = [src["URL"]]
    seen_pages = set()
    details = set()

    while queue and len(seen_pages) < 80 and len(details) < 2500:
        page = queue.pop(0)
        key = page.rstrip("/")
        if key in seen_pages:
            continue
        seen_pages.add(key)

        r = req("GET", page)
        soup = BeautifulSoup(r.text, "html.parser")

        # Visible detail links.
        for a in soup.find_all("a", href=True):
            h = urljoin(page, a["href"])
            hp = urlparse(h)
            host = hp.netloc.lower()
            if not ("ourcareerpages.com" in host or "isolvedhire.com" in host):
                continue

            if "/job/" in hp.path.lower():
                details.add(h.split("#",1)[0])
                continue

            # Follow listing/pagination links on same platform.
            hp2 = urlparse(h)
            if (
                "admin.isolvedhire.com" in hp2.netloc.lower()
                or hp2.path.lower().startswith("/help")
                or "/help/" in hp2.path.lower()
            ):
                continue
            if h.rstrip("/") not in seen_pages:
                queue.append(h)

        details.update(_isolved_job_links(page, r.text))

        for h in _listing_next_links(page, soup):
            hp = urlparse(h)
            host = hp.netloc.lower()
            if (
                ("ourcareerpages.com" in host or "isolvedhire.com" in host)
                and "admin.isolvedhire.com" not in host
                and not hp.path.lower().startswith("/help")
                and "/help/" not in hp.path.lower()
                and h.rstrip("/") not in seen_pages
            ):
                queue.append(h)

    out = []
    seen_ids = set()
    for url in sorted(details):
        try:
            rr = req("GET", url)
            final_url = str(getattr(rr, "url", "") or url)
            fu = urlparse(final_url)
            if (
                "admin.isolvedhire.com" in fu.netloc.lower()
                or fu.path.lower().startswith("/help")
                or "/help/" in fu.path.lower()
            ):
                continue
            j = _isolved_detail(src, final_url, rr.text)
            if j and j.id not in seen_ids:
                seen_ids.add(j.id)
                out.append(j)
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
            description_text,
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

def paramount_successfactors(src):
    """Paramount SAP SuccessFactors collector — v69.

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

    d("PARAMOUNT SUCCESSFACTORS v69")
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

    for url in detail_urls[:300]:
        try:
            j = _recent_detail_job(src, url)
            if not j or j.id in seen_ids:
                continue

            # Paramount corporate website should not be the careers host.
            try:
                j.company_website = "https://www.paramount.com/"
            except Exception:
                pass

            # Paramount is television/streaming-heavy. Preserve the global
            # classifier, but strengthen obvious CBS/TV newsroom/production
            # titles that generic fallback can otherwise misplace.
            low = clean(j.title).lower()
            dlow = clean(j.description).lower()

            if j.jobtype == "Internship":
                j.category = "Internships"
            elif re.search(
                r"\b(anchor|meteorologist|weather anchor|news producer|"
                r"executive producer|assignment editor|photojournalist|"
                r"photographer|newscast|broadcast director|tv producer|"
                r"television producer|studio technician|broadcast technician)\b",
                low,
            ) or (
                "cbs television stations" in dlow
                and re.search(r"\b(producer|director|reporter|anchor|news|studio)\b", low)
            ):
                j.category = "Television"

            seen_ids.add(j.id)
            out.append(j)

            if len(out) <= 80:
                d(
                    f"ACCEPT {j.id} title={j.title} type={j.jobtype} "
                    f"cat={j.category} loc={j.city},{j.state},{j.country} "
                    f"wa={j.work_arrangement} date={j.date}"
                )
        except Exception as e:
            d(f"DETAIL_ERROR {url} {type(e).__name__}:{e}")

    d(f"FINAL={len(out)}")
    Path("mjr-paramount-diagnostic-v69.txt").write_text(
        "\n".join(diag) + "\n", encoding="utf-8"
    )
    return out


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


def gray_direct(src):
    """Gray Media direct career-center fallback.

    Gray's corporate careers page currently exposes hundreds of openings and
    filters publicly. Prefer those canonical employer pages over relying solely
    on the legacy UKG board.
    """
    starts = [
        "https://graymedia.com/careers/",
        src["URL"],
    ]

    # First crawl Gray's own careers surface.
    jobs = _crawl_rendered_job_board(
        src,
        starts,
        allow_hosts={
            "graymedia.com",
            "www.graymedia.com",
            "recruiting.ultipro.com",
        },
        max_pages=100,
        max_jobs=3500,
    )
    if jobs:
        return jobs

    # If the corporate page links only to UKG opportunity details, use the
    # existing UKG collector as a final fallback; it already fails safely.
    try:
        return ukg(src)
    except Exception:
        return []


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
            desc = strip_html(desc_html)

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
    return _v17_samehost_details(
        src,
        [
            "https://www.nbcunicareers.com/find-a-job",
            src["URL"],
        ],
        {"nbcunicareers.com"},
        max_pages=140,
        max_jobs=4000,
    )


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
    """Cumulus public career site collector with common listing variants."""
    return _v17_samehost_details(
        src,
        [
            "https://jobs.cumulusmedia.com/jobs",
            "https://jobs.cumulusmedia.com/",
            src["URL"],
        ],
        {"jobs.cumulusmedia.com"},
        max_pages=160,
        max_jobs=4000,
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

    return out


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
        pd = TODAY
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
    main=(soup.find("main") or soup.find("article")
          or soup.find(attrs={"class":re.compile(r"(job.?description|job.?detail|posting|entry-content|career)",re.I)})
          or soup)
    desc=clean(main.get_text(" "))
    if len(desc)<250:
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
    """Target Midwest's Paylocity board plus Midwest Careers detail pages."""
    roots = [
        "https://midwestcareers.com/",
        "https://recruiting.paylocity.com/recruiting/jobs/All/0cb3a074-2113-4e9e-a32d-27e40c132e62/Midwest-Communications",
    ]
    details = set()
    for root in roots:
        try:
            r = req("GET", root)
        except Exception:
            continue
        final = str(getattr(r, "url", "") or root)
        details.update(_v23_detail_candidates(final, r.text))
        for m in re.finditer(
            r'https?://recruiting\.paylocity\.com/recruiting/jobs/Details/\d+/[^"\'<>\s]+',
            r.text, re.I
        ):
            details.add(m.group(0).replace("\\/", "/"))
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
    desc = strip_html(str(desc_html))
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

def _v28_source_enabled(src):
    return (not MJR_TEST_COMPANIES or
            clean(src.get("Company", "")).lower() in MJR_TEST_COMPANIES)

def _v28_before_request(url):
    host = urlparse(url).netloc.lower()
    count = _v28_domain_counts.get(host, 0)
    if count >= MJR_DOMAIN_REQUEST_CAP:
        raise RuntimeError(f"v28 domain request cap reached for {host}: {MJR_DOMAIN_REQUEST_CAP}")
    _v28_domain_counts[host] = count + 1
    lo = max(0.0, MJR_REQUEST_DELAY_MIN)
    hi = max(lo, MJR_REQUEST_DELAY_MAX)
    if hi:
        time.sleep(random.uniform(lo, hi))

def _v28_backoff_seconds(attempt):
    return min(12.0, 1.5 * (2 ** attempt)) + random.uniform(0.0, 0.75)

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

        r["misses"] = int(r.get("misses", 0)) + 1

        if r["misses"] >= 2:
            del st[k]
            continue

        x = r.get("job", {})

        try:
            pd = date.fromisoformat(x["date"])
        except Exception:
            del st[k]
            continue

        life = 30 if x.get("jobtype") == "Internship" else 21

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
            x["country"] = infer_country(
                x.get("city", ""),
                x.get("company", ""),
                x.get("description", ""),
            )
            ret.append(Job(**x))
        except Exception:
            pass

    STATE_FILE.write_text(json.dumps(st, indent=2, default=str))
    return ret


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

        vals = [
            ("id", j.id),
            ("title", j.title),
            ("company", j.company),
            ("description", j.description),
            ("date", j.date.isoformat()),
            ("expiration", str(j.expiration)),
            ("jobtype", j.jobtype),
            ("category", j.category),
            ("url", j.url),
            ("source", j.source),
            ("url_verified", TODAY.isoformat()),
            ("company_website", j.company_website),
            ("logo", j.logo),
            ("work_arrangement", j.work_arrangement),
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

        # Salem detail pages must provide an explicit recent posting date.
        # Do not fabricate TODAY for an undated posting.
        pd = _v18_icims_date(rr.text)
        if not pd or pd < CUTOFF:
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
    Build the public apply/detail URL from the exact job detail page being parsed.
    This prevents stale or mismatched Audacy links from being carried forward.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    candidates = []

    can = soup.find("link", rel=lambda x: x and "canonical" in str(x).lower())
    if can and can.get("href"):
        candidates.append(can.get("href"))

    og = soup.find("meta", attrs={"property": "og:url"})
    if og and og.get("content"):
        candidates.append(og.get("content"))

    # Prefer links on the current page that point to the same job id.
    m = re.search(r"/jobs/(\d+)/", detail_url, re.I)
    job_id = m.group(1) if m else None
    if job_id:
        for a in soup.find_all("a", href=True):
            href = a.get("href")
            if href and re.search(rf"/jobs/{re.escape(job_id)}/", href, re.I):
                candidates.append(href)

    candidates.append(detail_url)

    host = urlparse(detail_url).netloc.lower()
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

        pd = _v18_icims_date(rr.text)
        if not pd or pd < CUTOFF:
            continue
        j.date = pd

        if j.id not in seen_ids:
            seen_ids.add(j.id)
            out.append(j)

    print(f"Salem frame-aware collector qualifying jobs: {len(out)}")
    return out


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

            got = (
                ashby(s)
                if "ashby" in a or "ashbyhq.com" in s.get("URL", "").lower()
                else []
                if company_key == "audacy"
                else paylocity_v18(s)
                if company_key in {
                    "dick broadcasting company",
                    "hope media group",
                    "nrg media",
                    "weigel",
                }
                else siriusxm_v17(s)
                if company_key == "siriusxm"
                else townsquare_v17(s)
                if company_key == "townsquare media"
                else nbcuniversal_v17(s)
                if company_key == "nbcuniversal"
                else tegna_v17(s)
                if company_key == "tegna"
                else cumulus_v17(s)
                if company_key == "cumulus media"
                else cox_successfactors(s)
                if company_key in {"cox media group", "cox radio"}
                else paramount_successfactors(s)
                if company_key == "paramount"
                else disney_public(s)
                if company_key in {"disney / abc", "espn"}
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
                else adp(s)
                if "adp" in a
                else dayforce(s)
                if "dayforce" in a
                else salem_icims_v29(s)
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
                else ats_html(s)
                if _ats_family(s)
                else generic(s)
            )

            icims_enumerated = 0

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

            if not got:
                got = structured_jobs_v27(s)

            if not got and company_key in V25_FAST_RADIO_TARGETS:
                if company_key == "educational media foundation":
                    got = emf_v26_narrow(s)
                else:
                    got = radio_direct_v25(s)

            if not got and company_key in V23_RADIO_TARGETS:
                got = radio_targeted_v23(s)

            if not got and company_key in RADIO_RECOVERY_COMPANIES:
                got = radio_recovery(s)

            jobs += got

            audit.append(
                [
                    s["Company"],
                    s["ATS"],
                    s["URL"],
                    (
                        "ok"
                        if got
                        else "enumerated_no_fresh_jobs"
                        if icims_enumerated
                        else "zero_or_not_enumerable"
                    ),
                    len(got),
                    (
                        (
                            f"enumerated_jobs={icims_enumerated}"
                            if company_key == "audacy"
                            else f"enumerated_detail_urls={icims_enumerated}"
                        )
                        if icims_enumerated
                        else ""
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

    ded = {
        j.url.rstrip("/").lower(): j
        for j in jobs
        if j.url
        and len(j.description) >= 200
        and j.date >= CUTOFF
        and j.category in APPROVED
    }

    jobs = stateful(list(ded.values()))

    ded = {
        j.url.rstrip("/").lower(): j
        for j in jobs
    }

    jobs = list(ded.values())

    write_xml(jobs)

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
