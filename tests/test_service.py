import json
import plistlib
from pathlib import Path

import httpx
import pytest

from uxr_radar.core import Job
from uxr_radar.launchd import AGENTS, agent_environment, agent_plist, label
from uxr_radar.pipeline import BackendUnavailable, assessment_key, review_pending
from uxr_radar.service import backend_check, run_collector, served_model, work_once
from uxr_radar.store import Store

ROOT=Path(__file__).parents[1]


@pytest.fixture
def job():
    return Job(key="acme:123",source="acme",source_id="123",company="Acme",company_kind="large",
        title="UX Researcher",location="Worldwide",url="https://jobs.example.com/123",
        description="Conduct user interviews. Requires 5 years of UX research.")


def overview():
    return json.dumps({"role":"uxr","summary":"Conduct user research interviews.","quote":"Conduct user interviews."})


def details():
    return json.dumps({"decision":"recommend","role":"uxr","required_years":5,"experience":"at_least_3",
        "employment":"unknown","reason":"Directly relevant research duties.","uncertainties":[],"notes":[],
        "evidence":[{"field":"experience","quote":"Requires 5 years of UX research."}]})


def setup(tmp_path,job):
    store=Store(tmp_path/"jobs.sqlite3");store.snapshot(job.source,[job]);return store


def reviews(store):
    return store.db.execute("SELECT count(*) FROM reviews").fetchone()[0]


def raising(error):
    def fake(*args,**kwargs):raise error
    return fake


@pytest.mark.parametrize("error",[httpx.ConnectError("connection refused"),httpx.ConnectTimeout("timeout"),
    httpx.HTTPStatusError("unavailable",request=httpx.Request("POST","http://127.0.0.1:8013/v1"),response=httpx.Response(503))])
def test_unreachable_model_host_pauses_without_charging_the_job(monkeypatch,tmp_path,job,error):
    store=setup(tmp_path,job)
    monkeypatch.setattr("uxr_radar.pipeline.infer",raising(error))
    with pytest.raises(BackendUnavailable):review_pending(store,{},"http://127.0.0.1:8013/v1","test",5)
    task=store.task(job.key,assessment_key(job,{},"test"))
    assert task["stage_attempts"]==0 and task["total_attempts"]==0 and task["status"]=="queued" and task["error"] is None
    assert reviews(store)==0 and store.rows()[0]["error"] is None


def test_dropped_request_is_charged_once_and_cooled_down(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job)
    monkeypatch.setattr("uxr_radar.pipeline.infer",raising(httpx.RemoteProtocolError("server disconnected")))
    with pytest.raises(BackendUnavailable):review_pending(store,{},"http://127.0.0.1:8013/v1","test",5)
    task=store.task(job.key,assessment_key(job,{},"test"))
    assert task["total_attempts"]==1 and task["status"]=="retry" and task["failure_rounds"]==1 and task["next_retry_at"]
    assert reviews(store)==1


@pytest.mark.parametrize("command,expected",[
    ("/venv/bin/python /venv/bin/mlx_lm.server --model mlx-community/Qwen3.8-27B-8bit --port 8013","mlx-community/Qwen3.8-27B-8bit"),
    ("python -m mlx_lm.server --model=./models/local --port 8013","./models/local"),
    ("python -m mlx_lm.server --port 8013",""),
    ("/usr/bin/python3 other_server.py --model x",None)])
def test_served_model_parses_mlx_command_lines(command,expected):
    assert served_model(command)==expected


def test_model_guard_refuses_a_shared_host_serving_another_model(monkeypatch):
    monkeypatch.setattr("uxr_radar.service.httpx.get",lambda url,timeout:httpx.Response(200,json={"status":"ok"}))
    monkeypatch.setattr("uxr_radar.service.listener_commands",lambda port:["python mlx_lm.server --model other/model --port 8013"])
    ok,reason=backend_check("http://127.0.0.1:8013/v1","mlx-community/Qwen3.8-27B-8bit")
    assert not ok and "swap" in reason
    monkeypatch.setattr("uxr_radar.service.listener_commands",lambda port:["python mlx_lm.server --model mlx-community/Qwen3.8-27B-8bit"])
    assert backend_check("http://127.0.0.1:8013/v1","mlx-community/Qwen3.8-27B-8bit")==(True,None)


def test_model_guard_reports_unreachable_host(monkeypatch):
    monkeypatch.setattr("uxr_radar.service.httpx.get",raising(httpx.ConnectError("refused")))
    ok,reason=backend_check("http://127.0.0.1:8013/v1","model")
    assert not ok and reason=="model host unreachable: ConnectError"


def test_worker_waits_for_model_without_charging_queued_jobs(tmp_path,job):
    store=setup(tmp_path,job)
    state,wait=work_once(store,{},tmp_path/"jobs.sqlite3","http://127.0.0.1:8013/v1","test",check=lambda base,model:(False,"down"))
    assert (state,wait)==("waiting_for_model",None)
    task=store.task(job.key,assessment_key(job,{},"test"))
    assert task["total_attempts"]==0 and task["error"] is None and reviews(store)==0
    assert store.statuses()["analyzer"]["state"]=="waiting_for_model"


def test_worker_processes_a_batch_then_goes_idle(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job)
    monkeypatch.setattr("uxr_radar.pipeline.infer",lambda *a,**k:((overview() if k["stage"]=="overview" else details()),0.1,{}))
    ok=lambda base,model:(True,None)
    assert work_once(store,{},tmp_path/"jobs.sqlite3","http://h/v1","test",check=ok)==("working",0)
    status=store.statuses()["analyzer"]
    assert status["last_batch"]["completed"]==1 and status["eligible"]==0
    state,wait=work_once(store,{},tmp_path/"jobs.sqlite3","http://h/v1","test",check=ok,idle_seconds=300)
    assert state=="idle" and wait==300


def test_worker_yields_when_a_manual_run_holds_the_queue(tmp_path,job):
    from uxr_radar.store import try_lock
    store=setup(tmp_path,job)
    with try_lock(str(tmp_path/"jobs.sqlite3")+".inference.lock") as held:
        assert held
        assert work_once(store,{},tmp_path/"jobs.sqlite3","http://h/v1","test",check=lambda b,m:(True,None))==("waiting_for_lock",60)


def test_offline_collector_claims_no_source_interval(tmp_path,monkeypatch):
    store=Store(tmp_path/"jobs.sqlite3")
    monkeypatch.setattr("uxr_radar.pipeline.fetch_feed",raising(AssertionError("no fetch while offline")))
    result=run_collector(store,[{"id":"public-api","min_fetch_interval_seconds":21600}],tmp_path/"raw",check=lambda:False)
    assert result=={"state":"offline"}
    assert store.db.execute("SELECT count(*) FROM sources").fetchone()[0]==0
    assert store.statuses()["collector"]["state"]=="offline"


def test_collector_reports_new_and_changed_jobs(tmp_path,monkeypatch,job):
    store=Store(tmp_path/"jobs.sqlite3")
    feed=[[job],[job.model_copy(update={"description":job.description+" New duties."}),job.model_copy(update={"key":"acme:9","source_id":"9"})]]
    monkeypatch.setattr("uxr_radar.pipeline.fetch_feed",lambda client,source:(feed.pop(0),{}))
    first=run_collector(store,[{"id":"acme"}],tmp_path/"raw",check=lambda:True)
    second=run_collector(store,[{"id":"acme"}],tmp_path/"raw",check=lambda:True)
    assert (first["new"],first["changed"])==(1,0) and (second["new"],second["changed"])==(1,1)
    assert store.statuses()["collector"]["last_run"]["jobs"]==2


class FakeScreener:
    """Stands in for the CLM screen: scores by a lookup, records calls."""
    version="fake-screen-v1"

    def __init__(self,values):
        self.values,self.calls,self.closed=values,[],0
        self.encoder=self

    def score(self,jobs):
        self.calls.append([j.key for j in jobs])
        return [self.values.get(j.title,0.0) for j in jobs]

    def close(self):
        self.closed+=1


def posting(key,title,description="Duties described here."):
    return Job(key=f"acme:{key}",source="acme",source_id=str(key),company="Acme",company_kind="established",
        title=title,location="Worldwide",url=f"https://jobs.example.com/{key}",description=description)


def test_screen_scores_reorder_but_never_drop_or_demote(tmp_path):
    from uxr_radar.pipeline import pending_tasks
    from uxr_radar.service import screen_pending
    store=Store(tmp_path/"jobs.sqlite3")
    jobs=[posting(1,"Account Executive"),posting(2,"Program Coordinator"),posting(3,"UX Researcher"),posting(4,"Strategic Finance")]
    store.snapshot("acme",jobs)
    screener=FakeScreener({"Program Coordinator":0.9,"UX Researcher":0.01,"Strategic Finance":0.2})
    assert screen_pending(store,screener,{},"test")==4 and screener.closed==1
    # Title-priority order decides which jobs are screened first.
    assert screener.calls[0][0]=="acme:3"
    scores=store.screen_scores(screener.version)
    ordered=[item[0]["key"] for item in pending_tasks(store,{},"test",scores=scores)]
    # The direct title keeps tier 0 despite a low score; the high score promotes job 2.
    assert ordered==["acme:2","acme:3","acme:4","acme:1"]
    assert sorted(ordered)==sorted(j.key for j in jobs)
    assert screen_pending(store,screener,{},"test")==0


def test_changed_or_assessed_postings_are_rescreened_only_when_needed(monkeypatch,tmp_path,job):
    from uxr_radar.service import screen_pending
    store=setup(tmp_path,job)
    screener=FakeScreener({job.title:0.7})
    assert screen_pending(store,screener,{},"test")==1
    changed=job.model_copy(update={"description":job.description+" New duties."})
    store.snapshot("acme",[changed])
    assert store.screen_scores(screener.version)=={}
    monkeypatch.setattr("uxr_radar.pipeline.infer",lambda *a,**k:((overview() if k["stage"]=="overview" else details()),0.1,{}))
    review_pending(store,{},"http://h/v1","test",1)
    assert screen_pending(store,screener,{},"test")==0


def test_worker_screens_before_checking_the_model_host(tmp_path,job):
    store=setup(tmp_path,job)
    screener=FakeScreener({job.title:0.4})
    state,_=work_once(store,{},tmp_path/"jobs.sqlite3","http://h/v1","test",check=lambda b,m:(False,"down"),screener=screener)
    assert state=="waiting_for_model" and screener.calls==[[job.key]]
    assert store.statuses()["analyzer"]["screen"]["scored_total"]==1


def test_launch_agents_use_explicit_environment_and_project_venv(tmp_path):
    env=agent_environment("http://127.0.0.1:8013/v1","mlx-community/Qwen3.8-27B-8bit",tmp_path/"hf")
    from uxr_radar.launchd import DEFAULT_ROLES
    plists={role:plistlib.loads(plistlib.dumps(agent_plist(role,tmp_path,env))) for role in DEFAULT_ROLES}
    for role,plist in plists.items():
        assert plist["Label"]==label(role) and plist["WorkingDirectory"]==str(tmp_path.resolve())
        assert plist["ProgramArguments"][0]==str(tmp_path.resolve()/".venv/bin/uxr-radar")
        assert plist["EnvironmentVariables"]["UXR_LLM_BASE_URL"].endswith(":8013/v1")
        assert plist["EnvironmentVariables"]["HF_HUB_OFFLINE"]=="1" and "/opt/homebrew/bin" in plist["EnvironmentVariables"]["PATH"]
        assert plist["StandardOutPath"].startswith(str(tmp_path.resolve()/"state/logs"))
    assert plists["analyzer"]["KeepAlive"] is True and "StartInterval" not in plists["analyzer"]
    assert plists["collector"]["StartInterval"]==3600 and plists["collector"]["RunAtLoad"] is True
    assert plists["publisher"]["StartInterval"]==3600 and plists["publisher"]["RunAtLoad"] is False


def test_client_pids_are_other_processes_connected_to_the_port():
    from uxr_radar.service import client_pids
    lsof="p100\nn127.0.0.1:8013->127.0.0.1:59620\np200\nn127.0.0.1:59620->127.0.0.1:8013\np300\nn127.0.0.1:59700->127.0.0.1:8013\n"
    # 100 is the server side; 300 is this process.
    assert client_pids(lsof,8013,own_pid=300)=={200}


class FakeServer:
    def __init__(self):self.events=[]
    def ensure(self,check):self.events.append("ensure");return check()
    def busy(self):self.events.append("busy")
    def idle(self):self.events.append("idle")
    def stop(self,reason="stopped"):self.events.append("stop:"+reason)


def test_worker_yields_to_other_clients_without_stopping_the_host(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job);server=FakeServer()
    monkeypatch.setattr("uxr_radar.pipeline.infer",raising(AssertionError("no request while yielding")))
    screener=FakeScreener({})
    state,wait=work_once(store,{},tmp_path/"jobs.sqlite3","http://h/v1","test",check=lambda b,m:(True,None),
        server=server,screener=screener,others=lambda:{4242},power=lambda:False)
    assert (state,wait)==("yielding",60) and server.events==[] and screener.calls==[]
    assert store.statuses()["analyzer"]["other_clients"]==[4242]


def test_worker_on_battery_stops_its_server_and_does_no_model_work(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job);server=FakeServer()
    monkeypatch.setattr("uxr_radar.pipeline.infer",raising(AssertionError("no request on battery")))
    state,wait=work_once(store,{},tmp_path/"jobs.sqlite3","http://h/v1","test",check=lambda b,m:(True,None),
        server=server,power=lambda:False)
    assert (state,wait)==("waiting_for_ac_power",300) and server.events==["stop:waiting_for_ac_power"]


def test_worker_starts_host_only_for_due_work_and_lets_other_clients_go_first(monkeypatch,tmp_path,job):
    store=setup(tmp_path,job);server=FakeServer()
    second=job.model_copy(update={"key":"acme:2","source_id":"2"})
    store.snapshot("acme",[job,second])
    monkeypatch.setattr("uxr_radar.pipeline.infer",lambda *a,**k:((overview() if k["stage"]=="overview" else details()),0.1,{}))
    arrivals=iter([set(),{777}])  # a game client connects after the first job
    state,wait=work_once(store,{},tmp_path/"jobs.sqlite3","http://h/v1","test",check=lambda b,m:(True,None),
        server=server,others=lambda:next(arrivals,{777}))
    assert state=="yielding" and server.events==["ensure","busy"]
    assert store.statuses()["analyzer"]["last_batch"]["attempted"]==1
    server.events.clear()
    state,_=work_once(store,{},tmp_path/"jobs.sqlite3","http://h/v1","test",check=lambda b,m:(True,None),server=server)
    assert state=="working"
    assert work_once(store,{},tmp_path/"jobs.sqlite3","http://h/v1","test",check=lambda b,m:(True,None),server=server)[0]=="idle"
    assert server.events[-1]=="idle" and "ensure" in server.events


class FakeProcess:
    pid=999999
    def __init__(self):self.alive=True
    def poll(self):return None if self.alive else 0


def test_managed_server_starts_on_demand_and_stops_after_idle(monkeypatch,tmp_path):
    from uxr_radar.service import ManagedServer
    started=[];clock=[0.0];health=iter([(False,"model host unreachable: ConnectError"),(False,"model host unreachable: ConnectError"),(True,None)])
    process=FakeProcess()
    def popen(argv,**kwargs):
        started.append((argv,kwargs["env"]["UXR_LLM_PORT"],kwargs["env"]["UXR_MODEL_PATH"]));return process
    killed=[]
    monkeypatch.setattr("uxr_radar.service.os.kill",lambda pid,sig:(killed.append(sig),setattr(process,"alive",False)))
    server=ManagedServer(tmp_path,"http://127.0.0.1:8013/v1","mlx-community/Qwen3.8-27B-8bit",idle_stop_seconds=600,
        popen=popen,clock=lambda:clock[0],sleep=lambda s:clock.__setitem__(0,clock[0]+s))
    assert server.ensure(lambda:next(health))==(True,None)
    assert started[0][0][-1].endswith("scripts/serve-local.sh") and started[0][1:]==("8013","mlx-community/Qwen3.8-27B-8bit")
    assert (tmp_path/"state/model-server.pid").read_text()=="999999"
    server.idle();clock[0]+=599;server.idle()
    assert process.alive
    clock[0]+=2;server.idle()
    assert not process.alive and not (tmp_path/"state/model-server.pid").exists()


def test_managed_server_never_stops_or_duplicates_a_foreign_host(monkeypatch,tmp_path):
    from uxr_radar.service import ManagedServer
    monkeypatch.setattr("uxr_radar.service.listening_pids",lambda port:{1234})
    (tmp_path/"state").mkdir();(tmp_path/"state/model-server.pid").write_text("5678")  # stale: not the listener
    popen=raising(AssertionError("must not start a second host"))
    server=ManagedServer(tmp_path,"http://127.0.0.1:8013/v1","m",popen=popen,sleep=lambda s:None)
    assert server.pid is None
    assert server.ensure(lambda:(True,None))==(True,None)
    assert server.ensure(lambda:(False,"shared mlx_lm host serves 'other', not 'm'"))[0] is False
    monkeypatch.setattr("uxr_radar.service.os.kill",raising(AssertionError("must not signal a foreign host")))
    server.idle();server.stop("idle")
