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
    s=(title+" "+desc).lower();c=company.lower();t=title.lower()
    if "intern" in t:return "Internships"
    if any(x in c for x in ["npr","pbs","american public media","whyy","public broadcasting"]):return "Public Media / Higher Ed"
    if any(x in c for x in ["sony music","warner music","universal music","ascap","sesac","onerpm"]):return "Music Industry"
    if re.search(r"\b(vp|vice president|general manager|news director|director|manager|chief|head of)\b",t):
        if re.search(r"\b(sales|marketing|revenue|account)\b",s):return "Sales & Marketing"
        if re.search(r"\b(engineer|engineering|technical|technology|transmission)\b",s):return "Engineering"
        return "Management"
    if re.search(r"\b(account executive|sales|advertising|marketing|business development|partnership)\b",s):return "Sales & Marketing"
    if re.search(r"\b(public relations|media relations|publicist|communications)\b",s):return "Public Relations"
    if re.search(r"\b(engineer|engineering|technical director|transmission|master control|broadcast technician)\b",s):return "Engineering"
    if re.search(r"\b(reporter|anchor|journalist|assignment editor|news writer|news producer|newscast|editorial)\b",s):
        return "Radio" if re.search(r"\b(radio|on[- ]air|board operator)\b",s) else "Journalism"
    if re.search(r"\b(radio|on[- ]air|board operator|air talent|morning show)\b",s):return "Radio"
    if re.search(r"\b(digital|social media|web producer|content creator|podcast|streaming)\b",s):return "Digital"
    if re.search(r"\b(tv|television|studio|camera|producer|production)\b",s):return "Television"
    if re.search(r"\b(accounting|finance|human resources|administrative|legal)\b",s):return "Business Office"
    return industry if industry in APPROVED else "Television"

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
