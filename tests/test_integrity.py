import json

import pytest

from uxr_radar.core import Job, link_result, validate_assessment, publication_decision
from uxr_radar.pipeline import assessment_key, render
from uxr_radar.sources import parse_feed
from uxr_radar.store import Store


@pytest.fixture
def job():
    return Job(key="acme:123",source="acme",source_id="123",company="Acme",company_kind="large",title="UX Research Intern",location="US",url="https://jobs.example.com/123",description="Conduct user interviews. Current master's students welcome. No prior experience required.")


def result(**changes):
    r=dict(decision="recommend",role="uxr",required_years=0,experience="under_3",employment="internship",reason="Student internship with no prior experience required.",uncertainties=[],evidence=[{"field":"experience","quote":"No prior experience required."}])
    r.update(changes)
    return json.dumps(r)


def test_fabricated_evidence_cannot_publish(job):
    with pytest.raises(ValueError,match="exact excerpt"):
        validate_assessment(result(evidence=[{"field":"experience","quote":"Two years of experience required"}]),job)


def test_generated_application_url_not_allowed(job):
    with pytest.raises(ValueError):
        validate_assessment(result(apply_url="https://invented.example.com/apply"),job)


def test_missing_experience_is_retained_without_inventing_zero(job):
    assessment=validate_assessment(result(required_years=None,experience="not_stated"),job)
    assert assessment.required_years is None
    assert publication_decision(assessment)=="recommend"


def test_exactly_three_years_is_still_a_junior_opportunity(job):
    job=job.model_copy(update={"description":"Conduct user interviews. Requires 3 years of UX research."})
    assessment=validate_assessment(result(required_years=3,experience="at_least_3",evidence=[{"field":"experience","quote":"Requires 3 years of UX research."}]),job)
    assert publication_decision(assessment)=="recommend"


def test_multiple_years_is_not_a_numeric_seniority_threshold(job):
    with pytest.raises(ValueError,match="explicit numeric"):
        validate_assessment(result(decision="reject",required_years=None,experience="at_least_3"),job)


def test_preferred_experience_does_not_remove_relevant_opportunity(job):
    assessment=validate_assessment(result(required_years=None),job)
    assert assessment.decision=="recommend"  # preserve the raw model's judgment
    assert publication_decision(assessment)=="recommend"


def test_unknown_eligibility_is_retained_for_later_application_review(job):
    assessment=validate_assessment(result(uncertainties=["Mandatory overseas work authorization is unknown"]),job)
    assert publication_decision(assessment)=="recommend"


@pytest.mark.parametrize("status,body,final,expected",[
    (404,"","https://jobs.example.com/123","closed"),
    (403,"Access denied","https://jobs.example.com/123","unverified"),
    (200,"UX Research Intern — job is no longer available","https://jobs.example.com/123","closed"),
    (200,"Welcome to our careers page","https://jobs.example.com/jobs","unverified"),
    (200,"<h1>UX Research Intern</h1><button>Apply</button>","https://jobs.example.com/123","verified"),
])
def test_link_status(job,status,body,final,expected):
    assert link_result(job,status,final,body)==expected


def test_fetch_failure_preserves_membership_and_history(tmp_path,job):
    s=Store(tmp_path/"jobs.db");s.snapshot("acme",[job]);s.source_error("acme","429")
    assert s.rows()[0]["missing"]==0
    assert s.db.execute("SELECT error FROM sources").fetchone()[0]=="429"


def test_changed_description_invalidates_judgment(tmp_path,job):
    s=Store(tmp_path/"jobs.db");s.snapshot("acme",[job])
    s.db.execute("UPDATE jobs SET assessment=?,assessment_key='cached'",(result(),));s.db.commit()
    s.snapshot("acme",[job.model_copy(update={"description":"Requires 5 years of UXR experience."})])
    assert s.rows()[0]["assessment"] is None


def test_profile_model_and_job_changes_invalidate_cache(job):
    a=assessment_key(job,{"preferred_experience_max_exclusive":3},"model-a")
    assert a!=assessment_key(job,{"preferred_experience_max_exclusive":4},"model-a")
    assert a!=assessment_key(job,{"preferred_experience_max_exclusive":3},"model-b")


def test_non_research_postings_are_not_filtered():
    src={"id":"acme","ats":"greenhouse","slug":"acme","company":"Acme","kind":"startup"}
    data={"jobs":[{"id":1,"title":"Account Executive","absolute_url":"https://jobs.example.com/1","content":"Sell software to customers.","location":{"name":"US"}}]}
    assert parse_feed(src,data)[0].title=="Account Executive"


def test_incomplete_feed_fails_instead_of_becoming_empty():
    with pytest.raises(ValueError):parse_feed({"ats":"greenhouse"},{"error":"temporary failure"})


def test_unreviewed_jobs_never_become_recommendations(tmp_path,job):
    s=Store(tmp_path/"jobs.db");s.snapshot("acme",[job]);out=tmp_path/"README.md"
    render(s,{},"model",out)
    assert "Pending current model/policy review: **1**" in out.read_text()
    assert "[UX Research Intern]" not in out.read_text()


def test_flexible_timing_notes_do_not_block_recommendation(job):
    assessment=validate_assessment(result(notes=["Employer asks for return to school; user says scheduling is flexible."]),job)
    assert publication_decision(assessment)=="recommend"
    assert assessment.notes


def test_relevant_senior_job_stays_in_recall_pool(tmp_path,job):
    job=job.model_copy(update={"description":"Conduct user interviews. Requires 5 years of UX research."})
    raw=result(decision="reject",required_years=5,experience="at_least_3",evidence=[{"field":"experience","quote":"Requires 5 years of UX research."}])
    assessment=validate_assessment(raw,job)
    assert publication_decision(assessment)=="stretch"
    s=Store(tmp_path/"jobs.db");s.snapshot("acme",[job])
    s.db.execute("UPDATE jobs SET assessment=?,assessment_key=?,link_state='verified',checked_at=last_seen",(raw,assessment_key(job,{},"model")));s.db.commit()
    out=tmp_path/"README.md";render(s,{},"model",out)
    assert "[UX Research Intern]" in out.read_text()
    exported=json.loads(out.with_name("positions.jsonl").read_text())
    assert exported["retrieval_category"]=="stretch"
    assert exported["assessment"]["required_years"]==5


def test_amazon_overlapping_queries_are_deduplicated_and_complete():
    import httpx
    from uxr_radar.sources import fetch_amazon
    calls=[]
    row={"id_icims":"123","job_path":"/en/jobs/123/ux-researcher","title":"UX Researcher","description":"Conduct user interviews.","basic_qualifications":"1 year of research","preferred_qualifications":"","location":"US"}
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200,json={"hits":1,"jobs":[row],"error":None})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        jobs,raw=fetch_amazon(client,{"id":"amazon","company":"Amazon","kind":"large","queries":["UX research","user research"]})
    assert len(calls)==2 and len(jobs)==1
    assert jobs[0].url=="https://www.amazon.jobs/en/jobs/123/ux-researcher"
    assert "1 year of research" in jobs[0].description


def test_amazon_incomplete_pagination_is_not_a_complete_snapshot():
    import httpx
    from uxr_radar.sources import fetch_amazon
    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"hits":10,"jobs":[],"error":None}))) as client:
        with pytest.raises(ValueError,match="ended early"):
            fetch_amazon(client,{"id":"amazon","company":"Amazon","kind":"large","queries":["research"]})


def test_failed_model_attempt_retains_source_only_record(tmp_path,job):
    from uxr_radar.core import now
    s=Store(tmp_path/"jobs.db");s.snapshot("acme",[job]);key=assessment_key(job,{},"model")
    s.db.execute("UPDATE jobs SET error='quote mismatch',link_state='verified',checked_at=last_seen")
    s.db.execute("INSERT INTO reviews(job_key,at,model,input_key,error,raw) VALUES (?,?,?,?,?,?)",(job.key,now(),"model",key,"quote mismatch",'{"invented":"untrusted data"}'));s.db.commit()
    out=tmp_path/"README.md";render(s,{},"model",out)
    exported=json.loads(out.with_name("positions.jsonl").read_text())
    assert exported["retrieval_category"]=="model_pending"
    assert exported["assessment"] is None and exported["url"]==job.url
    assert "untrusted data" not in out.read_text()
    assert "Pending current model/policy review: **1**" in out.read_text()


def test_preferred_degree_in_reason_does_not_erase_mandatory_experience(job):
    from uxr_radar.pipeline import handoff_assessment
    from uxr_radar.core import Assessment
    assessment=Assessment.model_validate_json(result(required_years=5,experience="at_least_3",reason="Requires 5 years; a doctorate is preferred.",evidence=[{"field":"experience","quote":"Requires 5 years of UX research."}]))
    exported=handoff_assessment(assessment)
    assert exported["required_years"]==5 and exported["experience_level"]=="senior"
    assert "reason" not in exported


def test_student_eligibility_does_not_export_an_invented_zero_year_threshold(job):
    from uxr_radar.pipeline import handoff_assessment
    from uxr_radar.core import Assessment
    assessment=Assessment.model_validate_json(result(evidence=[{"field":"eligibility","quote":"Current master's students welcome."}]))
    exported=handoff_assessment(assessment)
    assert exported["required_years"] is None and exported["experience_level"]=="unknown"
    assert exported["decision"]=="recommend"
