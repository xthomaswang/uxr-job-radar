from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse
from pathlib import Path

import httpx

from .core import Job, text_content, public_https


def feed_url(source: dict) -> str:
    slug = source["slug"]
    if not slug or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in slug):
        raise ValueError("Invalid board slug")
    return {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json",
        "recruitee": f"https://{slug}.recruitee.com/api/offers/",
    }[source["ats"]]


def parse_feed(source: dict, payload) -> list[Job]:
    ats = source["ats"]
    rows = payload if ats == "lever" else payload.get("offers" if ats == "recruitee" else "jobs")
    if not isinstance(rows, list):
        raise ValueError("Invalid/incomplete feed: expected jobs array")
    jobs = []
    for row in rows:
        if ats == "greenhouse":
            jid = str(row["id"])
            title, url = row["title"], row["absolute_url"]
            desc = text_content(row["content"])
            loc = (row.get("location") or {}).get("name", "unknown")
            posted, employment = row.get("first_published"), "unknown"
        elif ats == "ashby":
            # Unlisted positions are not part of the public board.
            if row.get("isListed") is False:
                continue
            url = row["jobUrl"]
            jid = url.rstrip("/").split("/")[-1]
            title = row["title"]
            desc = row.get("descriptionPlain") or text_content(row.get("descriptionHtml", ""))
            loc = row.get("location", "unknown")
            posted, employment = row.get("publishedAt"), row.get("employmentType", "unknown")
        elif ats == "recruitee":
            # Public Careers Site API currently permits no-key reads; its official
            # docs announce mandatory employer tokens from 2027-02-10. A future
            # 401 must surface as a source failure, never an empty/closed board.
            jid, title, url = str(row["id"]), row["title"], row["careers_url"]
            desc = text_content((row.get("description") or "") + " " + (row.get("requirements") or ""))
            loc = row.get("location") or ", ".join(x for x in (row.get("city"), row.get("country")) if x) or "unknown"
            posted, employment = row.get("published_at"), row.get("employment_type_code") or "unknown"
        else:
            jid, title, url = row["id"], row["text"], row["hostedUrl"]
            desc = text_content(row.get("description", "") + " " + " ".join(x.get("text", "") + " " + x.get("content", "") for x in row.get("lists", [])) + " " + row.get("additional", ""))
            loc = row.get("categories", {}).get("location", "unknown")
            posted, employment = None, row.get("categories", {}).get("commitment", "unknown")
        if not desc or not public_https(url):
            raise ValueError(f"Incomplete job {jid}: abort snapshot instead of closing absent jobs")
        jobs.append(Job(key=f"{source['id']}:{jid}", source=source["id"], company=source["company"], company_kind=source["kind"], source_id=jid, title=title, location=loc, url=url, description=desc, posted_at=posted, employment_type=employment))
    if len({j.key for j in jobs}) != len(jobs):
        raise ValueError("Duplicate source IDs")
    return jobs


def fetch_feed(client: httpx.Client, source: dict) -> tuple[list[Job], dict | list]:
    if source["ats"] == "aggregator":
        from .aggregators import fetch_aggregator
        return fetch_aggregator(client, source)
    if source["ats"] == "workday":
        return fetch_workday(client, source)
    if source["ats"] == "smartrecruiters":
        return fetch_smartrecruiters(client, source)
    if source["ats"] == "amazon":
        return fetch_amazon(client, source)
    if source["ats"] == "google":
        return fetch_google(client, source)
    r = client.get(feed_url(source))
    r.raise_for_status()
    payload = r.json()
    return parse_feed(source, payload), payload


def fetch_google(client, source):
    """Read the official careers UX category, including non-research UX roles.

    All category results enter the LLM queue; no seniority or title exclusion.
    Missing/changing embedded-data format is an error, never an empty board.
    """
    root="https://www.google.com/about/careers/applications/jobs/results/"
    rows=[];seen=set();total=None
    for page in range(1,51):
        r=client.get(root,params={"category":"USER_EXPERIENCE","sort_by":"date","page":page})
        r.raise_for_status()
        match=re.search(r"AF_initDataCallback\(\{key:\s*'ds:1',\s*hash:\s*'[^']*',\s*data:(.*?),\s*sideChannel:",r.text,re.S)
        if not match:raise ValueError("Google embedded job data unavailable")
        payload=json.loads(match.group(1))
        batch=payload[0] or [];total=payload[2]
        if not isinstance(total,int):raise ValueError("Google total count unavailable")
        for item in batch:
            if str(item[0]) in seen:raise ValueError("Repeated Google page: incomplete snapshot")
            seen.add(str(item[0]));rows.append(item)
        if len(rows)>=total:break
        if not batch:raise ValueError("Google pagination ended before total")
    if len(rows)!=total:raise ValueError("Google scan was capped/incomplete")
    jobs=[]
    for row in rows:
        jid=str(row[0])
        if not jid.isdigit():raise ValueError("Unexpected Google job identifier")
        chunks=[row[i][1] for i in (3,4,10,15,18) if len(row)>i and isinstance(row[i],list) and len(row[i])>1 and isinstance(row[i][1],str)]
        desc=text_content(" ".join(chunks))
        if not desc:raise ValueError("Google description missing")
        jobs.append(Job(key=f"{source['id']}:{jid}",source=source["id"],source_id=jid,company=source["company"],company_kind=source["kind"],title=row[1],location=" / ".join(x[0] for x in row[9] or []),url=root+jid,description=desc))
    return jobs,{"jobs":rows,"total":total,"category":"USER_EXPERIENCE"}


def fetch_amazon(client, source):
    """Union of broad official searches; collect every result, then let the LLM judge."""
    jobs={};raw={}
    queries=source.get("queries")
    if not queries:raise ValueError("Amazon requires explicit coverage queries")
    for query in queries:
        rows=[];seen=set();expected=None;pages=[]
        for offset in range(0,20000,100):
            r=client.get("https://www.amazon.jobs/en/search.json",params={"base_query":query,"offset":offset,"result_limit":100,"sort":"recent"})
            r.raise_for_status();data=r.json()
            if data.get("error") or not isinstance(data.get("jobs"),list) or not isinstance(data.get("hits"),int):
                raise ValueError("Amazon search is incomplete")
            if expected is None:expected=data["hits"]
            elif expected!=data["hits"]:raise ValueError("Amazon result total changed during scan; retry later")
            batch=data["jobs"];pages.append(data)
            for row in batch:
                jid=str(row["id_icims"])
                if jid in seen:raise ValueError("Repeated Amazon result during pagination")
                seen.add(jid);rows.append(row)
            if len(rows)>=expected:break
            if not batch:raise ValueError("Amazon pagination ended early")
        if len(rows)!=expected:raise ValueError("Amazon scan was capped/incomplete")
        raw[query]=pages
        for row in rows:
            jid=str(row["id_icims"])
            if not jid.isdigit() or not row["job_path"].startswith("/en/jobs/"+jid+"/"):
                raise ValueError("Unexpected Amazon job identity")
            # Preserve the source's qualification boundaries. Flattening these
            # separate API fields makes preferred degrees/years look mandatory.
            sections = []
            for label, field in (("DESCRIPTION", "description"),
                                 ("BASIC QUALIFICATIONS", "basic_qualifications"),
                                 ("PREFERRED QUALIFICATIONS", "preferred_qualifications")):
                body = text_content(row.get(field) or "")
                if body:
                    sections.append(f"{label}:\n{body}")
            desc = "\n\n".join(sections)
            if not desc:raise ValueError("Amazon description missing")
            jobs[jid]=Job(key=f"{source['id']}:{jid}",source=source["id"],source_id=jid,company=source["company"],company_kind=source["kind"],title=row["title"],location=row.get("location") or "unknown",url="https://www.amazon.jobs"+row["job_path"],description=desc,posted_at=row.get("posted_date"),employment_type="internship" if row.get("is_intern") else row.get("job_schedule_type") or "unknown")
    return list(jobs.values()),raw


def fetch_smartrecruiters(client, source):
    """Union explicit public searches, then fetch every authoritative job detail.

    Query scope is declared in config: this is not an all-company/all-web index.
    See https://developers.smartrecruiters.com/docs/endpoints and /docs/rate-limiting.
    Partial lists, changing totals and failed details invalidate the snapshot.
    """
    slug = source["slug"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        raise ValueError("Invalid SmartRecruiters company identifier")
    queries = source.get("queries")
    if not isinstance(queries, list) or not queries or any(not isinstance(q, str) or not q.strip() for q in queries):
        raise ValueError("SmartRecruiters requires explicit nonempty coverage queries")
    root = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    next_request = 0.0

    def get_json(url, **kwargs):
        nonlocal next_request
        # Single-threaded and at most eight requests/second, below the documented
        # default 10/s. A 429 aborts safely; the next scheduled scan can retry.
        delay = next_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        next_request = time.monotonic() + 0.125
        response = client.get(url, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("SmartRecruiters response is not an object")
        return payload

    rows = {}
    raw = {"searches": {}, "details": {}}
    for query in dict.fromkeys(queries):
        pages = []
        seen = set()
        offset = 0
        expected = None
        while offset < 20000:
            data = get_json(root, params={"q": query, "limit": 100, "offset": offset, "destination": "PUBLIC"})
            pages.append(data)
            total = data.get("totalFound")
            batch = data.get("content")
            if type(total) is not int or total < 0 or not isinstance(batch, list) or data.get("offset") != offset:
                raise ValueError("SmartRecruiters listing/pagination metadata is incomplete")
            if expected is None:
                expected = total
            elif total != expected:
                raise ValueError("SmartRecruiters result total changed during scan; retry later")
            for row in batch:
                jid = str(row.get("id", ""))
                if not jid.isdigit():
                    raise ValueError("Unexpected SmartRecruiters posting identifier")
                if jid in seen:
                    raise ValueError("Repeated SmartRecruiters page: incomplete snapshot")
                seen.add(jid)
                rows[jid] = row
            if len(seen) == expected:
                break
            if len(seen) > expected or not batch:
                raise ValueError("SmartRecruiters pagination ended early or exceeded total")
            offset += len(batch)
        if len(seen) != expected:
            raise ValueError("SmartRecruiters scan was capped/incomplete")
        raw["searches"][query] = pages

    jobs = []
    for jid in rows:
        detail = get_json(root + "/" + jid)
        raw["details"][jid] = detail
        if str(detail.get("id")) != jid or detail.get("active") is not True or detail.get("visibility") != "PUBLIC":
            raise ValueError("SmartRecruiters detail identity/status changed; retry snapshot")
        if detail.get("company", {}).get("identifier", "").casefold() != slug.casefold():
            raise ValueError("SmartRecruiters company identity mismatch")
        url = detail.get("postingUrl", "")
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if (not public_https(url) or parsed.hostname != "jobs.smartrecruiters.com"
                or len(parts) != 2 or parts[0].casefold() != slug.casefold()
                or not (parts[1] == jid or parts[1].startswith(jid + "-"))):
            raise ValueError("SmartRecruiters authoritative posting URL is missing or mismatched")
        sections = detail.get("jobAd", {}).get("sections", {})
        if not isinstance(sections, dict) or not text_content(sections.get("jobDescription", {}).get("text", "")):
            raise ValueError("SmartRecruiters full job description is missing")
        description = text_content(" ".join(section.get("text", "") for section in sections.values()))
        title = detail.get("name")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("SmartRecruiters job title is missing")
        location = detail.get("location") or {}
        loc = ", ".join(dict.fromkeys(str(location[k]) for k in ("city", "region", "country") if location.get(k))) or "unknown"
        if location.get("remote"):
            loc = "Remote / " + loc
        jobs.append(Job(key=f"{source['id']}:{jid}", source=source["id"],
                        source_id=jid, company=source["company"], company_kind=source["kind"],
                        title=title, location=loc, url=url, description=description,
                        posted_at=detail.get("releasedDate"),
                        employment_type=(detail.get("typeOfEmployment") or {}).get("label", "unknown")))
    return jobs, raw


def fetch_workday(client, source):
    """Read public Workday career search JSON and full posting details.

    Workday's career-site data format is not a versioned integration contract.
    Changes fail the scan, preserving the last good snapshot. Search terms and
    all returned matches are retained without a second local title filter.
    """
    host, tenant, site = source["host"], source["tenant"], source["site"]
    if not re.fullmatch(r"[a-z0-9-]+\.wd[0-9]+\.myworkdayjobs\.com", host):
        raise ValueError("Invalid Workday career hostname")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in (tenant, site)):
        raise ValueError("Invalid Workday tenant/site")
    queries = source.get("queries")
    if not isinstance(queries, list) or not queries or any(not isinstance(q, str) or not q.strip() for q in queries):
        raise ValueError("Workday requires explicit coverage queries")
    root = f"https://{host}/wday/cxs/{tenant}/{site}"
    rows = {}
    raw = {"searches": {}, "details": {}}
    for query in dict.fromkeys(queries):
        pages, seen = [], set()
        offset, expected = 0, None
        while offset < 20000:
            response = client.post(root + "/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": query})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Workday listing is not an object")
            total, batch = data.get("total"), data.get("jobPostings")
            if type(total) is not int or total < 0 or not isinstance(batch, list):
                raise ValueError("Workday listing metadata is incomplete")
            if expected is None:
                expected = total
            elif total not in (0, expected):
                # Workday returns total=0 on continuation pages; the count is
                # authoritative only on offset=0. Recheck that page below.
                raise ValueError("Workday result total changed during scan; retry later")
            pages.append(data)
            for row in batch:
                path = row.get("externalPath", "")
                if not path.startswith("/job/") or any(c in path for c in ("?", "#", "..", "\\")):
                    raise ValueError("Workday posting path is missing or unsafe")
                if path in seen:
                    raise ValueError("Repeated Workday page: incomplete snapshot")
                seen.add(path)
                rows[path] = row
            if len(seen) == expected:
                break
            if len(seen) > expected or not batch:
                raise ValueError("Workday pagination ended early or exceeded total")
            offset += len(batch)
        if len(seen) != expected:
            raise ValueError("Workday scan was capped/incomplete")
        check = client.post(root + "/jobs", json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": query})
        check.raise_for_status()
        current = check.json()
        first_paths = [row.get("externalPath") for row in pages[0]["jobPostings"]]
        if (not isinstance(current, dict) or current.get("total") != expected
                or not isinstance(current.get("jobPostings"), list)
                or [row.get("externalPath") for row in current["jobPostings"]] != first_paths):
            raise ValueError("Workday first page changed during pagination; retry later")
        raw["searches"][query] = pages

    jobs = {}
    for path in rows:
        response = client.get(root + path)
        response.raise_for_status()
        data = response.json()
        raw["details"][path] = data
        detail = data.get("jobPostingInfo")
        if not isinstance(detail, dict) or detail.get("canApply") is not True or detail.get("posted") is not True:
            raise ValueError("Workday public posting became unavailable; retry snapshot")
        jid = detail.get("jobReqId", "")
        url = detail.get("externalUrl", "")
        parsed = urlparse(url)
        if (not re.fullmatch(r"[A-Za-z0-9_-]+", jid) or not public_https(url)
                or parsed.hostname != host or parsed.path != f"/{site}{path}"
                or jid not in path):
            raise ValueError("Workday authoritative URL/identity mismatch")
        description = text_content(detail.get("jobDescription", ""))
        title = detail.get("title")
        if not description or not isinstance(title, str) or not title.strip():
            raise ValueError("Workday full title/description missing")
        locations = [detail.get("location")] + (detail.get("additionalLocations") or [])
        location = " / ".join(dict.fromkeys(x for x in locations if isinstance(x, str) and x)) or "unknown"
        job = Job(key=f"{source['id']}:{jid}", source=source["id"], source_id=jid,
                  company=source["company"], company_kind=source["kind"],
                  title=title, location=location, url=url, description=description,
                  posted_at=detail.get("startDate"), employment_type=detail.get("timeType") or "unknown")
        if jid in jobs and jobs[jid].content_hash() != job.content_hash():
            raise ValueError("Workday conflicting public postings for one requisition")
        jobs[jid] = job
    return list(jobs.values()), raw
