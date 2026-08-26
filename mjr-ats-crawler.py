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

TODAY=date.today()
WINDOW_DAYS=int(os.getenv("MJR_WINDOW_DAYS","21"))
CUTOFF=TODAY-timedelta(days=WINDOW_DAYS)
SOURCES_FILE=Path(os.getenv("MJR_SOURCES","mjr-ats-sources.csv"))
OUTFILE=Path(os.getenv("MJR_OUTPUT","mjr-jboard-master.xml"))
AUDITFILE=Path(os.getenv("MJR_AUDIT","mjr-ats-audit.csv"))
STATE_FILE=Path(os.getenv("MJR_STATE","mjr-job-state.json"))
SESSION=requests.Session()
SESSION.headers.update({"User-Agent":"MJR-Jobs-Feed/1.0 (+https://www.mediajobsreport.com)"})
APPROVED={"Business Office","Digital","Engineering","Internships","Journalism","Management","Music Industry","Public Media / Higher Ed","Public Relations","Radio","Sales & Marketing","Television"}

def clean(s): return re.sub(r"\s+"," ",html.unescape(s or "")).strip()
def strip_html(s): return clean(BeautifulSoup(s or "","html.parser").get_text(" "))
def pdate(v):
    if not v:return None
    s=clean(v)
    m=re.search(r"(\d+)\s+days?\s+ago",s,re.I)
    if m:return TODAY-timedelta(days=int(m.group(1)))
    if "today" in s.lower():return TODAY
    if "yesterday" in s.lower():return TODAY-timedelta(days=1)
    try:return dtparser.parse(s,fuzzy=True).date()
    except:return None
def req(method,url,**kw):
    for n in range(4):
        try:
            r=SESSION.request(method,url,timeout=30,**kw)
            if r.status_code in (429,500,502,503,504):
                time.sleep(2**n);continue
            r.raise_for_status();return r
        except requests.RequestException:
            if n==3:raise
            time.sleep(2**n)

@dataclass
class Job:
    id:str;title:str;company:str;description:str;date:date;jobtype:str;category:str;url:str;source:str
    company_website:str="";logo:str="";work_arrangement:str="Not Specified";city:str="";state:str="";country:str="US"
    employer_deadline:date|None=None
    @property
    def expiration(self):
        life=30 if self.jobtype=="Internship" else 21
        days=max(1,life-(TODAY-self.date).days)
        if self.employer_deadline:
            d=(self.employer_deadline-TODAY).days
            if 1<=d<days:days=d
        return days

def jobtype(title,text=""):
    s=(title+" "+text).lower()
    if "intern" in s:return "Internship"
    if "part" in s and "time" in s:return "Part Time"
    if "temporary" in s or " temp " in " "+s:return "Temporary"
    if "contract" in s:return "Contract"
    return "Full Time"

def category(title,desc,industry,company):
    """
    MJR category classifier.
    Priority is JOB FUNCTION, especially the title.
    Employer/media type is only a final fallback.
    """
    t=clean(title).lower()
    d=clean(desc).lower()
    c=clean(company).lower()
    ind=clean(industry)

    # 1) Internships always win.
    if re.search(r"\b(intern|internship|trainee program)\b", t):
        return "Internships"

    # 2) Business/office functions should never become Sales simply because
    # the description says they "support sales" or work with revenue teams.
    if re.search(
        r"\b(accountant|accounting|accounts payable|accounts receivable|finance|financial|"
        r"controller|payroll|human resources|hr\b|people operations|talent acquisition|"
        r"recruiter|recruiting|administrative|administrator|executive assistant|"
        r"office assistant|office manager|legal|attorney|counsel|paralegal|"
        r"business affairs|contracts|compliance|procurement|purchasing|facilities|"
        r"receptionist|operations coordinator|sales operations coordinator|"
        r"traffic coordinator|traffic assistant|billing|credit|collections)\b", t
    ):
        return "Business Office"

    # 3) Engineering/technical.
    if re.search(
        r"\b(engineer|engineering|broadcast engineer|chief engineer|maintenance engineer|"
        r"systems engineer|network engineer|it\b|information technology|technical director|"
        r"broadcast technician|master control|transmission|transmitter|rf engineer|"
        r"studio technician|audio engineer|video engineer)\b", t
    ):
        return "Engineering"

    # 4) Public relations / communications.
    if re.search(
        r"\b(public relations|publicist|media relations|press relations|corporate communications|"
        r"communications manager|communications director|communications specialist|"
        r"communications coordinator|public affairs)\b", t
    ):
        return "Public Relations"

    # 5) Genuine sales/marketing/revenue roles.
    if re.search(
        r"\b(account executive|sales executive|sales manager|sales director|sales assistant|"
        r"media sales|advertising sales|digital sales|local sales|national sales|"
        r"business development|revenue manager|revenue director|revenue operations|"
        r"marketing manager|marketing director|marketing coordinator|marketing specialist|"
        r"brand marketing|affiliate sales|partnership sales|sponsorship sales)\b", t
    ):
        return "Sales & Marketing"

    # 6) Radio-specific programming/on-air/operations.
    if re.search(
        r"\b(board operator|on[- ]air|air personality|air talent|radio host|radio personality|"
        r"morning show|afternoon host|night host|music director|program director|"
        r"assistant program director|radio producer|radio news|traffic reporter|"
        r"play[- ]by[- ]play|sports talk host|announcer|disc jockey|dj\b)\b", t
    ):
        return "Radio"

    # 7) Television-specific production/studio roles.
    if re.search(
        r"\b(tv producer|television producer|newscast producer|executive producer|"
        r"associate producer|show producer|line producer|segment producer|"
        r"technical producer|studio manager|studio crew|camera operator|videographer|"
        r"photographer|photojournalist|director of photography|video editor|"
        r"news photographer|production assistant|production coordinator|"
        r"broadcast director|newscast director|floor director|graphics operator|"
        r"character generator|cg operator|master control operator)\b", t
    ):
        return "Television"

    # 8) Journalism/editorial roles independent of platform.
    if re.search(
        r"\b(reporter|journalist|news anchor|anchor/reporter|multimedia journalist|mmj\b|"
        r"assignment editor|assignment manager|news editor|managing editor|copy editor|"
        r"editorial|news writer|investigative reporter|correspondent|bureau chief|"
        r"news director|digital journalist|breaking news)\b", t
    ):
        return "Journalism"

    # 9) Digital product/content/social/podcast roles.
    if re.search(
        r"\b(digital producer|digital editor|digital content|web producer|web editor|"
        r"social media|social producer|audience development|seo\b|newsletter|"
        r"podcast producer|podcast editor|streaming producer|streaming editor|"
        r"product manager|product owner|mobile product|app product|ux\b|ui\b|"
        r"content strategist|digital strategist|ecommerce)\b", t
    ):
        return "Digital"

    # 10) Management only when the title is principally an executive/general-management role.
    if re.search(
        r"\b(general manager|market manager|president|chief executive officer|ceo\b|"
        r"chief operating officer|coo\b|chief content officer|station manager|"
        r"regional manager|vice president|vp\b|senior vice president|svp\b|"
        r"executive vice president|evp\b|head of)\b", t
    ):
        return "Management"

    # 11) Music-industry functions. Use employer context only after specific
    # office/sales/technical/etc. functions above have had the first chance.
    music_company = any(x in c for x in [
        "sony music","warner music","universal music","ascap","sesac","onerpm",
        "recording academy","music group","records","music entertainment"
    ])
    if music_company or re.search(
        r"\b(a&r|artist relations|artist development|record label|music publishing|"
        r"music licensing|royalties|royalty|sync licensing|repertoire|label manager)\b", t
    ):
        return "Music Industry"

    # 12) Public media / higher ed is a fallback for public-media employers
    # after the functional rules above.
    public_media = any(x in c for x in [
        "npr","pbs","american public media","public broadcasting","public radio",
        "university","college","state university"
    ])
    if public_media:
        return "Public Media / Higher Ed"

    # Description fallback is intentionally narrow and only used when title is vague.
    td = f" {t} {d[:1800]} "
    if re.search(r"\btelevision station\b|\btv station\b|\bnewscast\b|\bcamera\b|\bvideo production\b", td):
        return "Television"
    if re.search(r"\bradio station\b|\bon-air\b|\bcontrol board\b|\bbroadcast automation\b|\bplaylist\b", td):
        return "Radio"
    if re.search(r"\breporter\b|\bjournalism\b|\bnewsroom\b|\beditorial\b", td):
        return "Journalism"
    if re.search(r"\bdigital content\b|\bsocial media\b|\bwebsite\b|\bstreaming\b|\bpodcast\b", td):
        return "Digital"
    if re.search(r"\bengineering\b|\btransmitter\b|\btechnical systems\b|\bmaster control\b", td):
        return "Engineering"

    # Final source-family fallback. Avoid forcing all television-company jobs
    # into Television or all radio-company jobs into Radio when title is unclear.
    if ind == "Music Industry":
        return "Music Industry"
    if ind == "Journalism":
        return "Journalism"
    if ind == "Digital":
        return "Digital"
    if ind == "Radio":
        return "Radio"
    if ind == "Television":
        return "Television"

    return "Business Office"

def workday(src):
    u=urlparse(src["URL"]);host=u.netloc;tenant=host.split(".")[0]
    parts=[p for p in u.path.split("/") if p and p not in ("en-US","en-CA","jobs")]
    site=parts[0] if parts else ""
    if not tenant or not site or tenant=="myworkdaycenter":raise RuntimeError("Workday tenant/site not inferable")
    ep=f"https://{host}/wday/cxs/{tenant}/{site}/jobs";out=[];offset=0
    while True:
        d=req("POST",ep,json={"appliedFacets":{},"limit":20,"offset":offset,"searchText":""},headers={"Content-Type":"application/json"}).json()
        posts=d.get("jobPostings") or []
        if not posts:break
        for p in posts:
            ext=p.get("externalPath") or ""; 
            if not ext:continue
            info=req("GET",f"https://{host}/wday/cxs/{tenant}/{site}{ext}").json().get("jobPostingInfo",{})
            pd=pdate(info.get("postedOn") or p.get("postedOn"))
            if not pd or pd<CUTOFF:continue
            title=clean(info.get("title") or p.get("title"));desc=strip_html(info.get("jobDescription"))
            loc=clean(info.get("location") or p.get("locationsText"));url=info.get("externalUrl") or urljoin(src["URL"],ext)
            out.append(Job(info.get("jobReqId") or hashlib.sha1(url.encode()).hexdigest()[:16],title,src["Company"],desc,pd,jobtype(title,info.get("timeType","")),category(title,desc,src["Industry"],src["Company"]),url,src["URL"],src["URL"],"",("Remote" if "remote" in (desc+" "+loc).lower() else "On-Site"),loc))
        offset+=len(posts)
        if offset>=int(d.get("total") or offset):break
    return out

def greenhouse(src):
    parts=[p for p in urlparse(src["URL"]).path.split("/") if p];board=parts[0] if parts else ""
    if not board:raise RuntimeError("Greenhouse board missing")
    d=req("GET",f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true").json();out=[]
    for p in d.get("jobs",[]):
        pd=pdate(p.get("created_at")) or pdate(p.get("updated_at"))
        if not pd or pd<CUTOFF:continue
        title=clean(p.get("title"));desc=strip_html(p.get("content"));loc=clean((p.get("location") or {}).get("name"));url=p.get("absolute_url")
        out.append(Job(str(p.get("id")),title,src["Company"],desc,pd,jobtype(title),category(title,desc,src["Industry"],src["Company"]),url,src["URL"],src["URL"],"",("Remote" if "remote" in (desc+" "+loc).lower() else "On-Site"),loc))
    return out

def generic(src):
    # Strict fallback: only individual pages with an explicit recent posted date and substantial description.
    r=req("GET",src["URL"]);soup=BeautifulSoup(r.text,"html.parser");links=set()
    for a in soup.find_all("a",href=True):
        h=urljoin(src["URL"],a["href"])
        if any(x in h.lower() for x in ["/jobs/","/job/","jobdetail","/details/"]):links.add(h)
    out=[]
    for url in list(links)[:1000]:
        try:
            rr=req("GET",url);ss=BeautifulSoup(rr.text,"html.parser");txt=clean(ss.get_text(" "))
            h1=ss.find("h1");title=clean(h1.get_text(" ") if h1 else (ss.title.get_text(" ") if ss.title else ""))
            m=re.search(r"(?:posted|date posted|posted date)\s*:?\s*([A-Za-z]+\s+\d{1,2},\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2}|\d+\s+days?\s+ago)",txt,re.I)
            pd=pdate(m.group(1)) if m else None
            if not pd or pd<CUTOFF:continue
            main=ss.find("main") or ss.find("article") or ss
            desc=clean(main.get_text(" "))
            if len(desc)<250:continue
            out.append(Job(hashlib.sha1(url.encode()).hexdigest()[:16],title,src["Company"],desc,pd,jobtype(title,txt),category(title,desc,src["Industry"],src["Company"]),url,src["URL"],src["URL"]))
        except Exception:pass
    return out

def load_state():
    try:return json.loads(STATE_FILE.read_text())
    except:return {}
def stateful(jobs):
    st=load_state();now={j.url.rstrip("/").lower():j for j in jobs};ret=list(jobs)
    for k,j in now.items():
        st[k]={"misses":0,"last_seen":TODAY.isoformat(),"job":{x:(getattr(j,x).isoformat() if isinstance(getattr(j,x),date) else getattr(j,x)) for x in j.__dataclass_fields__}}
    for k,r in list(st.items()):
        if k in now:continue
        r["misses"]=int(r.get("misses",0))+1
        if r["misses"]>=2:del st[k];continue
        x=r.get("job",{})
        try:pd=date.fromisoformat(x["date"])
        except:del st[k];continue
        life=30 if x.get("jobtype")=="Internship" else 21
        if (TODAY-pd).days>=life:del st[k];continue
        dl=date.fromisoformat(x["employer_deadline"]) if x.get("employer_deadline") else None
        x["date"]=pd;x["employer_deadline"]=dl
        try:ret.append(Job(**x))
        except:pass
    STATE_FILE.write_text(json.dumps(st,indent=2,default=str))
    return ret

def write_xml(jobs):
    root=ET.Element("jobs")
    for j in sorted(jobs,key=lambda x:(x.company.lower(),x.title.lower(),x.city.lower())):
        e=ET.SubElement(root,"job")
        vals=[("id",j.id),("title",j.title),("company",j.company),("description",j.description),("date",j.date.isoformat()),("expiration",str(j.expiration)),("jobtype",j.jobtype),("category",j.category),("url",j.url),("source",j.source),("url_verified",TODAY.isoformat()),("company_website",j.company_website),("logo",j.logo),("work_arrangement",j.work_arrangement)]
        for t,v in vals:ET.SubElement(e,t).text=str(v or "")
        l=ET.SubElement(e,"location")
        for t,v in [("city",j.city),("state",j.state),("country",j.country)]:ET.SubElement(l,t).text=v or ""
    ET.indent(root,space="  ");ET.ElementTree(root).write(OUTFILE,encoding="utf-8",xml_declaration=True)

def main():
    with SOURCES_FILE.open(newline="",encoding="utf-8-sig") as f:sources=list(csv.DictReader(f))
    jobs=[];audit=[]
    for s in sources:
        if str(s.get("Active","True")).lower() in ("false","0","no"):continue
        try:
            a=s["ATS"].lower()
            got=workday(s) if "workday" in a else greenhouse(s) if "greenhouse" in a else generic(s)
            jobs+=got;audit.append([s["Company"],s["ATS"],s["URL"],"ok" if got else "zero_or_not_enumerable",len(got),""])
        except Exception as e:audit.append([s["Company"],s["ATS"],s["URL"],"error",0,repr(e)])
    ded={j.url.rstrip("/").lower():j for j in jobs if j.url and len(j.description)>=200 and j.date>=CUTOFF and j.category in APPROVED}
    jobs=stateful(list(ded.values()));ded={j.url.rstrip("/").lower():j for j in jobs};jobs=list(ded.values())
    write_xml(jobs)
    with AUDITFILE.open("w",newline="",encoding="utf-8") as f:
        w=csv.writer(f);w.writerow(["company","ats","url","status","jobs_collected","error"]);w.writerows(audit)
    print("Wrote",len(jobs),"jobs")
if __name__=="__main__":main()
