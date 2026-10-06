import argparse
import json
import os
import plistlib
import time
import fcntl
from contextlib import contextmanager
from pathlib import Path

from .pipeline import BackendUnavailable, fetch_all, load_exclusions, review_pending, verify_links, render
from .store import Store

DEFAULT_BASE_URL = "http://127.0.0.1:8013/v1"
DEFAULT_MODEL = "mlx-community/Qwen3.8-27B-8bit"


@contextmanager
def inference_lock(state_path, wait_seconds=600):
    """A process crash releases the lock; persisted stages remain restartable.
    The background analyzer holds it per batch, so a manual run waits for a gap."""
    with open(str(state_path)+".inference.lock","w") as lock:
        deadline=time.monotonic()+wait_seconds;announced=False
        while True:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise SystemExit("Another process owns the inference queue for this database")
                if not announced:print(json.dumps({"status":"waiting_for_inference_lock"}),flush=True);announced=True
                time.sleep(1)
        yield


def main():
    p=argparse.ArgumentParser(description="Every fetched job is queued for staged on-device review; failed stages are retried persistently.")
    p.add_argument("command",choices=["fetch","review","retry","render","run","verify","watch","collect","worker","screen","publish","status","agents","export-batch","import-batch"])
    p.add_argument("action",nargs="?",help="agents: install, uninstall or print; export-batch: output directory; import-batch: returned batch file")
    p.add_argument("--sources",default="config/sources.json")
    p.add_argument("--policy","--profile",dest="policy",default="config/search_policy.json",help="Anonymous collection preferences; no applicant dossier")
    p.add_argument("--state",default="state/jobs.sqlite3")
    p.add_argument("--output",default="README.md")
    p.add_argument("--base-url",default=os.getenv("UXR_LLM_BASE_URL",DEFAULT_BASE_URL))
    p.add_argument("--model",default=os.getenv("UXR_LLM_MODEL",DEFAULT_MODEL))
    p.add_argument("--limit",type=int,default=50,help="Maximum jobs attempted this invocation; a job may require multiple stage requests")
    p.add_argument("--attempts-per-stage",type=int,default=3,help="Immediate requests per stage (1-5), then persistent exponential cooldown")
    p.add_argument("--job-key",action="append",help="Only assess selected source:ID keys; repeatable, preserves other queued work")
    due=p.add_mutually_exclusive_group()
    due.add_argument("--due",action="store_true",help="Retry only eligible failures (default for retry)")
    due.add_argument("--force",action="store_true",help="Retry failures even before their cooldown expires")
    p.add_argument("--interval",type=int,default=3600,help="Minimum seconds between watch cycles")
    p.add_argument("--log-dir",help="collect/worker/publish: write a rotating log here instead of stdout")
    p.add_argument("--batch",type=int,default=10,help="worker: jobs attempted per queue scan")
    p.add_argument("--concurrency",type=int,default=1,help="review/retry: parallel jobs, for hosts that batch requests")
    p.add_argument("--idle-seconds",type=int,default=300,help="worker: wait when no job is due")
    p.add_argument("--once",action="store_true",help="worker: run one step and exit")
    p.add_argument("--no-keep-awake",dest="keep_awake",action="store_false",help="worker: do not hold the AC-power sleep assertion while working")
    p.add_argument("--no-screen",dest="screen",action="store_false",help="worker: skip the optional CLM ordering screen")
    p.add_argument("--repo",default=".",help="publish/status/agents: repository root")
    p.add_argument("--no-push",dest="push",action="store_false",help="publish: commit locally without pushing")
    p.add_argument("--dry-run",action="store_true",help="publish: render and audit the would-be commit, then stop")
    p.add_argument("--refresh-hours",type=float,default=6,help="publish: commit refreshed timestamps at least this often")
    p.add_argument("--verify-age-hours",type=float,default=6,help="publish: recheck links older than this")
    p.add_argument("--denylist",default="state/publication-denylist.json",help="publish: ignored local audit denylist, if present")
    p.add_argument("--exclude-employers",default="state/excluded-employers.json",help="publish/render: ignored local list of employers omitted from the public outputs, if present")
    p.add_argument("--no-probe",dest="probe",action="store_false",help="status: skip the model host check")
    p.add_argument("--no-manage-server",dest="manage_server",action="store_false",help="worker: never start/stop the local model host, only use one that answers")
    p.add_argument("--idle-stop-minutes",type=float,default=10,help="worker: stop the model host it started after this long without due jobs")
    p.add_argument("--allow-battery",dest="require_ac",action="store_false",help="worker: also run model work on battery power")
    p.add_argument("--no-yield",dest="yield_to_others",action="store_false",help="worker: keep working while other clients use the model host")
    args=p.parse_args()
    if args.limit<0:p.error("--limit must be nonnegative")
    if not 1<=args.attempts_per_stage<=5:p.error("--attempts-per-stage must be between 1 and 5")
    if args.interval<60:p.error("--interval must be at least 60 seconds")
    if args.batch<1:p.error("--batch must be positive")
    if (args.force or args.due) and args.command!="retry":p.error("--due and --force are options for the retry command")
    if (args.command in {"agents","export-batch","import-batch"})!=(args.action is not None):p.error("agents, export-batch and import-batch take one argument; other commands take none")
    if args.command=="agents" and args.action not in {"install","uninstall","print"}:p.error("agents takes install, uninstall or print")
    if args.concurrency<1:p.error("--concurrency must be positive")

    if args.command=="agents":
        from .launchd import agent_environment, agent_plist, install, uninstall, AGENTS
        environment=agent_environment(args.base_url,args.model,os.getenv("HF_HOME",str(Path.home()/"Developer/ml_source/huggingface")))
        if args.action=="print":
            for role in AGENTS:print(plistlib.dumps(agent_plist(role,args.repo,environment)).decode())
        elif args.action=="install":
            print(json.dumps({"installed":install(args.repo,environment)},indent=2))
        else:
            print(json.dumps({"removed":uninstall()},indent=2))
        return

    policy=json.loads(Path(args.policy).read_text());store=Store(args.state)

    if args.command=="collect":
        from .service import daemon_logging, run_collector
        with daemon_logging("collector",args.log_dir):
            run_collector(store,json.loads(Path(args.sources).read_text()),Path(args.state).parent/"raw")
        return
    if args.command=="worker":
        from .service import daemon_logging, run_worker, stop_on_sigterm
        stop_on_sigterm()
        with daemon_logging("analyzer",args.log_dir):
            run_worker(store,args.policy,args.state,args.base_url,args.model,batch=args.batch,
                attempts_per_stage=args.attempts_per_stage,idle_seconds=args.idle_seconds,
                keep_awake=args.keep_awake,once=args.once,screen=args.screen,manage_server=args.manage_server,
                idle_stop_seconds=args.idle_stop_minutes*60,require_ac=args.require_ac,
                yield_to_others=args.yield_to_others,repo=args.repo)
        return
    if args.command=="screen":
        # Precompute ordering scores (no model host needed); --limit 0 means the whole backlog.
        from .service import load_screener, screen_pending
        screener,reason=load_screener()
        if screener is None:raise SystemExit(reason)
        try:screen_pending(store,screener,policy,args.model,limit=args.limit or 10**9)
        finally:screener.close()
        return
    if args.command=="publish":
        from .publish import run_publisher
        from .service import daemon_logging
        with daemon_logging("publisher",args.log_dir):
            code=run_publisher(args.repo,store,policy,args.model,state_path=args.state,push=args.push,dry_run=args.dry_run,
                refresh_seconds=args.refresh_hours*3600,verify_age_seconds=args.verify_age_hours*3600,denylist=args.denylist,exclude_path=args.exclude_employers)
        raise SystemExit(code)
    if args.command=="export-batch":
        from .batch import export_batch
        print(json.dumps(export_batch(store,policy,args.model,args.action,repo=args.repo),indent=2))
        return
    if args.command=="import-batch":
        from .batch import import_batch
        print(json.dumps(import_batch(store,args.action,policy,args.model),indent=2))
        return
    if args.command=="status":
        from .service import status_report
        print(json.dumps(status_report(store,Path(args.repo).resolve(),policy,args.model,args.base_url,probe=args.probe),ensure_ascii=False,indent=2))
        return

    unavailable=[]
    def review(policy):
        with inference_lock(args.state):
            try:
                return review_pending(store,policy,args.base_url,args.model,args.limit,
                    retries_only=args.command=="retry",force=args.force,
                    attempts_per_stage=args.attempts_per_stage,
                    job_keys=set(args.job_key) if args.job_key else None,concurrency=args.concurrency)
            except BackendUnavailable as error:
                # Queue state is intact; remaining jobs were not charged with attempts.
                unavailable.append(str(error))
                print(json.dumps({"status":"model_unavailable","reason":str(error)}),flush=True)

    if args.command=="watch":
        with open(str(args.state)+".lock","w") as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:p.error("A watcher already owns this state database")
            while True:
                start=time.monotonic()
                policy=json.loads(Path(args.policy).read_text())
                fetch_all(store,json.loads(Path(args.sources).read_text()),Path(args.state).parent/"raw")
                review(policy)
                verify_links(store)
                render(store,policy,args.model,args.output,excluded=load_exclusions(args.exclude_employers))
                time.sleep(max(1,args.interval-(time.monotonic()-start)))
        return
    if args.command in {"fetch","run"}:
        fetch_all(store,json.loads(Path(args.sources).read_text()),Path(args.state).parent/"raw")
    if args.command in {"review","retry","run"}:review(policy)
    if args.command in {"verify","run"}:
        verify_links(store)
    render(store,policy,args.model,args.output,excluded=load_exclusions(args.exclude_employers))
    if unavailable:raise SystemExit(3)


if __name__=="__main__":main()
