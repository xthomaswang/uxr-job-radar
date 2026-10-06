"""No-key public job feeds. Preserve provider attribution and canonical listing URLs.

These are discovery feeds, not independent confirmations from employers. A rolling
feed's absence is never evidence that an older employer opening has closed.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from .core import Job, public_https, text_content

PROVIDERS = {
    "remotive": ("Remotive", "https://remotive.com", "https://remotive.com/api/remote-jobs"),
    "arbeitnow": ("Arbeitnow", "https://www.arbeitnow.com", "https://www.arbeitnow.com/api/job-board-api"),
    "remoteok": ("Remote OK", "https://remoteok.com", "https://remoteok.com/api"),
    "jobicy": ("Jobicy", "https://jobicy.com", "https://jobicy.com/api/v2/remote-jobs"),
    "himalayas": ("Himalayas", "https://himalayas.app", "https://himalayas.app/jobs/api/search"),
}


def timestamp(value):
    if value is None:return None
    if isinstance(value,(int,float)):
        return datetime.fromtimestamp(value,timezone.utc).isoformat(timespec="seconds")
    return str(value)


def _job(source,row):
    provider=source["provider"];name,home,_=PROVIDERS[provider]
    if provider=="remotive":
        jid=row["id"];title=row["title"];company=row["company_name"];url=row["url"]
        desc=row["description"];location=row.get("candidate_required_location") or "unknown"
        posted=row.get("publication_date");employment=row.get("job_type") or "unknown"
    elif provider=="arbeitnow":
        jid=row["slug"];title=row["title"];company=row["company_name"];url=row["url"]
        desc=row["description"];location=row.get("location") or "unknown"
        posted=row.get("created_at");employment=" / ".join(row.get("job_types") or []) or "unknown"
    elif provider=="remoteok":
        jid=row["id"];title=row["position"];company=row["company"];url=row["url"]
        desc=row["description"];location=row.get("location") or "Remote; restrictions unspecified"
        posted=row.get("date") or row.get("epoch");employment="unknown"
    elif provider=="jobicy":
        jid=row["id"];title=row["jobTitle"];company=row["companyName"];url=row["url"]
        desc=row["jobDescription"];location=row.get("jobGeo") or "unknown"
        posted=row.get("pubDate");employment=" / ".join(row.get("jobType") or []) or "unknown"
    else:
        jid=row["guid"];title=row["title"];company=row["companyName"];url=row["applicationLink"]
        desc=row["description"];location=" / ".join(row.get("locationRestrictions") or []) or "Worldwide"
        posted=row.get("pubDate");employment=row.get("employmentType") or "unknown"
    allowed_hosts={urlparse(home).hostname.removeprefix("www.")}
    if provider=="arbeitnow":allowed_hosts.update({"arbeitnow.co.uk","arbeitnow.fr","arbeitnow.ch"})
    actual_host=(urlparse(url).hostname or "").removeprefix("www.")
    desc=text_content(desc)
    if not str(jid) or not title.strip() or not company.strip():
        raise ValueError(f"Incomplete {provider} job; refuse a partial snapshot")
    if not public_https(url) or actual_host not in allowed_hosts:
        raise ValueError(f"Unexpected {provider} canonical listing URL")
    company_kind=source.get("company_kinds",{}).get(company.strip().casefold(),"unknown")
    return Job(key=f"{source['id']}:{jid}",source=source["id"],source_id=str(jid),
        company=company.strip(),company_kind=company_kind,title=title.strip(),location=location,
        url=url,description=desc,posted_at=timestamp(posted),employment_type=employment,
        source_kind="aggregator",source_label=name,source_url=home,application_url=None)


def _request(client,url,params=None):
    response=client.get(url,params=params)
    response.raise_for_status()  # Includes 429; caller preserves the prior snapshot and retries later.
    return response.json()


def _pause(source):
    time.sleep(max(0,float(source.get("request_delay_seconds",1))))


def _add(source,rows,jobs,*,allow_overlap=False):
    if not isinstance(rows,list):raise ValueError("Aggregator response lacks a job array")
    for row in rows:
        if not isinstance(row,dict):raise ValueError("Malformed aggregator job record")
        job=_job(source,row)
        if job.key in jobs:
            if allow_overlap:
                previous=jobs[job.key]
                if (previous.title,previous.company,previous.url)!=(job.title,job.company,job.url):
                    raise ValueError("Aggregator source identity changed across pages")
                jobs[job.key]=job
                continue
            raise ValueError("Duplicate aggregator job across pages; snapshot may be incomplete")
        jobs[job.key]=job


def fetch_aggregator(client,source):
    provider=source["provider"]
    if provider not in PROVIDERS:raise ValueError("Unsupported aggregator provider")
    _,_,endpoint=PROVIDERS[provider]
    jobs={};pages=[];coverage={"mode":source.get("membership_mode","snapshot")}
    max_pages=int(source.get("max_pages",200))
    if max_pages<1:raise ValueError("max_pages must be positive")
    if provider=="remotive":
        data=_request(client,endpoint)
        rows=data.get("jobs");_add(source,rows,jobs)
        if data.get("job-count")!=len(rows) or data.get("total-job-count",len(rows))!=len(rows):
            raise ValueError("Remotive count mismatch; incomplete feed")
        pages.append(data);coverage["scope"]="all active listings exposed by the public feed; 24-hour delay"
    elif provider=="remoteok":
        data=_request(client,endpoint)
        if not isinstance(data,list) or not data or not isinstance(data[0],dict) or "legal" not in data[0]:
            raise ValueError("Remote OK legal/feed envelope missing")
        _add(source,data[1:],jobs);pages.append(data)
        coverage["scope"]="latest public feed; no full-inventory or pagination guarantee"
    elif provider=="arbeitnow":
        next_url=endpoint;seen_urls=set()
        for page in range(1,max_pages+1):
            if next_url in seen_urls:raise ValueError("Repeated Arbeitnow pagination URL")
            parsed=urlparse(next_url);expected=urlparse(endpoint)
            if parsed.scheme!="https" or parsed.netloc!=expected.netloc or parsed.path!=expected.path:
                raise ValueError("Untrusted Arbeitnow next-page URL")
            params=parse_qs(parsed.query)
            if set(params)-{"page"} or (params and params.get("page")!=[str(page)]):
                raise ValueError("Unexpected Arbeitnow pagination sequence")
            seen_urls.add(next_url);data=_request(client,next_url)
            if data.get("meta",{}).get("current_page")!=page:
                raise ValueError("Arbeitnow page identity mismatch")
            rows=data.get("data");_add(source,rows,jobs,allow_overlap=True);pages.append(data)
            links=data.get("links")
            if not isinstance(links,dict) or "next" not in links:
                raise ValueError("Arbeitnow pagination metadata missing")
            next_url=links["next"]
            if not next_url:break
            if not rows:raise ValueError("Arbeitnow empty page before pagination completed")
            if not isinstance(next_url,str):raise ValueError("Malformed Arbeitnow pagination URL")
            _pause(source)
        else:raise ValueError("Arbeitnow pagination cap reached; incomplete snapshot")
        coverage["scope"]="all pages currently exposed by the public Europe/Germany feed"
    elif provider=="jobicy":
        cursor=None;seen_cursors=set()
        for _ in range(max_pages):
            params={"count":200}
            if cursor:params["cursor"]=cursor
            data=_request(client,endpoint,params)
            if data.get("success") is not True:raise ValueError("Jobicy did not report success")
            rows=data.get("jobs");_add(source,rows,jobs);pages.append(data)
            if data.get("jobCount")!=len(rows):raise ValueError("Jobicy page count mismatch")
            if "nextCursor" not in data or not isinstance(data.get("hasMore"),bool):
                raise ValueError("Jobicy cursor metadata missing")
            cursor=data["nextCursor"]
            if not data["hasMore"]:
                if cursor is not None:raise ValueError("Jobicy contradictory pagination metadata")
                break
            if not rows or not isinstance(cursor,str) or not cursor or cursor in seen_cursors:
                raise ValueError("Jobicy pagination ended early or repeated")
            seen_cursors.add(cursor);_pause(source)
        else:raise ValueError("Jobicy pagination cap reached; incomplete synchronization")
        coverage["scope"]="all cursor pages in the last seven publication days; absence does not mean closed"
    else:
        queries=source.get("queries")
        if not isinstance(queries,list) or not queries or not all(isinstance(q,str) and q.strip() for q in queries):
            raise ValueError("Himalayas requires explicit public search queries")
        summaries=[]
        for query_index,query in enumerate(queries):
            query_jobs={};total=None
            if query_index:_pause(source)
            for page in range(1,max_pages+1):
                data=_request(client,endpoint,{"q":query,"sort":"recent","page":page})
                page_total=data.get("totalCount");limit=data.get("limit");offset=data.get("offset")
                if not isinstance(page_total,int) or page_total<0 or not isinstance(limit,int) or not 1<=limit<=20:
                    raise ValueError("Himalayas count/page metadata missing")
                if offset!=(page-1)*limit:raise ValueError("Himalayas ignored requested page; incomplete search")
                if total is not None and page_total!=total:raise ValueError("Himalayas search count changed during pagination")
                total=page_total;rows=data.get("jobs")
                if not isinstance(rows,list) or len(rows)>limit:raise ValueError("Himalayas page size mismatch")
                # The public search index can return fewer materialized listings than its
                # advertised hit count. Traverse offsets, not len(jobs); never invent gap rows.
                _add(source,rows,query_jobs);pages.append({"query":query,"page":page,"response":data})
                if offset+limit>=total:break
                _pause(source)
            else:raise ValueError("Himalayas search page cap reached; incomplete search")
            if total and not query_jobs:
                raise ValueError("Himalayas returned no materialized entries for a nonzero search count")
            jobs.update(query_jobs)
            summaries.append({"query":query,"indexed_hits":total,"returned_jobs":len(query_jobs),"pages":page})
        coverage.update(scope="complete advertised search page ranges; index/materialized counts can differ",queries=summaries)
    return list(jobs.values()),{"provider":provider,"coverage":coverage,"returned_jobs":len(jobs),"jobs_without_readable_description":sum(not j.description for j in jobs.values()),"pages":pages}
