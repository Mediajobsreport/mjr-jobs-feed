#!/usr/bin/env python3
import csv, hashlib, html, json, os, re, time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

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

            info = req(
                "GET",
                f"https://{host}/wday/cxs/{tenant}/{site}{ext}",
            ).json().get("jobPostingInfo", {})

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
