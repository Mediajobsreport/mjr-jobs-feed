#!/usr/bin/env python3
import csv, hashlib, html, json, os, re, time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser


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


def req(method, url, **kw):
    for n in range(4):
        try:
            r = SESSION.request(method, url, timeout=30, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2**n)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException:
            if n == 3:
                raise
            time.sleep(2**n)


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
    s = (title + " " + text).lower()

    if "intern" in s:
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
    Determine work arrangement from explicit employer language.
    On-Site, Hybrid, and Remote are kept as separate values.
    Explicit on-site language takes precedence over incidental mentions
    such as "remote location." Hybrid is used only when the employer
    explicitly describes a mixed in-office/remote arrangement.
    """
    desc = clean(description).lower()
    loc = clean(location).lower()
    s = f" {desc} {loc} "

    # Strong explicit on-site signals win over incidental uses of "remote."
    onsite_patterns = [
        r"requires full[- ]time on[- ]site presence",
        r"requires full time on site presence",
        r"this role requires full[- ]time on[- ]site",
        r"this role requires full time on site",
        r"this position is an on[- ]site role",
        r"this position is onsite",
        r"on[- ]site role",
        r"on site role",
        r"\(on[- ]site\)",
        r"\(on site\)",
    ]
    if any(re.search(p, s) for p in onsite_patterns):
        return "On-Site"

    # Explicit hybrid signals. These must be checked before Remote.
    hybrid_patterns = [
        r"telework/hybrid",
        r"hybrid/telework",
        r"hybrid role",
        r"hybrid position",
        r"hybrid work",
        r"hybrid schedule",
        r"hybrid arrangement",
        r"mix of in-office and remote work",
        r"mix of in office and remote work",
        r"combining remote work and office presence",
        r"combination of remote work and office presence",
        r"partly remote",
        r"partially remote",
        r"days? per week in the office",
        r"days? a week in the office",
    ]
    if any(re.search(p, s) for p in hybrid_patterns):
        return "Hybrid"

    # Explicit remote/work-from-home signals that do not require routine office presence.
    remote_patterns = [
        r"fully remote",
        r"100% remote",
        r"remote role",
        r"remote position",
        r"remote work",
        r"work from home",
        r"work-from-home",
        r"telework",
        r"telecommute",
    ]
    if any(re.search(p, s) for p in remote_patterns):
        return "Remote"

    # Title/location can explicitly indicate the arrangement.
    if re.search(r"\bhybrid\b", loc):
        return "Hybrid"
    if re.search(r"\b(remote|telework|telecommute)\b", loc):
        return "Remote"

    return "On-Site"

def workday(src):
    u = urlparse(src["URL"])
    host = u.netloc
    tenant = host.split(".")[0]
    parts = [
        p
        for p in u.path.split("/")
        if p and p not in ("en-US", "en-CA", "jobs")
    ]
    site = parts[0] if parts else ""

    if not tenant or not site or tenant == "myworkdaycenter":
        raise RuntimeError("Workday tenant/site not inferable")

    ep = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out = []
    offset = 0

    while True:
        d = req(
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

            # A requisition can disappear between the Workday list call and
            # its detail call. Do not let one expired/withdrawn posting abort
            # the entire employer crawl.
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
            url = info.get("externalUrl") or urljoin(src["URL"], ext)

            out.append(
                Job(
                    info.get("jobReqId")
                    or hashlib.sha1(url.encode()).hexdigest()[:16],
                    title,
                    src["Company"],
                    desc,
                    pd,
                    jobtype(title, info.get("timeType", "")),
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

        r = req("GET", page)
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


def main():
    with SOURCES_FILE.open(
        newline="",
        encoding="utf-8-sig",
    ) as f:
        sources = list(csv.DictReader(f))

    jobs = []
    audit = []

    for s in sources:
        if str(s.get("Active", "True")).lower() in ("false", "0", "no"):
            continue

        try:
            a = s["ATS"].lower()

            got = (
                workday(s)
                if "workday" in a
                else greenhouse(s)
                if "greenhouse" in a
                else paylocity(s)
                if "paylocity" in a
                else adp(s)
                if "adp" in a
                else dayforce(s)
                if "dayforce" in a
                else icims(s)
                if "icims" in a
                else ats_html(s)
                if _ats_family(s)
                else generic(s)
            )

            jobs += got

            audit.append(
                [
                    s["Company"],
                    s["ATS"],
                    s["URL"],
                    "ok" if got else "zero_or_not_enumerable",
                    len(got),
                    "",
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
