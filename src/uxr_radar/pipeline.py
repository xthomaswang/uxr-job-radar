from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from .core import (Assessment, Job, Overview, PROMPT_VERSION, digest, link_result, messages, now,
    validate_assessment, validate_overview, overview_assessment, publication_decision, anonymous_policy, experience_level, experience_metadata, explicit_zero_experience)
from .sources import fetch_feed
from .store import Store

HEADERS = {"User-Agent": "uxr-job-radar/0.1 (personal job research)", "Accept": "application/json,text/html"}


def assessment_key(job, profile, model):
    return digest([job.content_hash(), anonymous_policy(profile), model, PROMPT_VERSION])


def fetch_all(store, sources, raw_dir):
    Path(raw_dir).mkdir(parents=True, exist_ok=True)
    summary={"fetched":0,"cached":0,"failed":[],"jobs":0,"new":0,"changed":0}
    def one(s):
        with httpx.Client(headers=HEADERS, timeout=45, follow_redirects=True) as c:
            return fetch_feed(c, s)
    with ThreadPoolExecutor(max_workers=4) as pool:
        eligible=[]
        for source in sources:
            interval=source.get("min_fetch_interval_seconds",0)
            if not store.claim_source_attempt(source["id"],interval):
                print(json.dumps({"source":source["id"],"status":"cached","minimum_interval_seconds":interval}),flush=True)
                summary["cached"]+=1
                continue
            eligible.append(source)
        futures = {pool.submit(one,s):s for s in eligible}
        for f in as_completed(futures):
            s = futures[f]
            try:
                jobs, raw = f.result()
                Path(raw_dir, s["id"]+".json").write_text(json.dumps(raw,ensure_ascii=False))
                counts=store.snapshot(s["id"],jobs,membership_mode=s.get("membership_mode","snapshot"))
                summary["fetched"]+=1
                for field in ("jobs","new","changed"):summary[field]+=counts[field]
                print(json.dumps({"source":s["id"],"fetched":len(jobs),"new":counts["new"],"changed":counts["changed"]}),flush=True)
            except Exception as e:
                store.source_error(s["id"],e)
                summary["failed"].append(s["id"])
                print(json.dumps({"source":s["id"],"error":str(e)}),flush=True)
    return summary


def infer(client, base, model, job, profile, max_tokens=1100, *, stage="details", overview=None, feedback=None, attempt=0):
    start = time.perf_counter()
    r = client.post(base.rstrip("/")+"/chat/completions", json={
        "model":model, "messages":messages(job,profile,stage=stage,overview=overview,feedback=feedback),
        "temperature":min(0.4,0.1*attempt), "max_tokens":max_tokens,
        "chat_template_kwargs":{"enable_thinking":False}, "stream":False,
    })
    r.raise_for_status()
    data=r.json()
    choice=data["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("Truncated/incomplete model output")
    raw=choice["message"]["content"]
    return raw, time.perf_counter()-start, data.get("usage",{})


# Score cut-offs for the optional CLM relevance screen (screen.py), best band first.
# On 400 random queued postings about 6% scored >=0.5 and 22% >=0.2 (2026-10-06).
SCREEN_BANDS = (0.5, 0.2)


def priority(row, score=None):
    j=Job.model_validate_json(row["data"])
    # Ordering ONLY; no fetched title is discarded before model review.
    direct=bool(re.search(r"user (?:experience )?research|ux research|design research|usability|human factors|research operations|researcher.*figma|researcher.*rapid research|consumer insight|product insight|product research|market research|behavioral|social research|product analy|customer.*analy|program evaluat|design strateg|service strateg",j.title,re.I))
    research=bool(re.search(r"research|insight|customer experience|strateg|analy|evaluat|behavior|programme?",j.title,re.I))
    tier=0 if direct else 1 if research else 2
    if score is not None:
        # A screen score can promote a job above its title tier, never demote it.
        tier=min(tier,next((band for band,cut in enumerate(SCREEN_BANDS) if score>=cut),len(SCREEN_BANDS)))
    return (tier,j.company_kind!="large",-(score or 0.0),row["first_seen"],j.key)


def validation_feedback(error):
    # Pydantic/HTTP reprs may contain model output or request details: do not persist/export them.
    if isinstance(error,ValidationError):
        details=[]
        for item in error.errors(include_input=False)[:4]:
            detail=".".join(map(str,item["loc"]))+": "+item["type"]
            # Only copy this typed schema bound; ctx can contain arbitrary private data.
            maximum=(item.get("ctx") or {}).get("max_length")
            if item["type"]=="string_too_long" and type(maximum) is int and maximum>0:
                detail+=f" (must be at most {maximum} characters)"
            details.append(detail)
        return "JSON schema validation failed: "+"; ".join(details)
    if isinstance(error,httpx.HTTPStatusError):
        return "Model endpoint HTTP status "+str(error.response.status_code)
    if isinstance(error,httpx.HTTPError):
        return "Model endpoint transport failure: "+type(error).__name__
    if isinstance(error,ValueError):
        # Only our fixed validation messages or JSON decoder messages, never raw model text.
        known=("Evidence is not an exact excerpt", "Overview evidence is not an exact excerpt",
               "Experience number", "Senior experience", "Experience category", "Recommendation contradicts",
               "Truncated/incomplete", "Relevant and uncertain", "Experience evidence is preference-only",
               "Experience zero requires", "Experience preference number")
        if str(error).startswith(known):return str(error)
    return "Model response could not be validated: "+type(error).__name__


def pending_tasks(store, profile, model, *, retries_only=False, force=False, job_keys=None, scores=None):
    """Due work, ordered: retries interleaved with fresh jobs; scores (job key -> screen
    relevance) only reorder, every pending job stays in the list."""
    fresh=[];retry=[];timestamp=now();candidates=[];scores=scores or {}
    for row in store.rows():
        if row["missing"] or (job_keys is not None and row["key"] not in job_keys):continue
        job=Job.model_validate_json(row["data"]);key=assessment_key(job,profile,model)
        if row["assessment_key"]==key:continue
        candidates.append((row,key))
    tasks=store.enqueue_many([(row["key"],key) for row,key in candidates])
    for row,key in candidates:
        task=tasks[(row["key"],key)]
        is_retry=task["status"]=="retry" or bool(task["error"])
        if retries_only and not is_retry:continue
        if is_retry:
            if not force and task["next_retry_at"] and task["next_retry_at"]>timestamp:continue
            retry.append((row,key,task))
        else:fresh.append((row,key,task))
    retry.sort(key=lambda item:(item[2]["next_retry_at"] or "",priority(item[0],scores.get(item[0]["key"]))))
    fresh.sort(key=lambda item:priority(item[0],scores.get(item[0]["key"])))
    # Give eligible retries an immediate slot and 1/4 of a busy mixed queue, without starving new jobs.
    ordered=[];ri=fi=0
    while ri<len(retry) or fi<len(fresh):
        if ri<len(retry):ordered.append(retry[ri]);ri+=1
        for _ in range(3):
            if fi<len(fresh):ordered.append(fresh[fi]);fi+=1
    return ordered


class BackendUnavailable(RuntimeError):
    """The shared model host is down or dropped a request; pause the queue."""


def unreachable(error):
    """The request never reached a working model host, so it is not this job's failure."""
    if isinstance(error,(httpx.ConnectError,httpx.ConnectTimeout,httpx.PoolTimeout)):return True
    return isinstance(error,httpx.HTTPStatusError) and error.response.status_code in {502,503,504}


def schedule_retry(store, job, key, stage, feedback):
    task=store.task(job.key,key)
    rounds=task["failure_rounds"]+1
    delay=min(21600,30*(2**min(rounds-1,10)))
    due=(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat(timespec="seconds")
    with store.db:
        store.db.execute("UPDATE inference_queue SET status='retry',failure_rounds=?,next_retry_at=?,error=?,updated_at=? WHERE job_key=? AND input_key=?",
            (rounds,due,feedback,now(),job.key,key))
    print(json.dumps({"job":job.key,"stage":stage,"status":"retry","next_retry_at":due}),flush=True)


def process_task(store, client, profile, base, model, row, key, attempts_per_stage):
    job=Job.model_validate_json(row["data"])
    task=store.task(job.key,key)
    overview=Overview.model_validate_json(task["overview"]) if task["overview"] else None
    stage=task["stage"]
    feedback=task["error"]
    while stage in {"overview","details"}:
        for _ in range(attempts_per_stage):
            task=store.task(job.key,key)
            raw="";duration=0;usage={};error=None;result=None;dropped=False
            try:
                budget=min(2000,(450 if stage=="overview" else 1100)+200*task["stage_attempts"])
                raw,duration,usage=infer(client,base,model,job,profile,budget,stage=stage,
                    overview=overview,feedback=feedback,attempt=task["stage_attempts"])
                result=validate_overview(raw,job) if stage=="overview" else validate_assessment(raw,job)
            except Exception as exc:
                if unreachable(exc):raise BackendUnavailable(validation_feedback(exc)) from exc
                error=validation_feedback(exc)
                # The host died mid-request: charge this job once and cool it down, so a
                # request that crashes the shared server cannot be replayed in a loop.
                dropped=isinstance(exc,(httpx.RemoteProtocolError,httpx.ReadError,httpx.WriteError))
            with store.db:
                store.db.execute("INSERT INTO reviews(job_key,at,model,input_key,duration,usage,raw,error,stage) VALUES (?,?,?,?,?,?,?,?,?)",
                    (job.key,now(),model,key,duration,json.dumps(usage),raw,error,stage))
                store.db.execute("UPDATE inference_queue SET stage_attempts=stage_attempts+1,total_attempts=total_attempts+1,error=?,updated_at=? WHERE job_key=? AND input_key=?",
                    (error,now(),job.key,key))
            if result is not None:
                if stage=="overview":
                    overview=result
                    with store.db:
                        store.db.execute("UPDATE inference_queue SET overview=?,stage='details',stage_attempts=0,error=NULL,next_retry_at=NULL,status='queued' WHERE job_key=? AND input_key=?",
                            (overview.model_dump_json(),job.key,key))
                    if overview.role=="non_target":
                        result=overview_assessment(overview)
                    else:
                        stage="details";feedback=None
                        break
                with store.db:
                    saved=store.db.execute("UPDATE jobs SET assessment=?,assessment_key=?,assessed_at=?,error=NULL,attempts=0 WHERE key=? AND content_hash=?",
                        (result.model_dump_json(),key,now(),job.key,job.content_hash())).rowcount == 1
                    if saved:
                        store.db.execute("UPDATE inference_queue SET stage='complete',status='complete',error=NULL,next_retry_at=NULL,updated_at=? WHERE job_key=? AND input_key=?",
                            (now(),job.key,key))
                    else:
                        # A separate fetch may replace the posting while the model runs.
                        # Keep the old input restartable, without claiming a saved assessment.
                        store.db.execute("""UPDATE inference_queue SET stage='overview',overview=NULL,
                          status='queued',stage_attempts=0,failure_rounds=0,error=NULL,next_retry_at=NULL,
                          updated_at=? WHERE job_key=? AND input_key=?""", (now(),job.key,key))
                if not saved:
                    print(json.dumps({"job":job.key,"status":"superseded","reason":"posting_changed_during_inference"}),flush=True)
                    return False
                print(json.dumps({"job":job.key,"title":job.title,"stage":"complete","decision":publication_decision(result,profile),"seconds":round(duration,2)},ensure_ascii=False),flush=True)
                return True
            feedback=error
            with store.db:
                store.db.execute("UPDATE jobs SET error=?,attempts=attempts+1 WHERE key=? AND content_hash=?",(error,job.key,job.content_hash()))
            print(json.dumps({"job":job.key,"stage":stage,"attempt":task["stage_attempts"]+1,"error":error},ensure_ascii=False),flush=True)
            if dropped:
                schedule_retry(store,job,key,stage,feedback)
                raise BackendUnavailable(error)
        else:
            schedule_retry(store,job,key,stage,feedback)
            return False
    return False


def review_pending(store, profile, base, model, limit, *, retries_only=False, force=False, attempts_per_stage=3, job_keys=None):
    if not 1<=attempts_per_stage<=5:raise ValueError("attempts_per_stage must be between 1 and 5")
    count=0
    tasks=pending_tasks(store,profile,model,retries_only=retries_only,force=force,job_keys=job_keys)[:limit]
    with httpx.Client(timeout=240) as client:
        for row,key,task in tasks:
            count+=int(process_task(store,client,profile,base,model,row,key,attempts_per_stage))
    return count


def verify_links(store, *, min_age_seconds=0):
    """Check candidate links; rows checked within min_age_seconds keep their result."""
    summary={}
    recent=(datetime.now(timezone.utc)-timedelta(seconds=min_age_seconds)).isoformat(timespec="seconds")
    with httpx.Client(headers=HEADERS,timeout=30,follow_redirects=True) as c:
        for row in store.rows():
            if row["missing"] or not (row["assessment"] or row["error"]):continue
            if min_age_seconds and row["checked_at"] and row["checked_at"]>recent:continue
            a=Assessment.model_validate_json(row["assessment"]) if row["assessment"] else None
            if a and publication_decision(a)=="reject" and not row["error"]:continue
            j=Job.model_validate_json(row["data"])
            try:
                r=c.get(j.url)
                state=link_result(j,r.status_code,str(r.url),r.text)
            except httpx.HTTPError:
                state="unverified"
            with store.db:
                store.db.execute("UPDATE jobs SET link_state=?,checked_at=? WHERE key=?",(state,now(),j.key))
            summary[state]=summary.get(state,0)+1
            print(json.dumps({"job":j.key,"link":state}),flush=True)
    return summary


# Keeps the auto-published README renderable on GitHub; positions.jsonl stays complete.
README_ROWS_PER_SECTION = 100


def safe_md(value):
    return str(value).replace("|","／").replace("\n"," ").replace("<","&lt;").replace(">","&gt;").replace("[","［").replace("]","］").replace("*","\\*").replace("`","'")


def display_time(value):
    return datetime.fromisoformat(value).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M %Z") if value else "never"


def short_location(value):
    places=value.split(" / ")
    return " / ".join(places[:2])+f" (+{len(places)-2} locations)" if len(places)>2 else value


def handoff_assessment(a, title="", policy=None):
    """Public years come from separate mandatory/preferred fields, never narrative reasons."""
    result=a.model_dump()
    result.pop("reason")
    result.pop("experience")  # legacy extraction flag is not the public seniority label
    # Unconstrained prose may invent qualification claims even after JSON/quote checks.
    # Keep the raw assessment locally; expose only the structured labels and evidence.
    result["notes"]=[]
    result["uncertainties"]=[]
    result["narrative_status"]="omitted_unverified_model_narrative"
    result["extraction_warnings"]=[]
    result["preferred_years_range"]=None
    ranges=set()
    range_pattern=r"(?<![\w.,$€£])(?P<low>\d+(?:\.\d+)?)\s*(?:[-–—]|to)\s*(?P<high>\d+(?:\.\d+)?)\s*(?:years?|yrs?)(?!\w)(?!\s+old\b)"
    for item in a.evidence:
        if item.field!="preferred_experience":continue
        for match in re.finditer(range_pattern,item.quote,re.I):
            prefix=item.quote[max(0,match.start()-12):match.start()]
            if re.search(r"(?:[$€£]|\b(?:USD|CAD|EUR|GBP))\s*$",prefix,re.I):continue
            low,high=float(match["low"]),float(match["high"])
            if 0<=low<=high<=50:ranges.add((low,high))
    if ranges:
        # A quoted upper bound must not become a scalar preferred minimum.
        result["preferred_years"]=None
        if len(ranges)==1:
            low,high=next(iter(ranges))
            result["preferred_years_range"]={"min":low,"max":high}
        else:
            result["extraction_warnings"].append("Multiple distinct preferred experience ranges are quoted; inspect the evidence rather than a single collapsed threshold.")
    if a.required_years == 0:
        quotes=[e.quote for e in a.evidence if e.field=="experience"]
        if not any(explicit_zero_experience(q) for q in quotes):
            result["required_years"]=None
            result["extraction_warnings"].append("No explicit numeric minimum in the cited requirements; student eligibility is separate from an experience threshold.")
    result.update(experience_metadata(title,result["required_years"],policy))
    return result


def render(store,profile,model,path):
    rows=store.rows();sources=store.db.execute("SELECT * FROM sources ORDER BY id").fetchall()
    pending=sum(not r["missing"] and r["assessment_key"]!=assessment_key(Job.model_validate_json(r["data"]),profile,model) for r in rows)
    lines=["# UXR Job Radar", "", "High-recall UXR and related research opportunity pool, assessed on-device. Applications are handled separately.", "",
      f"Generated: {display_time(now())} · Model: `{model}` · Prompt: `{PROMPT_VERSION}` · Policy: `{digest(anonymous_policy(profile))}`", "",
      f"Tracked: {len(rows)} · Pending current model/policy review: **{pending}** · Sources with errors: **{sum(bool(s['error']) for s in sources)}** · Jobs with inference errors: **{sum(bool(r['error']) for r in rows if not r['missing'])}**", "",
      "**★ LARGE** is a curated company-priority label, not a model judgment. Startups and AI startups remain eligible.", "",
      "Experience levels use explicit mandatory years only: **Junior ≤3**, **Mid >3 and <5**, **Senior ≥5 and <8**, **Staff ≥8**, **Unknown** when unstated. Mid is an explicit intermediate bin added for the otherwise uncovered range. Preferred years and title seniority remain separate; discrepancies are labeled. Every relevant level stays in the recall pool.", "",
      "Every public posting returned by configured feeds enters the model queue. Title terms and, when installed, an on-device CLM-v0.1-8B relevance score affect processing order only; neither rejects a posting. Cached judgments are reused only for identical content, anonymous collection policy, model and prompt. A partial queue is not complete coverage.", "",
      "This is information retrieval, not final eligibility screening. Relevant roles stay visible even with unknown experience or qualification gaps. Free-text model reasons, notes and uncertainties are withheld. Source quotes are not a complete eligibility check; downstream reviewers must inspect the original posting. No applicant eligibility is inferred. Links come only from source feeds; the model cannot create or edit them. Verified means recent source membership and a matching reachable listing at check time. Aggregator records verify the provider listing, not the employer application page; JSONL distinguishes verification_scope and employer_verified.", ""]
    sections={"Priority opportunities — verified links":[],"More relevant opportunities — verified links":[],"Stretch opportunities — verified links":[],"Model validation needed — source facts only":[],"Link/source verification needed":[]}
    exported=[]
    rejected=0;reviewed=0;failed_attempts=0
    source_by_id={s["id"]:s for s in sources}
    for r in rows:
        j=Job.model_validate_json(r["data"])
        if r["missing"]:continue
        current_key=assessment_key(j,profile,model)
        a=None
        if r["assessment"] and r["assessment_key"]==current_key:
            reviewed+=1;a=Assessment.model_validate_json(r["assessment"])
            decision=publication_decision(a,profile)
            if decision=="reject":rejected+=1;continue
        elif r["error"]:
            failed=store.db.execute("SELECT 1 FROM reviews WHERE job_key=? AND input_key=? AND error IS NOT NULL LIMIT 1",(j.key,current_key)).fetchone()
            if not failed:continue
            decision="model_pending";failed_attempts+=1
        else:continue
        retry_task=store.task(j.key,current_key)
        source=source_by_id.get(j.source)
        fresh=bool(source and source["succeeded_at"] and not source["error"] and (datetime.now(timezone.utc)-datetime.fromisoformat(source["succeeded_at"])).total_seconds()<86400)
        listing_seen_recently=(datetime.now(timezone.utc)-datetime.fromisoformat(r["last_seen"])).total_seconds()<86400
        fresh=fresh and listing_seen_recently
        checked_fresh=bool(r["checked_at"] and (datetime.now(timezone.utc)-datetime.fromisoformat(r["checked_at"])).total_seconds()<86400)
        verified=r["link_state"]=="verified" and fresh and checked_fresh
        section=({"recommend":"Priority opportunities — verified links","review":"More relevant opportunities — verified links","stretch":"Stretch opportunities — verified links","model_pending":"Model validation needed — source facts only"}[decision]) if verified else "Link/source verification needed"
        handoff=handoff_assessment(a,j.title,profile) if a else None
        level_metadata=experience_metadata(j.title,handoff["required_years"] if handoff else None,profile)
        exported.append({**level_metadata,"required_years":handoff["required_years"] if handoff else None,"preferred_years":handoff["preferred_years"] if handoff else None,"preferred_years_range":handoff["preferred_years_range"] if handoff else None,"key":j.key,"company":j.company,"company_kind":j.company_kind,"title":j.title,"location":j.location,"url":j.url,"source":j.source,"source_id":j.source_id,"source_kind":j.source_kind,"source_label":j.source_label or j.company,"source_url":j.source_url,"source_listing_url":j.url,"application_url":j.application_url if j.source_kind=="aggregator" else j.url,"verification_scope":"aggregator_listing" if j.source_kind=="aggregator" else "employer_listing","employer_verified":verified and j.source_kind=="official","first_seen":r["first_seen"],"last_seen":r["last_seen"],"link_state":r["link_state"],"link_checked_at":r["checked_at"],"source_fresh":fresh,"link_fresh":checked_fresh,"verified":verified,"retrieval_category":decision,"assessment":handoff,"validation_error":"model_request_or_validation_failure" if a is None else None,"inference_stage":retry_task["stage"] if retry_task else "legacy","inference_attempts":retry_task["total_attempts"] if retry_task else None,"retry_at":retry_task["next_retry_at"] if retry_task else None,"model":model,"prompt_version":PROMPT_VERSION,"policy_hash":digest(anonymous_policy(profile))})
        sections[section].append((j,a,r))
    for name,items in sections.items():
        lines += [f"## {name}","","| Company | Position | Experience level | Type | Location | AI labels & source evidence | Link checked |","|---|---|---|---|---|---|---|"]
        ranked=sorted(items,key=lambda x:(x[0].company_kind!="large", {"junior":0,"unknown":1,"mid":2,"senior":3,"staff":4}[experience_level(x[1].required_years if x[1] else None,profile)], x[0].company,x[0].title))
        for j,a,r in ranked[:README_ROWS_PER_SECTION]:
            company=f"**★ LARGE · {safe_md(j.company)}**" if j.company_kind=="large" else f"{safe_md(j.company)} · {j.company_kind}"
            url=j.url.replace("(","%28").replace(")","%29")
            if j.source_kind=="aggregator":
                company+=f" · via [{safe_md(j.source_label or j.source)}]({url})"
            if a is None:
                lines.append(f"| {company} | [{safe_md(j.title)}]({url}) | Unknown — model pending | {safe_md(j.employment_type)} | {safe_md(short_location(j.location))} | Model validation pending; source facts only. Failed stage stays in the persistent retry queue. | {display_time(r['checked_at'])} ({r['link_state']}) |")
                continue
            evidence=" / ".join(safe_md(e.quote) for e in a.evidence)
            handoff=handoff_assessment(a,j.title,profile)
            threshold=handoff["required_years"]
            years="unknown" if threshold is None else f"{threshold:g}"
            preferred="unknown" if handoff["preferred_years"] is None else f"{handoff['preferred_years']:g}"
            if handoff["preferred_years_range"]:
                preferred=f"{handoff['preferred_years_range']['min']:g}–{handoff['preferred_years_range']['max']:g}"
            level=handoff["experience_level"].title()
            reason=f"AI role label: {a.role}; mandatory minimum years: {years}; preferred years: {preferred}; source title seniority: {handoff['title_seniority']}. "
            if handoff["seniority_note"]:reason+=handoff["seniority_note"]+" "
            if handoff["extraction_warnings"]:reason+=" ".join(handoff["extraction_warnings"])+" "
            if publication_decision(a,profile)=="stretch":reason+="Retained at its stated experience level. "
            lines.append(f"| {company} | [{safe_md(j.title)}]({url}) | {level} | {a.employment} | {safe_md(short_location(j.location))} | {safe_md(reason)} Evidence: {evidence}. Model notes to verify: withheld as unverified narrative. Source quotes are not a complete eligibility check. | {display_time(r['checked_at'])} ({r['link_state']}) |")
        if not items:lines += ["","No verified entries in this section yet."]
        if len(ranked)>README_ROWS_PER_SECTION:
            lines += ["",f"{len(ranked)-README_ROWS_PER_SECTION} more in this section are listed in [positions.jsonl](positions.jsonl); the table shows the first {README_ROWS_PER_SECTION} in this order."]
        lines += [""]
    lines += [f"Reviewed with current configuration: {reviewed}; clearly unrelated: {rejected}; failed attempts retained without fit claims: {failed_attempts}. All judgments and raw model outputs are retained locally in SQLite; relevant fit-gap roles remain above.","","## Source health","","| Source | Last successful fetch | Jobs | Error |","|---|---|---|---|"]
    lines += [f"| {s['id']} | {display_time(s['succeeded_at'])} | {s['count']} | {'source_fetch_failed' if s['error'] else 'none'} |" for s in sources]
    lines += ["","## Run locally","","See [setup and commands](docs/SETUP.md).","","The public collection policy contains no candidate dossier. Application decisions belong to downstream humans or agents."]
    output=Path(path)
    output.parent.mkdir(parents=True,exist_ok=True)
    lines += ["", "Machine-readable handoff: [positions.jsonl](positions.jsonl). Includes relevant reviewed roles and source-only records for failed model attempts, with explicit validation status, source IDs, links and verification times. Never-attempted jobs stay queued and are not mislabeled as reviewed."]
    output.write_text("\n".join(lines)+"\n")
    output.with_name("positions.jsonl").write_text("".join(json.dumps(item,ensure_ascii=False)+"\n" for item in exported))
