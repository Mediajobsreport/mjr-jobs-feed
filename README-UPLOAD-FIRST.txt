MJR GITHUB CRAWLER — UPLOAD INSTRUCTIONS

Upload the CONTENTS of this folder to the ROOT of your existing mjr-jobs-feed GitHub repository.

FILES:
1. mjr-ats-crawler.py
2. mjr-ats-sources.csv
3. mjr-jboard-master.xml
4. mjr-ats-audit.csv
5. mjr-job-state.json
6. requirements.txt
7. .github/workflows/update-mjr-feed.yml

IMPORTANT: Preserve the .github folder structure exactly.

AFTER UPLOAD:
1. Commit the files.
2. Open the repository's Actions tab.
3. Select "Update MJR Jobs XML".
4. Click "Run workflow".
5. When it completes, open mjr-ats-audit.csv.
6. The XML remains named mjr-jboard-master.xml, so your JBoard raw URL does not need to change.

RULES BUILT IN:
- Normal jobs: 21 days
- Internships: 30 days
- Employer earlier deadline wins when captured
- First missed crawl: retain job
- Second consecutive missed crawl: remove job
- One approved MJR category
- Numeric expiration field
- Direct ATS/employer URLs only
- Daily automatic GitHub Action plus manual runs

NOTE:
Workday and Greenhouse have structured enumeration in this first production package.
Other ATS types are included in the source control list but currently use a strict fallback.
Use mjr-ats-audit.csv to identify which ATS adapters we need to improve next.
