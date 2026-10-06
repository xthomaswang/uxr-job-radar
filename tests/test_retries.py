import json
from datetime import datetime, timedelta, timezone

import pytest

from uxr_radar.core import Job, PROMPT_VERSION, digest, messages, anonymous_policy
from uxr_radar.pipeline import assessment_key, pending_tasks, review_pending, render
from uxr_radar.store import Store


@pytest.fixture
def job():
    return Job(key="acme:123",source="acme",source_id="123",company="Acme",company_kind="large",
        title="UX Researcher",location="Worldwide",url="https://jobs.example.com/123",
        description="Conduct user interviews. Requires 5 years of UX research.")


def overview(role="uxr"):
    return json.dumps({"role":role,"summary":"Conduct user research interviews.","quote":"Conduct user interviews."})


def details():
    return json.dumps({"decision":"recommend","role":"uxr","required_years":5,"experience":"at_least_3",
        "employment":"unknown","reason":"Directly relevant research duties.","uncertainties":[],"notes":[],
        "evidence":[{"field":"experience","quote":"Requires 5 years of UX research."}]})


def setup(tmp_path,job):
    store=Store(tmp_path/"jobs.sqlite3");store.snapshot(job.source,[job]);return store


def test_feedback_retry_then_success_keeps_overview_and_senior_role(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job);calls=[]
    def fake(client,base,model,job,policy,max_tokens,**kwargs):
        calls.append(kwargs)
        if kwargs["stage"]=="overview":return overview(),0.1,{}
        return ("not json" if len(calls)<4 else details()),0.1,{}
    monkeypatch.setattr("uxr_radar.pipeline.infer",fake)
    assert review_pending(store,{},"http://localhost","test",1)==1
    assert [c["stage"] for c in calls]==["overview","details","details","details"]
    assert calls[1]["overview"].role=="uxr"
    assert calls[2]["feedback"].startswith("JSON schema")
    assert calls[3]["attempt"]==2
    task=store.task(job.key,assessment_key(job,{},"test"))
    assert task["status"]=="complete" and task["total_attempts"]==4
    out=tmp_path/"README.md";render(store,{},"test",out)
    exported=json.loads(out.with_name("positions.jsonl").read_text())
    assert exported["retrieval_category"]=="stretch"
    assert exported["policy_hash"]==digest({}) and exported["prompt_version"]==PROMPT_VERSION


def test_failed_stage_persists_and_forced_retry_resumes_only_details(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job);stages=[]
    def fake(client,base,model,job,policy,max_tokens,**kwargs):
        stages.append(kwargs["stage"])
        return (overview() if kwargs["stage"]=="overview" else "bad JSON"),0.1,{}
    monkeypatch.setattr("uxr_radar.pipeline.infer",fake)
    assert review_pending(store,{},"http://localhost","test",1,attempts_per_stage=2)==0
    key=assessment_key(job,{},"test");task=store.task(job.key,key)
    assert task["status"]=="retry" and task["stage"]=="details" and task["next_retry_at"]
    assert task["total_attempts"]==3 and task["failure_rounds"]==1
    assert pending_tasks(store,{},"test",retries_only=True)==[]
    assert len(pending_tasks(store,{},"test",retries_only=True,force=True))==1
    store.db.close();store=Store(tmp_path/"jobs.sqlite3")
    calls=[]
    def success(client,base,model,job,policy,max_tokens,**kwargs):
        calls.append(kwargs);return details(),0.1,{}
    monkeypatch.setattr("uxr_radar.pipeline.infer",success)
    assert review_pending(store,{},"http://localhost","test",1,retries_only=True,force=True)==1
    assert len(calls)==1 and calls[0]["stage"]=="details" and calls[0]["overview"].role=="uxr"
    assert store.task(job.key,key)["status"]=="complete"


def test_due_retry_is_not_starved_by_new_postings(tmp_path,job):
    store=setup(tmp_path,job)
    key=assessment_key(job,{},"test");store.enqueue(job.key,key)
    store.db.execute("UPDATE inference_queue SET status='retry',error='invalid JSON',next_retry_at='2000-01-01T00:00:00+00:00'");store.db.commit()
    jobs=[job]+[job.model_copy(update={"key":f"acme:{i}","source_id":str(i)}) for i in range(1000,1100)]
    store.snapshot("acme",jobs)
    ordered=pending_tasks(store,{},"test")
    assert len(ordered)==101 and ordered[0][0]["key"]==job.key


def test_repeated_failure_backoff_is_bounded_but_job_not_abandoned(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job)
    monkeypatch.setattr("uxr_radar.pipeline.infer",lambda *args,**kwargs:("bad JSON",0.1,{}))
    for _ in range(3):
        review_pending(store,{},"http://localhost","test",1,force=True,attempts_per_stage=1)
    task=store.task(job.key,assessment_key(job,{},"test"))
    delay=(datetime.fromisoformat(task["next_retry_at"])-datetime.now(timezone.utc)).total_seconds()
    assert 110<delay<=120 and task["failure_rounds"]==3
    assert task["status"]=="retry" and task["total_attempts"]==3
    assert pending_tasks(store,{},"test",retries_only=True,force=True)


def test_changed_policy_or_content_never_reuses_stage_context(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job)
    monkeypatch.setattr("uxr_radar.pipeline.infer",lambda *args,**kwargs:((overview() if kwargs["stage"]=="overview" else "bad"),0.1,{}))
    review_pending(store,{},"http://localhost","test",1,attempts_per_stage=1)
    original=assessment_key(job,{},"test")
    assert store.task(job.key,original)["overview"]
    tasks=pending_tasks(store,{"targets":["human factors"]},"test")
    assert tasks[0][2]["overview"] is None and tasks[0][2]["stage"]=="overview"
    changed=job.model_copy(update={"description":job.description+" New responsibilities."})
    store.snapshot("acme",[changed]);tasks=pending_tasks(store,{},"test")
    assert tasks[0][2]["overview"] is None and tasks[0][1]!=original


def test_every_irrelevant_job_still_gets_local_overview(monkeypatch,tmp_path,job):
    job=job.model_copy(update={"title":"Account Executive","description":"Sell software to customers."})
    store=setup(tmp_path,job);stages=[]
    def fake(*args,**kwargs):
        stages.append(kwargs["stage"])
        return json.dumps({"role":"non_target","summary":"Sells software to customers.","quote":"Sell software to customers."}),0.1,{}
    monkeypatch.setattr("uxr_radar.pipeline.infer",fake)
    assert review_pending(store,{},"http://localhost","test",1)==1
    assert stages==["overview"]
    assert json.loads(store.rows()[0]["assessment"])["role"]=="non_target"


def test_job_selector_preserves_other_work(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job);other=job.model_copy(update={"key":"acme:other"})
    store.snapshot("acme",[job,other])
    selected=pending_tasks(store,{},"test",job_keys={other.key})
    assert [item[0]["key"] for item in selected]==[other.key]
    assert store.task(job.key,assessment_key(job,{},"test")) is None


def test_prompt_has_only_anonymous_policy_and_overview_is_not_evidence(job):
    from uxr_radar.core import Overview
    policy={"name":"Private Person","education":["Private School"],"citizenship":"private",
        "sources":["https://private.example.com"],"targets":["UX research"]}
    all_messages=json.dumps(messages(job,policy,stage="details",overview=Overview.model_validate_json(overview())))
    for forbidden in ["Private Person","Private School","https://private.example.com"]:
        assert forbidden not in all_messages
    assert "PROVISIONAL OVERVIEW" in all_messages and "NOT source evidence" in all_messages
    assert anonymous_policy(policy)=={"targets":["UX research"]}


def test_failure_export_uses_category_not_exception_or_raw_output(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job)
    secret_raw='{"role":"uxr","summary":"Private Person at Private School","quote":"invented source quotation"}'
    monkeypatch.setattr("uxr_radar.pipeline.infer",lambda *args,**kwargs:(secret_raw,0.1,{}))
    review_pending(store,{},"http://localhost","test",1,attempts_per_stage=1)
    out=tmp_path/"README.md";render(store,{},"test",out)
    exported=out.with_name("positions.jsonl").read_text()
    assert "Private Person" not in exported+out.read_text()
    assert json.loads(exported)["validation_error"]=="model_request_or_validation_failure"
    assert json.loads(exported)["retry_at"]


def test_invalid_overview_quote_is_not_passed_to_detail_stage(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job);stages=[]
    def fake(*args,**kwargs):
        stages.append(kwargs["stage"])
        return json.dumps({"role":"uxr","summary":"User research work.","quote":"This sentence was invented."}),0.1,{}
    monkeypatch.setattr("uxr_radar.pipeline.infer",fake)
    review_pending(store,{},"http://localhost","test",1,attempts_per_stage=2)
    assert stages==["overview","overview"]
    assert store.task(job.key,assessment_key(job,{},"test"))["overview"] is None


@pytest.mark.parametrize("change", ["policy", "model", "content"])
def test_completed_input_can_be_revisited_after_other_input(monkeypatch,tmp_path,job,change):
    store=setup(tmp_path,job)
    calls=[]
    def fake(*args,**kwargs):
        calls.append(kwargs["stage"])
        return (overview() if kwargs["stage"]=="overview" else details()),0.1,{}
    monkeypatch.setattr("uxr_radar.pipeline.infer",fake)
    policy_a={"targets":["UX research"]};model_a="model-a"
    assert review_pending(store,policy_a,"http://localhost",model_a,1)==1
    key_a=assessment_key(job,policy_a,model_a)
    policy_b={"targets":["human factors"]} if change=="policy" else policy_a
    model_b="model-b" if change=="model" else model_a
    if change=="content":
        store.snapshot("acme",[job.model_copy(update={"description":job.description+" New duties."})])
    assert review_pending(store,policy_b,"http://localhost",model_b,1)==1
    if change=="content":store.snapshot("acme",[job])
    tasks=pending_tasks(store,policy_a,model_a)
    assert len(tasks)==1 and tasks[0][2]["stage"]=="overview" and tasks[0][2]["status"]=="queued"
    assert tasks[0][2]["overview"] is None
    assert review_pending(store,policy_a,"http://localhost",model_a,1)==1
    assert store.rows()[0]["assessment_key"]==key_a
    assert store.task(job.key,key_a)["stage"]=="complete" and len(calls)==6


def test_changed_posting_during_inference_is_not_marked_complete(monkeypatch,tmp_path,job,capsys):
    store=setup(tmp_path,job)
    changed=job.model_copy(update={"description":job.description+" A changed posting."})
    def fake(*args,**kwargs):
        if kwargs["stage"]=="details":store.snapshot("acme",[changed])
        return (overview() if kwargs["stage"]=="overview" else details()),0.1,{}
    monkeypatch.setattr("uxr_radar.pipeline.infer",fake)
    key=assessment_key(job,{},"test")
    assert review_pending(store,{},"http://localhost","test",1)==0
    assert store.rows()[0]["assessment"] is None
    task=store.task(job.key,key)
    assert task["stage"]=="overview" and task["status"]=="queued" and task["overview"] is None
    assert '"status": "superseded"' in capsys.readouterr().out
    store.snapshot("acme",[job])
    monkeypatch.setattr("uxr_radar.pipeline.infer",lambda *args,**kwargs:((overview() if kwargs["stage"]=="overview" else details()),0.1,{}))
    assert review_pending(store,{},"http://localhost","test",1)==1
    assert store.rows()[0]["assessment_key"]==key


def test_restart_after_overview_committed_resumes_details(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job);stages=[]
    def interrupted(*args,**kwargs):
        stages.append(kwargs["stage"])
        if kwargs["stage"]=="details":raise KeyboardInterrupt
        return overview(),0.1,{}
    monkeypatch.setattr("uxr_radar.pipeline.infer",interrupted)
    with pytest.raises(KeyboardInterrupt):review_pending(store,{},"http://localhost","test",1)
    key=assessment_key(job,{},"test")
    assert store.task(job.key,key)["stage"]=="details"
    store.db.close();store=Store(tmp_path/"jobs.sqlite3")
    def resumed(*args,**kwargs):
        stages.append(kwargs["stage"])
        assert kwargs["overview"] is not None
        return details(),0.1,{}
    monkeypatch.setattr("uxr_radar.pipeline.infer",resumed)
    assert review_pending(store,{},"http://localhost","test",1)==1
    assert stages==["overview","details","details"]


def test_source_error_publication_is_category_only(tmp_path,job):
    store=setup(tmp_path,job)
    marker="Fictional private transport value"
    store.source_error("acme",marker)
    output=tmp_path/"README.md"
    render(store,{},"test",output)
    assert marker not in output.read_text()
    assert "source_fetch_failed" in output.read_text()
    assert store.db.execute("SELECT error FROM sources").fetchone()["error"]==marker


def test_string_length_feedback_includes_only_safe_schema_limit():
    from pydantic import ValidationError
    from uxr_radar.pipeline import validation_feedback
    marker="Fictional private model input"
    error=ValidationError.from_exception_data("Overview",[{
        "type":"string_too_long","loc":("summary",),"input":marker,
        "ctx":{"max_length":220,"private_context":marker}}])
    feedback=validation_feedback(error)
    assert feedback=="JSON schema validation failed: summary: string_too_long (must be at most 220 characters)"
    assert marker not in feedback and "private_context" not in feedback


def test_string_length_feedback_rejects_non_numeric_context_limit():
    from pydantic import ValidationError
    from uxr_radar.pipeline import validation_feedback
    class UntrustedContextError(ValidationError):
        def errors(self,**kwargs):
            return [{"type":"string_too_long","loc":("summary",),
                "ctx":{"max_length":"Fictional private context"}}]
    feedback=validation_feedback(UntrustedContextError("Overview",[]))
    assert feedback=="JSON schema validation failed: summary: string_too_long"


def test_every_fixed_validator_message_reaches_the_retry_prompt(job):
    """Fixed validate_assessment messages carry no model text; generic feedback made retries repeat the error."""
    import inspect, re as regex
    from uxr_radar import core
    from uxr_radar.pipeline import validation_feedback
    messages_raised=regex.findall(r'raise ValueError\("([^"]+)"\)',inspect.getsource(core.validate_assessment)+inspect.getsource(core.validate_overview))
    assert messages_raised
    for message in messages_raised:
        assert validation_feedback(ValueError(message))==message
    assert validation_feedback(ValueError("Fictional model text"))=="Model response could not be validated: ValueError"
