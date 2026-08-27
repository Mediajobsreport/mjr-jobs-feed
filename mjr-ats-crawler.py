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
        return dtparser.parse(s, fuzzy=True).date()
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
    if re.search(r"\b(intern|internship|trainee program|summer trainee)\b", t):
        return "Internships"

    # ------------------------------------------------------------------
    # 2) BUSINESS OFFICE / ADMIN / FINANCE / HR / LEGAL / SCHEDULING
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"accountant|accounting|accounts payable|accounts receivable|"
        r"finance|financial|controller|payroll|bookkeeper|treasury|tax|"
        r"human resources|hr coordinator|hr manager|hr business partner|"
        r"people operations|people partner|talent acquisition|recruiter|recruiting|"
        r"administrative assistant|administrative coordinator|administrator|"
        r"executive assistant|office assistant|office coordinator|office manager|"
        r"legal|attorney|counsel|paralegal|business affairs|contracts|compliance|"
        r"procurement|purchasing|facilities|receptionist|billing|credit|collections|"
        r"traffic coordinator|traffic assistant|traffic specialist|"
        r"operations coordinator|business operations|"
        r"assignment coordinator|assignment co-ordinator|"
        r"scheduling coordinator|schedule coordinator|scheduler|"
        r"resource coordinator|program coordinator|project coordinator"
        r")\b",
        t,
    ):
        return "Business Office"

    # ------------------------------------------------------------------
    # 3) ENGINEERING / TECHNICAL / BROADCAST SYSTEMS
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"broadcast engineer|chief engineer|maintenance engineer|"
        r"systems engineer|network engineer|rf engineer|audio engineer|video engineer|"
        r"studio engineer|field engineer|transmission engineer|"
        r"broadcast technician|studio technician|maintenance technician|"
        r"technical maintenance|technical director|technical operations|"
        r"master control|master control operator|transmission|transmitter|"
        r"information technology|it engineer|it technician|it support|"
        r"network administrator|systems administrator|systems technician|"
        r"av technician|audiovisual technician|electronics technician"
        r")\b",
        t,
    ):
        return "Engineering"

    # Generic "engineer" titles are engineering unless clearly software/product.
    if re.search(r"\bengineer\b", t) and not re.search(
        r"\b(software|data|machine learning|ai|web|mobile|product)\b", t
    ):
        return "Engineering"

    # Vague maintenance/technician title + explicit broadcast/production-system work.
    if re.search(r"\b(maintenance|technician)\b", t) and re.search(
        r"\b("
        r"broadcasting systems?|broadcast systems?|production systems?|"
        r"studio equipment|transmitter|transmission|networking|electronics|"
        r"audiovisual|telecommunications|technical infrastructure"
        r")\b",
        d_short,
    ):
        return "Engineering"

    # ------------------------------------------------------------------
    # 4) PUBLIC RELATIONS / CORPORATE COMMUNICATIONS
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"public relations|publicist|media relations|press relations|"
        r"corporate communications|communications manager|communications director|"
        r"communications specialist|communications coordinator|"
        r"communications officer|public affairs|press secretary|"
        r"external communications|internal communications"
        r")\b",
        t,
    ):
        return "Public Relations"

    # ------------------------------------------------------------------
    # 5) SALES / MARKETING / PROMOTIONS / REVENUE
    # ------------------------------------------------------------------
    if re.search(
        r"\b("
        r"account executive|account manager|sales executive|sales manager|"
        r"sales director|sales assistant|sales representative|sales consultant|"
        r"media consultant|marketing consultant|advertising consultant|"
        r"media sales|advertising sales|digital sales|local sales|national sales|"
        r"business development|revenue manager|revenue director|revenue operations|"
        r"marketing manager|marketing director|marketing coordinator|"
        r"marketing specialist|marketing assistant|brand marketing|brand manager|"
        r"brand ambassador|promotions assistant|promotions coordinator|"
        r"promotions manager|promotion coordinator|promotion manager|"
        r"event marketing|field marketing|affiliate sales|partnership sales|"
        r"sponsorship sales|sponsorship manager|sponsorship coordinator|"
        r"client services|client success|customer success"
        r")\b",
        t,
    ):
        return "Sales & Marketing"

    # ------------------------------------------------------------------
    # 6) JOURNALISM / NEWS / EDITORIAL
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
        r"fact checker|fact-checker"
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
        r"traffic reporter|play[- ]by[- ]play|sports talk host|"
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

    # Software/data/product engineering is treated as Digital for MJR.
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
                    (
                        "Remote"
                        if "remote" in (desc + " " + loc).lower()
                        else "On-Site"
                    ),
                    loc,
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
                (
                    "Remote"
                    if "remote" in (desc + " " + loc).lower()
                    else "On-Site"
                ),
                loc,
            )
        )

    return out


def generic(src):
    # Strict fallback: only individual pages with an explicit recent posted
    # date and a substantial description.
    r = req("GET", src["URL"])
    soup = BeautifulSoup(r.text, "html.parser")
    links = set()

    for a in soup.find_all("a", href=True):
        h = urljoin(src["URL"], a["href"])
        if any(
            x in h.lower()
            for x in ["/jobs/", "/job/", "jobdetail", "/details/"]
        ):
            links.add(h)

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
