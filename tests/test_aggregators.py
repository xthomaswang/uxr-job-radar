import json

import httpx
import pytest

from uxr_radar.aggregators import fetch_aggregator
from uxr_radar.core import link_result


def source(provider,**overrides):
    return {"id":provider,"ats":"aggregator","provider":provider,"request_delay_seconds":0,**overrides}


def remotive_row():
    return {"id":123,"title":"UX Researcher","company_name":"Example","url":"https://remotive.com/remote-jobs/design/ux-researcher-123","description":"<p>Conduct user interviews.</p>"}


def arbeitnow_row(slug):
    return {"slug":slug,"title":"Account Executive","company_name":"Example","url":"https://www.arbeitnow.com/jobs/"+slug,"description":"Sell software.","created_at":1790000000}


def jobicy_row(jid):
    return {"id":jid,"jobTitle":"UX Researcher","companyName":"Example","url":f"https://jobicy.com/jobs/{jid}-ux-researcher","jobDescription":"Conduct user interviews."}


def himalayas_row(slug):
    url="https://himalayas.app/companies/example/jobs/"+slug
    return {"guid":url,"applicationLink":url,"title":"UX Researcher","companyName":"Example","description":"Conduct user interviews."}


def fetch(provider,handler,**overrides):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return fetch_aggregator(client,source(provider,**overrides))


def test_remotive_provenance_keeps_provider_listing_and_unknown_company_kind():
    jobs,raw=fetch("remotive",lambda r:httpx.Response(200,json={"job-count":1,"total-job-count":1,"jobs":[remotive_row()]}))
    assert len(jobs)==1 and jobs[0].source_kind=="aggregator"
    assert jobs[0].source_label=="Remotive" and jobs[0].source_url=="https://remotive.com"
    assert jobs[0].application_url is None and jobs[0].url==remotive_row()["url"]
    assert jobs[0].company_kind=="unknown" and jobs[0].description=="Conduct user interviews."


def test_remotive_partial_feed_count_is_failure():
    with pytest.raises(ValueError,match="count mismatch"):
        fetch("remotive",lambda r:httpx.Response(200,json={"job-count":1,"total-job-count":2,"jobs":[remotive_row()]}))


def test_arbeitnow_follows_all_pages_and_keeps_non_research_jobs():
    requested=[]
    def handler(request):
        page=int(request.url.params.get("page","1"));requested.append(page)
        return httpx.Response(200,json={"data":[arbeitnow_row(str(page))],"meta":{"current_page":page},"links":{"next":"https://www.arbeitnow.com/api/job-board-api?page=2" if page==1 else None}})
    jobs,raw=fetch("arbeitnow",handler)
    assert requested==[1,2] and len(jobs)==2 and all(j.title=="Account Executive" for j in jobs)


def test_arbeitnow_does_not_follow_untrusted_next_url():
    requested=[]
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200,json={"data":[arbeitnow_row("one")],"meta":{"current_page":1},"links":{"next":"https://attacker.example.com/steal"}})
    with pytest.raises(ValueError,match="Untrusted"):
        fetch("arbeitnow",handler)
    assert len(requested)==1


def test_pagination_cap_never_commits_a_partial_result():
    def handler(request):
        return httpx.Response(200,json={"data":[arbeitnow_row("one")],"meta":{"current_page":1},"links":{"next":"https://www.arbeitnow.com/api/job-board-api?page=2"}})
    with pytest.raises(ValueError,match="cap reached"):
        fetch("arbeitnow",handler,max_pages=1)


def test_jobicy_cursor_is_preserved_and_window_is_documented():
    seen=[]
    def handler(request):
        cursor=request.url.params.get("cursor");seen.append(cursor)
        return httpx.Response(200,json={"success":True,"jobCount":1,"jobs":[jobicy_row(1 if cursor is None else 2)],"nextCursor":"opaque+/=" if cursor is None else None,"hasMore":cursor is None})
    jobs,raw=fetch("jobicy",handler,membership_mode="rolling")
    assert seen==[None,"opaque+/="] and len(jobs)==2
    assert raw["coverage"]["mode"]=="rolling" and "seven" in raw["coverage"]["scope"]


def test_jobicy_does_not_treat_broken_continuation_as_empty_feed():
    with pytest.raises(ValueError,match="ended early"):
        fetch("jobicy",lambda r:httpx.Response(200,json={"success":True,"jobCount":0,"jobs":[],"nextCursor":None,"hasMore":True}))


def test_remoteok_metadata_is_not_a_job_and_original_case_url_is_preserved():
    url="https://remoteOK.com/remote-jobs/ux-researcher-55"
    row={"id":"55","position":"UX Researcher","company":"Example","description":"Conduct user interviews.","url":url}
    jobs,raw=fetch("remoteok",lambda r:httpx.Response(200,json=[{"legal":"Credit Remote OK"},row]),membership_mode="rolling")
    assert len(jobs)==1 and jobs[0].url==url and jobs[0].source_label=="Remote OK"


def test_himalayas_walks_index_offsets_despite_sparse_materialized_pages():
    requested=[]
    def handler(request):
        page=int(request.url.params["page"]);requested.append(page)
        return httpx.Response(200,json={"totalCount":3,"limit":2,"offset":(page-1)*2,"jobs":[himalayas_row(str(page))]})
    jobs,raw=fetch("himalayas",handler,queries=["UX research"])
    assert requested==[1,2] and len(jobs)==2
    assert raw["coverage"]["queries"][0]=={"query":"UX research","indexed_hits":3,"returned_jobs":2,"pages":2}


def test_himalayas_search_overlap_is_deduplicated_without_rejecting_jobs():
    jobs,raw=fetch("himalayas",lambda r:httpx.Response(200,json={"totalCount":1,"limit":20,"offset":0,"jobs":[himalayas_row("same")]}),queries=["UX research","human factors"])
    assert len(jobs)==1 and len(raw["coverage"]["queries"])==2


def test_himalayas_ignored_page_is_error():
    with pytest.raises(ValueError,match="ignored requested page"):
        fetch("himalayas",lambda r:httpx.Response(200,json={"totalCount":30,"limit":20,"offset":0,"jobs":[himalayas_row("same")]}),queries=["UX research"])


def test_429_surfaces_and_does_not_produce_a_successful_empty_snapshot():
    with pytest.raises(httpx.HTTPStatusError):
        fetch("remotive",lambda r:httpx.Response(429,json={"error":"Rate limited"},headers={"Retry-After":"3600"}))


def test_aggregator_does_not_weaken_direct_employer_link_identity():
    jobs,_=fetch("remotive",lambda r:httpx.Response(200,json={"job-count":1,"jobs":[remotive_row()]}))
    assert link_result(jobs[0],200,"https://employer.example.com/jobs/different-id","UX Researcher")=="unverified"


def test_noncanonical_provider_destination_is_rejected():
    row=remotive_row();row["url"]="https://untrusted.example.com/job/123"
    with pytest.raises(ValueError,match="canonical"):
        fetch("remotive",lambda r:httpx.Response(200,json={"job-count":1,"jobs":[row]}))


def test_image_only_remoteok_description_stays_empty_not_invented_or_dropped():
    row={"id":"55","position":"UX Researcher","company":"Example","description":"<img src='https://example.com/job.png'>","url":"https://remoteok.com/remote-jobs/ux-researcher-55"}
    jobs,raw=fetch("remoteok",lambda r:httpx.Response(200,json=[{"legal":"Credit Remote OK"},row]))
    assert len(jobs)==1 and jobs[0].description=="" and raw["jobs_without_readable_description"]==1


def test_arbeitnow_preserves_provider_regional_urls_returned_by_feed():
    row=arbeitnow_row("one");row["url"]="https://www.arbeitnow.co.uk/jobs/one"
    jobs,raw=fetch("arbeitnow",lambda r:httpx.Response(200,json={"data":[row],"meta":{"current_page":1},"links":{"next":None}}))
    assert jobs[0].url==row["url"] and jobs[0].source_label=="Arbeitnow"


def test_arbeitnow_repeated_regional_listings_are_deduplicated_with_advancing_pages():
    def handler(request):
        page=int(request.url.params.get("page","1"))
        return httpx.Response(200,json={"data":[arbeitnow_row("regional"),arbeitnow_row(str(page))],"meta":{"current_page":page},"links":{"next":"https://www.arbeitnow.com/api/job-board-api?page=2" if page==1 else None}})
    jobs,raw=fetch("arbeitnow",handler)
    assert len(jobs)==3 and len(raw["pages"])==2


def test_himalayas_nonzero_index_with_no_materialized_jobs_is_not_empty_snapshot():
    with pytest.raises(ValueError,match="no materialized entries"):
        fetch("himalayas",lambda r:httpx.Response(200,json={"totalCount":10,"limit":20,"offset":0,"jobs":[]}),queries=["UX research"])
