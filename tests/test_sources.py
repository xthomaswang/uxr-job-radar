"""Source contracts: never publish an incomplete snapshot as an empty board."""
import copy

import httpx
import pytest

from uxr_radar.sources import fetch_feed


SOURCE = {
    "id": "sample", "company": "Sample", "ats": "smartrecruiters",
    "slug": "Sample", "kind": "large", "queries": ["research", "insights"],
}


def detail(jid):
    return {
        "id": jid, "name": "UX Researcher" if jid == "1" else "Accountant",
        "company": {"identifier": "Sample"}, "active": True, "visibility": "PUBLIC",
        "postingUrl": f"https://jobs.smartrecruiters.com/Sample/{jid}-role",
        "location": {"city": "Paris", "country": "fr", "remote": True},
        "typeOfEmployment": {"label": "Full-time"},
        "jobAd": {"sections": {
            "companyDescription": {"text": "<p>A company</p>"},
            "jobDescription": {"text": "<p>Conduct user interviews.</p>"},
            "qualifications": {"text": "<p>Two years preferred.</p>"},
        }},
    }


def test_smartrecruiters_paginates_unions_queries_and_keeps_every_role():
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("/postings"):
            assert request.url.params["destination"] == "PUBLIC"
            query, offset = request.url.params["q"], int(request.url.params["offset"])
            total = 2 if query == "research" else 1
            jid = "1" if query == "research" and offset == 0 else "2"
            return httpx.Response(200, json={"offset": offset, "totalFound": total, "content": [{"id": jid}]})
        return httpx.Response(200, json=detail(request.url.path.rsplit("/", 1)[1]))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        jobs, raw = fetch_feed(client, SOURCE)
    assert [j.source_id for j in jobs] == ["1", "2"]
    assert jobs[1].title == "Accountant"  # No title/experience prefilter.
    assert jobs[0].url == detail("1")["postingUrl"]
    assert "Two years preferred." in jobs[0].description
    assert jobs[0].location == "Remote / Paris, fr"
    assert len(calls) == 5  # Three pages, then one detail per unique posting.
    assert len(raw["searches"]["research"]) == 2
    assert len(raw["details"]) == 2


@pytest.mark.parametrize("failure", ["early_end", "duplicate", "changed_total", "wrong_offset", "missing_total", "overflow", "http_blocked"])
def test_smartrecruiters_incomplete_or_blocked_snapshot_fails(failure):
    source = dict(SOURCE, queries=["research"])

    def respond(request):
        offset = int(request.url.params["offset"])
        data = {"offset": offset, "totalFound": 2, "content": [{"id": "1" if offset == 0 else "2"}]}
        if failure == "http_blocked":
            return httpx.Response(429)
        if offset:
            if failure == "early_end":
                data["content"] = []
            elif failure == "duplicate":
                data["content"] = [{"id": "1"}]
            elif failure == "changed_total":
                data["totalFound"] = 3
            elif failure == "wrong_offset":
                data["offset"] = 0
            elif failure == "missing_total":
                del data["totalFound"]
            elif failure == "overflow":
                data["content"] = [{"id": "2"}, {"id": "3"}]
        return httpx.Response(200, json=data)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            fetch_feed(client, source)


@pytest.mark.parametrize("change", [
    {"id": "99"}, {"active": False}, {"visibility": "INTERNAL"},
    {"company": {"identifier": "SomeoneElse"}},
    {"postingUrl": "https://jobs.smartrecruiters.com/Sample/99-role"},
    {"postingUrl": "https://example.com/Sample/1-role"},
    {"jobAd": {"sections": {"companyDescription": {"text": "Company only"}}}},
])
def test_smartrecruiters_requires_active_public_identity_and_complete_detail(change):
    source = dict(SOURCE, queries=["research"])

    def respond(request):
        if request.url.path.endswith("/postings"):
            return httpx.Response(200, json={"offset": 0, "totalFound": 1, "content": [{"id": "1"}]})
        payload = copy.deepcopy(detail("1"))
        payload.update(change)
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError):
            fetch_feed(client, source)


def test_smartrecruiters_explicit_empty_query_result_is_valid():
    def respond(request):
        return httpx.Response(200, json={"offset": 0, "totalFound": 0, "content": []})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        jobs, _ = fetch_feed(client, SOURCE)
    assert jobs == []


WORKDAY = {
    "id": "sample", "company": "Sample", "ats": "workday", "kind": "large",
    "host": "sample.wd5.myworkdayjobs.com", "tenant": "sample", "site": "External",
    "queries": ["research", "UX"],
}


def workday_detail(jid):
    path = f"/job/US/Role_{jid}"
    return {"jobPostingInfo": {
        "jobReqId": jid, "title": "UX Research Intern" if jid == "R1" else "Accountant",
        "canApply": True, "posted": True,
        "externalUrl": f"https://sample.wd5.myworkdayjobs.com/External{path}",
        "jobDescription": "<p>Conduct interviews.</p>", "timeType": "Full time",
        "location": "US", "additionalLocations": ["Canada"], "startDate": "2026-10-05",
    }}


@pytest.mark.parametrize("continuation_zero", [False, True])
def test_workday_paginates_deduplicates_queries_preserves_authoritative_urls(continuation_zero):
    import json
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "POST":
            data = json.loads(request.content)
            jid = "R1" if data["searchText"] == "research" and data["offset"] == 0 else "R2"
            return httpx.Response(200, json={"total": 0 if continuation_zero and data["offset"] else (2 if data["searchText"] == "research" else 1),
                "jobPostings": [{"externalPath": f"/job/US/Role_{jid}"}]})
        return httpx.Response(200, json=workday_detail(request.url.path.rsplit("_", 1)[1]))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        jobs, raw = fetch_feed(client, WORKDAY)
    assert len(calls) == 7  # Includes first-page stability recheck for each query.
    assert [j.source_id for j in jobs] == ["R1", "R2"]
    assert jobs[1].title == "Accountant"
    assert jobs[0].location == "US / Canada"
    assert jobs[0].url == workday_detail("R1")["jobPostingInfo"]["externalUrl"]
    assert len(raw["details"]) == 2


@pytest.mark.parametrize("failure", ["early_end", "duplicate", "changed_total", "missing_total", "unsafe_path"])
def test_workday_incomplete_snapshot_fails(failure):
    import json

    def respond(request):
        offset = json.loads(request.content)["offset"]
        payload = {"total": 2, "jobPostings": [{"externalPath": f"/job/US/Role_R{offset + 1}"}]}
        if offset:
            if failure == "early_end":
                payload["jobPostings"] = []
            elif failure == "duplicate":
                payload["jobPostings"] = [{"externalPath": "/job/US/Role_R1"}]
            elif failure == "changed_total":
                payload["total"] = 3
            elif failure == "missing_total":
                del payload["total"]
            elif failure == "unsafe_path":
                payload["jobPostings"] = [{"externalPath": "/job/../admin"}]
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError):
            fetch_feed(client, dict(WORKDAY, queries=["research"]))


@pytest.mark.parametrize("change", [
    {"canApply": False}, {"posted": False}, {"jobDescription": ""},
    {"jobReqId": "R99"}, {"externalUrl": "https://example.com/job/R1"},
])
def test_workday_detail_requires_live_identity_description_and_official_url(change):
    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, json={"total": 1, "jobPostings": [{"externalPath": "/job/US/Role_R1"}]})
        data = workday_detail("R1")
        data["jobPostingInfo"].update(change)
        return httpx.Response(200, json=data)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError):
            fetch_feed(client, dict(WORKDAY, queries=["research"]))


def test_recruitee_full_public_feed_keeps_requirements_and_source_link():
    from uxr_radar.sources import parse_feed
    source = {"id": "bunq", "company": "bunq", "ats": "recruitee", "slug": "bunq", "kind": "established"}
    payload = {"offers": [{"id": 123, "title": "Researcher", "careers_url": "https://careers.bunq.com/o/researcher",
        "description": "<p>Run usability studies.</p>", "requirements": "<p>One year preferred.</p>"}]}
    jobs = parse_feed(source, payload)
    assert jobs[0].source_id == "123"
    assert jobs[0].url == payload["offers"][0]["careers_url"]
    assert "One year preferred." in jobs[0].description
    with pytest.raises(ValueError):
        parse_feed(source, {"error": "authentication required"})


def test_workday_rechecks_authoritative_count_after_last_page():
    posts = 0

    def respond(request):
        nonlocal posts
        assert request.method == "POST"
        posts += 1
        return httpx.Response(200, json={"total": 1 if posts == 1 else 2,
            "jobPostings": [{"externalPath": "/job/US/Role_R1"}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="first page changed"):
            fetch_feed(client, dict(WORKDAY, queries=["research"]))
    assert posts == 2


def test_amazon_preserves_basic_vs_preferred_qualification_boundaries():
    source = {"id": "amazon", "company": "Amazon", "ats": "amazon", "kind": "large", "queries": ["UX research"]}
    row = {"id_icims": "123", "job_path": "/en/jobs/123/ux-researcher", "title": "UX Researcher",
        "description": "<p>Conduct user interviews.</p>",
        "basic_qualifications": "<ul><li>1+ years of research experience.</li><li>Bachelor's degree.</li></ul>",
        "preferred_qualifications": "<ul><li>Master's degree in psychology.</li><li>3+ years of research experience.</li></ul>"}

    def respond(request):
        return httpx.Response(200, json={"hits": 1, "jobs": [row]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        jobs, _ = fetch_feed(client, source)
    job = jobs[0]
    basic, preferred = job.description.split("PREFERRED QUALIFICATIONS:", 1)
    assert "BASIC QUALIFICATIONS:" in basic
    assert "1+ years of research experience." in basic
    assert "Master's degree" not in basic
    assert "Master's degree in psychology." in preferred
    assert "3+ years of research experience." in preferred
    assert job.key == "amazon:123"
    assert job.url == "https://www.amazon.jobs/en/jobs/123/ux-researcher"
    old_description = "Conduct user interviews. 1+ years of research experience. Bachelor's degree. Master's degree in psychology. 3+ years of research experience."
    assert job.content_hash() != job.model_copy(update={"description": old_description}).content_hash()


def test_amazon_empty_qualification_labels_do_not_hide_missing_description():
    source = {"id": "amazon", "company": "Amazon", "ats": "amazon", "kind": "large", "queries": ["UX research"]}
    row = {"id_icims": "123", "job_path": "/en/jobs/123/ux-researcher", "title": "UX Researcher",
        "description": "<br>", "basic_qualifications": "", "preferred_qualifications": None}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"hits": 1, "jobs": [row]}))) as client:
        with pytest.raises(ValueError, match="Amazon description missing"):
            fetch_feed(client, source)
