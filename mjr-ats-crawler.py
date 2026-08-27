#!/usr/bin/env python3

import csv
import hashlib
import html
import json
import os
import re
import time
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

SOURCES_FILE = Path(
    os.getenv("MJR_SOURCES", "mjr-ats-sources.csv")
)

OUTFILE = Path(
    os.getenv("MJR_OUTPUT", "mjr-jboard-master.xml")
)

AUDITFILE = Path(
    os.getenv("MJR_AUDIT", "mjr-ats-audit.csv")
)

STATE_FILE = Path(
    os.getenv("MJR_STATE", "mjr-job-state.json")
)


SESSION = requests.Session()

SESSION.headers.update(
    {
        "User-Agent":
        "MJR-Jobs-Feed/1.0 (+https://www.mediajobsreport.com)"
    }
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
    "Television"
}


def clean(s):
    return re.sub(
        r"\s+",
        " ",
        html.unescape(s or "")
    ).strip()


def strip_html(s):
    return clean(
        BeautifulSoup(
            s or "",
            "html.parser"
        ).get_text(" ")
    )


def pdate(v):

    if not v:
        return None

    s = clean(v)

    m = re.search(
        r"(\d+)\s+days?\s+ago",
        s,
        re.I
    )

    if m:
        return TODAY - timedelta(
            days=int(m.group(1))
        )

    if "today" in s.lower():
        return TODAY

    if "yesterday" in s.lower():
        return TODAY - timedelta(days=1)

    try:
        return dtparser.parse(
            s,
            fuzzy=True
        ).date()

    except Exception:
        return None


def req(method, url, **kw):

    for n in range(4):

        try:

            r = SESSION.request(
                method,
                url,
                timeout=30,
                **kw
            )

            if r.status_code in (
                429,
                500,
                502,
                503,
                504
            ):
                time.sleep(2 ** n)
                continue

            r.raise_for_status()

            return r

        except requests.RequestException:

            if n == 3:
                raise

            time.sleep(2 ** n)


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

        life = (
            30
            if self.jobtype == "Internship"
            else 21
        )

        days = max(
            1,
            life - (TODAY - self.date).days
        )

        if self.employer_deadline:

            d = (
                self.employer_deadline - TODAY
            ).days

            if 1 <= d < days:
                days = d

        return days


def jobtype(title, text=""):

    s = (
        title + " " + text
    ).lower()

    if "intern" in s:
        return "Internship"

    if (
        "part" in s
        and "time" in s
    ):
        return "Part Time"

    if (
        "temporary" in s
        or " temp " in " " + s
    ):
        return "Temporary"

    if "contract" in s:
        return "Contract"

    return "Full Time"


def category(title, desc, industry, company):
    """
    MJR job category classifier.

    Classification priority:
    1. Internship
    2. Specific job function from title
    3. Specialized media function
    4. Executive/general management
    5. Employer/media type fallback

    The job title is intentionally given much more weight
    than description text or employer industry.
    """

    t = clean(title).lower()
    d = clean(desc).lower()
    c = clean(company).lower()
    ind = clean(industry).lower()


    # ---------------------------------------------------------
    # 1. INTERNSHIPS
    # ---------------------------------------------------------

    if re.search(
        r"\b("
        r"intern|internship|internships|trainee program"
        r")\b",
        t
    ):
        return "Internships"


    # ---------------------------------------------------------
    # 2. BUSINESS OFFICE
    # ---------------------------------------------------------

    if re.search(
        r"\b("
        r"accountant|accounting|"
        r"accounts payable|accounts receivable|"
        r"finance|financial analyst|financial planning|"
        r"controller|payroll|bookkeeper|bookkeeping|"
        r"human resources|hr\b|"
        r"people operations|people partner|"
        r"talent acquisition|recruiter|recruiting|"
        r"administrative assistant|"
        r"administrative coordinator|administrator|"
        r"executive assistant|office assistant|"
        r"office manager|legal|attorney|counsel|"
        r"paralegal|business affairs|contracts|"
        r"contract administrator|compliance|"
        r"procurement|purchasing|facilities|"
        r"receptionist|billing|credit|collections|"
        r"traffic coordinator|traffic assistant|"
        r"traffic manager|"
        r"sales operations coordinator|"
        r"revenue operations coordinator|"
        r"business operations coordinator"
        r")\b",
        t
    ):
        return "Business Office"


    # ---------------------------------------------------------
    # 3. ENGINEERING / TECHNICAL
    # ---------------------------------------------------------

    if re.search(
        r"\b("
        r"chief engineer|broadcast engineer|"
        r"broadcast engineering|maintenance engineer|"
        r"systems engineer|network engineer|"
        r"rf engineer|audio engineer|video engineer|"
        r"engineer|engineering|"
        r"information technology|it manager|"
        r"it director|it specialist|"
        r"technical director|technical operations|"
        r"broadcast technician|studio technician|"
        r"master control|transmission|transmitter|"
        r"broadcast systems|network administrator|"
        r"systems administrator"
        r")\b",
        t
    ):
        return "Engineering"


    # ---------------------------------------------------------
    # 4. PUBLIC RELATIONS / COMMUNICATIONS
    # ---------------------------------------------------------

    if re.search(
        r"\b("
        r"public relations|publicist|"
        r"media relations|press relations|"
        r"corporate communications|"
        r"communications manager|"
        r"communications director|"
        r"communications specialist|"
        r"communications coordinator|"
        r"communications officer|"
        r"public affairs|press secretary"
        r")\b",
        t
    ):
        return "Public Relations"


    # ---------------------------------------------------------
    # 5. SALES & MARKETING
    # ---------------------------------------------------------

    if re.search(
        r"\b("
        r"account executive|account manager|"
        r"senior account executive|"
        r"sales executive|sales representative|"
        r"sales consultant|media consultant|"
        r"marketing consultant|"
        r"integrated marketing consultant|"
        r"sales manager|sales director|"
        r"director of sales|"
        r"vice president of sales|vp of sales|"
        r"senior vice president of sales|"
        r"svp of sales|"
        r"sales assistant|sales coordinator|"
        r"media sales|advertising sales|"
        r"digital sales|local sales|"
        r"national sales|regional sales|"
        r"business development|"
        r"client development|"
        r"revenue manager|revenue director|"
        r"revenue operations|"
        r"marketing manager|marketing director|"
        r"director of marketing|"
        r"vice president of marketing|"
        r"vp of marketing|"
        r"marketing coordinator|"
        r"marketing specialist|"
        r"brand marketing|affiliate sales|"
        r"partnership sales|sponsorship sales|"
        r"sponsorship manager|"
        r"promotions director|promotions manager|"
        r"promotion director|promotion manager|"
        r"advertising director"
        r")\b",
        t
    ):
        return "Sales & Marketing"


    # ---------------------------------------------------------
    # 6. JOURNALISM
    # ---------------------------------------------------------

    if re.search(
        r"\b("
        r"reporter|journalist|news anchor|"
        r"anchor reporter|anchor/reporter|"
        r"multimedia journalist|mmj\b|"
        r"correspondent|investigative reporter|"
        r"investigative journalist|"
        r"assignment editor|assignment manager|"
        r"assignment desk|news editor|"
        r"managing editor|copy editor|copywriter|"
        r"editorial writer|editorial producer|"
        r"news writer|news director|"
        r"assistant news director|bureau chief|"
        r"digital journalist|breaking news|"
        r"meteorologist|chief meteorologist|"
        r"weather anchor|sports anchor|"
        r"sports reporter|sports journalist|"
        r"photojournalist|news photographer"
        r")\b",
        t
    ):
        return "Journalism"


    # ---------------------------------------------------------
    # 7. RADIO
    # ---------------------------------------------------------

    if re.search(
        r"\b("
        r"board operator|board op|"
        r"on[- ]air|air personality|air talent|"
        r"radio host|radio personality|"
        r"morning host|morning show host|"
        r"morning show|afternoon host|"
        r"afternoon personality|night host|"
        r"evening host|music director|"
        r"program director|"
        r"assistant program director|apd\b|"
        r"radio producer|radio news|"
        r"traffic reporter|play[- ]by[- ]play|"
        r"sports talk host|talk host|"
        r"announcer|disc jockey|dj\b|"
        r"radio programmer|radio programming"
        r")\b",
        t
    ):
        return "Radio"


    # ---------------------------------------------------------
    # 8. TELEVISION
    # ---------------------------------------------------------

    if re.search(
        r"\b("
        r"tv producer|television producer|"
        r"newscast producer|news producer|"
        r"executive producer|associate producer|"
        r"show producer|line producer|"
        r"segment producer|technical producer|"
        r"studio manager|studio crew|"
        r"camera operator|camera technician|"
        r"videographer|director of photography|"
        r"video editor|production assistant|"
        r"production coordinator|"
        r"production manager|"
        r"broadcast director|newscast director|"
        r"floor director|graphics operator|"
        r"character generator|cg operator|"
        r"master control operator|"
        r"television director|tv director"
        r")\b",
        t
    ):
        return "Television"


    # ---------------------------------------------------------
    # 9. DIGITAL
    # ---------------------------------------------------------

    if re.search(
        r"\b("
        r"digital producer|digital editor|"
        r"digital content|"
        r"digital content producer|"
        r"digital content manager|"
        r"web producer|web editor|web content|"
        r"social media|social producer|"
        r"social editor|audience development|"
        r"audience engagement|seo\b|"
        r"search engine optimization|"
        r"newsletter|newsletter editor|"
        r"newsletter producer|"
        r"podcast producer|podcast editor|"
        r"podcast host|streaming producer|"
        r"streaming editor|product manager|"
        r"product owner|digital product|"
        r"mobile product|app product|"
        r"ux\b|ui\b|content strategist|"
        r"digital strategist|"
        r"ecommerce|e-commerce"
        r")\b",
        t
    ):
        return "Digital"


    # ---------------------------------------------------------
    # 10. MUSIC INDUSTRY
    # ---------------------------------------------------------

    if re.search(
        r"\b("
        r"a&r|artists and repertoire|"
        r"artist relations|artist development|"
        r"artist services|record label|"
        r"label manager|label operations|"
        r"music publishing|music publisher|"
        r"music licensing|sync licensing|"
        r"synch licensing|music rights|"
        r"rights management|royalties|royalty|"
        r"repertoire|catalog manager|"
        r"catalog management|music supervisor|"
        r"music coordinator"
        r")\b",
        t
    ):
        return "Music Industry"


    # ---------------------------------------------------------
    # 11. MANAGEMENT
    # ---------------------------------------------------------

    if re.search(
        r"\b("
        r"general manager|market manager|"
        r"station manager|regional manager|"
        r"market president|president|"
        r"chief executive officer|ceo\b|"
        r"chief operating officer|coo\b|"
        r"chief content officer|"
        r"chief revenue officer|cro\b|"
        r"chief financial officer|cfo\b|"
        r"executive vice president|evp\b|"
        r"senior vice president|svp\b|"
        r"vice president|vp\b|head of"
        r")\b",
        t
    ):
        return "Management"


    # ---------------------------------------------------------
    # 12. EMPLOYER-SPECIFIC FALLBACKS
    # ---------------------------------------------------------

    music_company = any(
        x in c
        for x in [
            "sony music",
            "warner music",
            "universal music",
            "universal music group",
            "atlantic records",
            "warner records",
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
            "music entertainment"
        ]
    )

    if music_company:
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
            "state university"
        ]
    )

    if public_media:
        return "Public Media / Higher Ed"


    # ---------------------------------------------------------
    # 13. DESCRIPTION FALLBACK
    # ---------------------------------------------------------

    td = f" {t} {d[:1500]} "


    if re.search(
        r"\breporter\b|"
        r"\bjournalism\b|"
        r"\bnewsroom\b|"
        r"\beditorial\b|"
        r"\bnews gathering\b",
        td
    ):
        return "Journalism"


    if re.search(
        r"\btelevision station\b|"
        r"\btv station\b|"
        r"\bnewscast\b|"
        r"\bvideo production\b|"
        r"\bstudio production\b",
        td
    ):
        return "Television"


    if re.search(
        r"\bradio station\b|"
        r"\bon-air\b|"
        r"\bcontrol board\b|"
        r"\bbroadcast automation\b|"
        r"\bplaylist\b",
        td
    ):
        return "Radio"


    if re.search(
        r"\bdigital content\b|"
        r"\bsocial media\b|"
        r"\bwebsite\b|"
        r"\bstreaming\b|"
        r"\bpodcast\b",
        td
    ):
        return "Digital"


    if re.search(
        r"\bengineering\b|"
        r"\btransmitter\b|"
        r"\btechnical systems\b|"
        r"\bmaster control\b",
        td
    ):
        return "Engineering"


    # ---------------------------------------------------------
    # 14. SOURCE INDUSTRY FALLBACK
    # ---------------------------------------------------------

    if ind == "music industry":
        return "Music Industry"

    if ind == "journalism":
        return "Journalism"

    if ind == "digital":
        return "Digital"

    if ind == "radio":
        return "Radio"

    if ind == "television":
        return "Television"

    if ind in (
        "public media",
        "higher ed",
        "public media / higher ed"
    ):
        return "Public Media / Higher Ed"


    # ---------------------------------------------------------
    # 15. SAFE DEFAULT
    # ---------------------------------------------------------

    return "Business Office"


def workday(src):

    u = urlparse(src["URL"])

    host = u.netloc

    tenant = host.split(".")[0]

    parts = [
        p
        for p in u.path.split("/")
        if p
        and p not in (
            "en-US",
            "en-CA",
            "jobs"
        )
    ]

    site = (
        parts[0]
        if parts
        else ""
    )

    if (
        not tenant
        or not site
        or tenant == "myworkdaycenter"
    ):
        raise RuntimeError(
            "Workday tenant/site not inferable"
        )

    ep = (
        f"https://{host}/wday/cxs/"
        f"{tenant}/{site}/jobs"
    )

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
                "searchText": ""
            },
            headers={
                "Content-Type":
                "application/json"
            }
        ).json()


        posts = (
            d.get("jobPostings")
            or []
        )

        if not posts:
            break


        for p in posts:

            ext = (
                p.get("externalPath")
                or ""
            )

            if not ext:
                continue


            info = req(
                "GET",
                f"https://{host}/wday/cxs/"
                f"{tenant}/{site}{ext}"
            ).json().get(
                "jobPostingInfo",
                {}
            )


            pd = pdate(
                info.get("postedOn")
                or p.get("postedOn")
            )

            if (
                not pd
                or pd < CUTOFF
            ):
                continue


            title = clean(
                info.get("title")
                or p.get("title")
            )

            desc = strip_html(
                info.get("jobDescription")
            )

            loc = clean(
                info.get("location")
                or p.get("locationsText")
            )

            url = (
                info.get("externalUrl")
                or urljoin(
                    src["URL"],
                    ext
                )
            )


            out.append(
                Job(
                    info.get("jobReqId")
                    or hashlib.sha1(
                        url.encode()
                    ).hexdigest()[:16],

                    title,
                    src["Company"],
                    desc,
                    pd,

                    jobtype(
                        title,
                        info.get(
                            "timeType",
                            ""
                        )
                    ),

                    category(
                        title,
                        desc,
                        src["Industry"],
                        src["Company"]
                    ),

                    url,
                    src["URL"],
                    src["URL"],
                    "",

                    (
                        "Remote"
                        if "remote" in (
                            desc + " " + loc
                        ).lower()
                        else "On-Site"
                    ),

                    loc
                )
            )


        offset += len(posts)

        if offset >= int(
            d.get("total")
            or offset
        ):
            break


    return out


def greenhouse(src):

    parts = [
        p
        for p in urlparse(
            src["URL"]
        ).path.split("/")
        if p
    ]

    board = (
        parts[0]
        if parts
        else ""
    )

    if not board:
        raise RuntimeError(
            "Greenhouse board missing"
        )


    d = req(
        "GET",
        f"https://boards-api.greenhouse.io/"
        f"v1/boards/{board}/jobs?content=true"
    ).json()


    out = []


    for p in d.get(
        "jobs",
        []
    ):

        pd = (
            pdate(
                p.get("created_at")
            )
            or pdate(
                p.get("updated_at")
            )
        )

        if (
            not pd
            or pd < CUTOFF
        ):
            continue


        title = clean(
            p.get("title")
        )

        desc = strip_html(
            p.get("content")
        )

        loc = clean(
            (
                p.get("location")
                or {}
            ).get("name")
        )

        url = p.get(
            "absolute_url"
        )


        out.append(
            Job(
                str(
                    p.get("id")
                ),
                title,
                src["Company"],
                desc,
                pd,
                jobtype(title),

                category(
                    title,
                    desc,
                    src["Industry"],
                    src["Company"]
                ),

                url,
                src["URL"],
                src["URL"],
                "",

                (
                    "Remote"
                    if "remote" in (
                        desc + " " + loc
                    ).lower()
                    else "On-Site"
                ),

                loc
            )
        )


    return out


def generic(src):
    """
    Strict generic fallback.

    Only collects individual job pages
    containing an explicit recent posting date
    and a substantial description.
    """

    r = req(
        "GET",
        src["URL"]
    )

    soup = BeautifulSoup(
        r.text,
        "html.parser"
    )

    links = set()


    for a in soup.find_all(
        "a",
        href=True
    ):

        h = urljoin(
            src["URL"],
            a["href"]
        )

        if any(
            x in h.lower()
            for x in [
                "/jobs/",
                "/job/",
                "jobdetail",
                "/details/"
            ]
        ):
            links.add(h)


    out = []


    for url in list(
        links
    )[:1000]:

        try:

            rr = req(
                "GET",
                url
            )

            ss = BeautifulSoup(
                rr.text,
                "html.parser"
            )

            txt = clean(
                ss.get_text(" ")
            )

            h1 = ss.find("h1")

            title = clean(
                h1.get_text(" ")
                if h1
                else (
                    ss.title.get_text(" ")
                    if ss.title
                    else ""
                )
            )


            m = re.search(
                r"(?:posted|date posted|posted date)"
                r"\s*:?\s*("
                r"[A-Za-z]+\s+\d{1,2},\s+20\d{2}|"
                r"\d{1,2}/\d{1,2}/20\d{2}|"
                r"\d+\s+days?\s+ago"
                r")",
                txt,
                re.I
            )


            pd = (
                pdate(m.group(1))
                if m
                else None
            )


            if (
                not pd
                or pd < CUTOFF
            ):
                continue


            main = (
                ss.find("main")
                or ss.find("article")
                or ss
            )

            desc = clean(
                main.get_text(" ")
            )


            if len(desc) < 250:
                continue


            out.append(
                Job(
                    hashlib.sha1(
                        url.encode()
                    ).hexdigest()[:16],

                    title,
                    src["Company"],
                    desc,
                    pd,
                    jobtype(
                        title,
                        txt
                    ),

                    category(
                        title,
                        desc,
                        src["Industry"],
                        src["Company"]
                    ),

                    url,
                    src["URL"],
                    src["URL"]
                )
            )


        except Exception:
            pass


    return out


def load_state():

    try:
        return json.loads(
            STATE_FILE.read_text()
        )

    except Exception:
        return {}


def stateful(jobs):

    st = load_state()

    now = {
        j.url.rstrip("/").lower(): j
        for j in jobs
    }

    ret = list(jobs)


    for k, j in now.items():

        st[k] = {
            "misses": 0,
            "last_seen": TODAY.isoformat(),
            "job": {
                x: (
                    getattr(j, x).isoformat()
                    if isinstance(
                        getattr(j, x),
                        date
                    )
                    else getattr(j, x)
                )
                for x
                in j.__dataclass_fields__
            }
        }


    for k, r in list(
        st.items()
    ):

        if k in now:
            continue


        r["misses"] = (
            int(
                r.get(
                    "misses",
                    0
                )
            )
            + 1
        )


        if r["misses"] >= 2:

            del st[k]

            continue


        x = r.get(
            "job",
            {}
        )


        try:
            pd = date.fromisoformat(
                x["date"]
            )

        except Exception:

            del st[k]

            continue


        life = (
            30
            if x.get("jobtype")
            == "Internship"
            else 21
        )


        if (
            TODAY - pd
        ).days >= life:

            del st[k]

            continue


        dl = (
            date.fromisoformat(
                x["employer_deadline"]
            )
            if x.get(
                "employer_deadline"
            )
            else None
        )


        x["date"] = pd

        x["employer_deadline"] = dl


        try:
            ret.append(
                Job(**x)
            )

        except Exception:
            pass


    STATE_FILE.write_text(
        json.dumps(
            st,
            indent=2,
            default=str
        )
    )


    return ret


def write_xml(jobs):

    root = ET.Element(
        "jobs"
    )


    for j in sorted(
        jobs,
        key=lambda x: (
            x.company.lower(),
            x.title.lower(),
            x.city.lower()
        )
    ):

        e = ET.SubElement(
            root,
            "job"
        )


        vals = [
            ("id", j.id),
            ("title", j.title),
            ("company", j.company),
            (
                "description",
                j.description
            ),
            (
                "date",
                j.date.isoformat()
            ),
            (
                "expiration",
                str(j.expiration)
            ),
            (
                "jobtype",
                j.jobtype
            ),
            (
                "category",
                j.category
            ),
            ("url", j.url),
            ("source", j.source),

            (
                "url_verified",
                TODAY.isoformat()
            ),

            (
                "company_website",
                j.company_website
            ),

            (
                "logo",
                j.logo
            ),

            (
                "work_arrangement",
                j.work_arrangement
            )
        ]


        for t, v in vals:

            ET.SubElement(
                e,
                t
            ).text = str(
                v or ""
            )


        l = ET.SubElement(
            e,
            "location"
        )


        for t, v in [
            ("city", j.city),
            ("state", j.state),
            ("country", j.country)
        ]:

            ET.SubElement(
                l,
                t
            ).text = (
                v or ""
            )


    ET.indent(
        root,
        space="  "
    )


    ET.ElementTree(
        root
    ).write(
        OUTFILE,
        encoding="utf-8",
        xml_declaration=True
    )


def main():

    with SOURCES_FILE.open(
        newline="",
        encoding="utf-8-sig"
    ) as f:

        sources = list(
            csv.DictReader(f)
        )


    jobs = []

    audit = []


    for s in sources:

        if str(
            s.get(
                "Active",
                "True"
            )
        ).lower() in (
            "false",
            "0",
            "no"
        ):
            continue


        try:

            a = s["ATS"].lower()


            if "workday" in a:

                got = workday(s)

            elif "greenhouse" in a:

                got = greenhouse(s)

            else:

                got = generic(s)


            jobs += got


            audit.append(
                [
                    s["Company"],
                    s["ATS"],
                    s["URL"],
                    (
                        "ok"
                        if got
                        else
                        "zero_or_not_enumerable"
                    ),
                    len(got),
                    ""
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
                    repr(e)
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


    jobs = stateful(
        list(
            ded.values()
        )
    )


    ded = {
        j.url.rstrip("/").lower(): j
        for j in jobs
    }


    jobs = list(
        ded.values()
    )


    write_xml(
        jobs
    )


    with AUDITFILE.open(
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        w = csv.writer(f)

        w.writerow(
            [
                "company",
                "ats",
                "url",
                "status",
                "jobs_collected",
                "error"
            ]
        )

        w.writerows(
            audit
        )


    print(
        "Wrote",
        len(jobs),
        "jobs"
    )


if __name__ == "__main__":
    main()
