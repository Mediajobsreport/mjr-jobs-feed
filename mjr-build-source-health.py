#!/usr/bin/env python3
"""Build the MJR Job Crawl Health Dashboard JSON after a production crawl.

Inputs are the crawler's existing audit and state files. The previous health
file is used to identify jobs first seen in the current crawl and to preserve
source history. No network access is required.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


AUDIT_FILE = Path(os.getenv("MJR_AUDIT", "mjr-ats-audit.csv"))
STATE_FILE = Path(os.getenv("MJR_STATE", "mjr-job-state.json"))
OUTPUT_FILE = Path(os.getenv("MJR_HEALTH_OUTPUT", "mjr-job-source-health.json"))
MAX_LATEST_JOBS = 5
SCHEMA_VERSION = 1


def text(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_day(value: Any) -> date | None:
    raw = text(value)[:10]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def job_fingerprint(job: dict[str, Any], fallback: str) -> str:
    identity = text(job.get("url") or job.get("id") or fallback).lower()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def status_for(last_new: date | None, has_error: bool, today: date) -> tuple[str, int | None]:
    if has_error:
        return "red", (today - last_new).days if last_new else None
    if not last_new:
        return "red", None
    days = max(0, (today - last_new).days)
    if days <= 2:
        return "green", days
    if days <= 4:
        return "yellow", days
    if days <= 6:
        return "orange", days
    return "red", days


def main() -> None:
    if not AUDIT_FILE.exists():
        raise SystemExit(f"Missing audit file: {AUDIT_FILE}")
    if not STATE_FILE.exists():
        raise SystemExit(f"Missing state file: {STATE_FILE}")

    generated_at = iso_now()
    today = date.today()
    previous = load_json(OUTPUT_FILE, {})
    previous_sources = {
        text(item.get("company")): item
        for item in previous.get("sources", [])
        if isinstance(item, dict) and text(item.get("company"))
    }

    raw_state = load_json(STATE_FILE, {})
    if not isinstance(raw_state, dict):
        raise SystemExit(f"State file must contain a JSON object: {STATE_FILE}")

    jobs_by_company: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fallback, record in raw_state.items():
        if not isinstance(record, dict):
            continue
        job = record.get("job")
        if not isinstance(job, dict):
            continue
        company = text(job.get("company"))
        if not company:
            continue
        normalized = dict(job)
        normalized["_fingerprint"] = job_fingerprint(job, str(fallback))
        normalized["_last_seen"] = text(record.get("last_seen"))
        jobs_by_company[company].append(normalized)

    with AUDIT_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        audit_rows = list(csv.DictReader(handle))

    sources: list[dict[str, Any]] = []
    total_new = 0
    total_active = 0

    for row in audit_rows:
        company = text(row.get("company"))
        if not company:
            continue
        previous_item = previous_sources.get(company, {})
        has_previous_run = bool(previous_item)
        previous_ids = set(previous_item.get("active_job_ids", []))
        company_jobs = jobs_by_company.get(company, [])
        active_ids = sorted({job["_fingerprint"] for job in company_jobs})
        newly_seen = [job for job in company_jobs if job["_fingerprint"] not in previous_ids]

        def job_day(job: dict[str, Any]) -> date:
            return parse_day(job.get("date")) or parse_day(job.get("_last_seen")) or date.min

        newly_seen.sort(key=job_day, reverse=True)
        company_jobs.sort(key=job_day, reverse=True)
        latest_pool = newly_seen or company_jobs
        latest_jobs = [
            {
                "title": text(job.get("title")),
                "date": text(job.get("date") or job.get("_last_seen"))[:10],
                "url": text(job.get("url")),
            }
            for job in latest_pool[:MAX_LATEST_JOBS]
        ]

        prior_last_new = parse_day(previous_item.get("last_new_job_at"))
        newest_discovered = today if has_previous_run and newly_seen else None
        newest_known = max((job_day(job) for job in company_jobs), default=None)
        last_new = max(
            (item for item in (prior_last_new, newest_discovered, newest_known) if item),
            default=None,
        )

        crawl_state = text(row.get("status")).lower()
        error_text = text(row.get("error"))
        has_error = crawl_state == "error"
        health_status, days_since_new = status_for(last_new, has_error, today)

        if has_error:
            attention = "Crawl error"
        elif not last_new:
            attention = "No job history"
        elif health_status == "green":
            attention = "Healthy"
        else:
            attention = f"No newly posted jobs for {days_since_new} days"

        new_count = len(newly_seen) if has_previous_run else 0
        active_count = len(active_ids)
        total_new += new_count
        total_active += active_count

        sources.append(
            {
                "company": company,
                "ats": text(row.get("ats")),
                "source_url": text(row.get("url")),
                "status": health_status,
                "attention_reason": attention,
                "days_since_new_job": days_since_new,
                "last_new_job_at": last_new.isoformat() if last_new else None,
                "last_crawl_at": generated_at,
                "last_successful_crawl_at": (
                    generated_at if not has_error else previous_item.get("last_successful_crawl_at")
                ),
                "crawl_result": crawl_state or "unknown",
                "jobs_collected_this_crawl": int(row.get("jobs_collected") or 0),
                "new_jobs_this_crawl": new_count,
                "active_jobs": active_count,
                "latest_jobs": latest_jobs,
                "error": error_text if has_error else "",
                "note": error_text if error_text and not has_error else "",
                "active_job_ids": active_ids,
            }
        )

    priority = {"red": 0, "orange": 1, "yellow": 2, "green": 3}
    sources.sort(key=lambda item: (priority.get(item["status"], 9), item["company"].lower()))
    counts = Counter(item["status"] for item in sources)
    error_count = sum(1 for item in sources if item["crawl_result"] == "error")
    zero_count = sum(1 for item in sources if item["crawl_result"] != "ok" and item["crawl_result"] != "error")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "thresholds": {"green": "0-2", "yellow": "3-4", "orange": "5-6", "red": "7+ or error"},
        "summary": {
            "sources": len(sources),
            "green": counts["green"],
            "yellow": counts["yellow"],
            "orange": counts["orange"],
            "red": counts["red"],
            "crawl_errors": error_count,
            "zero_or_not_enumerable": zero_count,
            "new_jobs_this_crawl": total_new,
            "active_jobs": total_active,
        },
        "sources": sources,
    }

    temp = OUTPUT_FILE.with_suffix(OUTPUT_FILE.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(OUTPUT_FILE)
    print(f"Wrote {OUTPUT_FILE}: {len(sources)} sources, {total_active} active jobs")


if __name__ == "__main__":
    main()
