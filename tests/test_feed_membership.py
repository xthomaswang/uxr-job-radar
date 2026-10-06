import json
from datetime import datetime,timezone,timedelta
from pathlib import Path

from uxr_radar.core import Job
from uxr_radar.store import Store
from uxr_radar.pipeline import fetch_all,render,assessment_key


def sample():
    return Job(key="test:123",source="test",source_id="123",company="Example",company_kind="unknown",title="UX Researcher",location="Remote",url="https://jobs.example.com/123",description="Conduct user interviews.",source_kind="aggregator",source_label="Example Feed",source_url="https://jobs.example.com")


def test_rolling_feed_absence_does_not_close_existing_job(tmp_path):
    store=Store(tmp_path/"jobs.db");job=sample()
    store.snapshot("test",[job],membership_mode="rolling")
    store.snapshot("test",[],membership_mode="rolling")
    assert store.rows()[0]["missing"]==0
    store.snapshot("test",[])
    assert store.rows()[0]["missing"]==1


def test_minimum_feed_interval_uses_cached_snapshot(tmp_path,monkeypatch):
    store=Store(tmp_path/"jobs.db");store.snapshot("test",[sample()])
    called=[]
    monkeypatch.setattr("uxr_radar.pipeline.fetch_feed",lambda client,source:called.append(source))
    fetch_all(store,[{"id":"test","min_fetch_interval_seconds":21600}],tmp_path/"raw")
    assert called==[] and store.rows()[0]["missing"]==0


def test_old_rolling_record_not_verified_by_fresh_provider_fetch(tmp_path):
    store=Store(tmp_path/"jobs.db");job=sample();store.snapshot("test",[job],membership_mode="rolling")
    old=(datetime.now(timezone.utc)-timedelta(days=3)).isoformat(timespec="seconds")
    assessment={"decision":"recommend","role":"uxr","required_years":None,"experience":"not_stated","employment":"full_time","reason":"Relevant research role.","uncertainties":[],"notes":[],"evidence":[{"field":"role","quote":"Conduct user interviews."}]}
    store.db.execute("UPDATE jobs SET last_seen=?,checked_at=?,link_state='verified',assessment=?,assessment_key=?",(old,datetime.now(timezone.utc).isoformat(),json.dumps(assessment),assessment_key(job,{},"model")));store.db.commit()
    store.snapshot("test",[],membership_mode="rolling")
    output=tmp_path/"README.md";render(store,{},"model",output)
    exported=json.loads(output.with_name("positions.jsonl").read_text())
    assert not exported["verified"] and not exported["source_fresh"]
    assert not exported["employer_verified"]
    assert exported["verification_scope"]=="aggregator_listing"
    assert "Example Feed" in output.read_text()
