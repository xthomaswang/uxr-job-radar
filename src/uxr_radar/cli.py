import argparse
import json
import os
import time
import fcntl
from contextlib import contextmanager
from pathlib import Path

from .pipeline import fetch_all, review_pending, verify_links, render
from .store import Store


@contextmanager
def inference_lock(state_path):
    """A process crash releases the lock; persisted stages remain restartable."""
    with open(str(state_path)+".inference.lock","w") as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit("Another process owns the inference queue for this database")
        yield


def main():
    p=argparse.ArgumentParser(description="Every fetched job is queued for staged on-device review; failed stages are retried persistently.")
    p.add_argument("command",choices=["fetch","review","retry","render","run","verify","watch"])
    p.add_argument("--sources",default="config/sources.json")
    p.add_argument("--policy","--profile",dest="policy",default="config/search_policy.json",help="Anonymous collection preferences; no applicant dossier")
    p.add_argument("--state",default="state/jobs.sqlite3")
    p.add_argument("--output",default="README.md")
    p.add_argument("--base-url",default=os.getenv("UXR_LLM_BASE_URL","http://127.0.0.1:8012/v1"))
    p.add_argument("--model",default=os.getenv("UXR_LLM_MODEL","mlx-community/Qwen3.8-27B-8bit"))
    p.add_argument("--limit",type=int,default=50,help="Maximum jobs attempted this invocation; a job may require multiple stage requests")
    p.add_argument("--attempts-per-stage",type=int,default=3,help="Immediate requests per stage (1-5), then persistent exponential cooldown")
    p.add_argument("--job-key",action="append",help="Only assess selected source:ID keys; repeatable, preserves other queued work")
    due=p.add_mutually_exclusive_group()
    due.add_argument("--due",action="store_true",help="Retry only eligible failures (default for retry)")
    due.add_argument("--force",action="store_true",help="Retry failures even before their cooldown expires")
    p.add_argument("--interval",type=int,default=3600,help="Minimum seconds between watch cycles")
    args=p.parse_args()
    if args.limit<0:p.error("--limit must be nonnegative")
    if not 1<=args.attempts_per_stage<=5:p.error("--attempts-per-stage must be between 1 and 5")
    if args.interval<60:p.error("--interval must be at least 60 seconds")
    if (args.force or args.due) and args.command!="retry":p.error("--due and --force are options for the retry command")
    policy=json.loads(Path(args.policy).read_text());store=Store(args.state)

    def review(policy):
        with inference_lock(args.state):
            return review_pending(store,policy,args.base_url,args.model,args.limit,
                retries_only=args.command=="retry",force=args.force,
                attempts_per_stage=args.attempts_per_stage,
                job_keys=set(args.job_key) if args.job_key else None)

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
                render(store,policy,args.model,args.output)
                time.sleep(max(1,args.interval-(time.monotonic()-start)))
        return
    if args.command in {"fetch","run"}:
        fetch_all(store,json.loads(Path(args.sources).read_text()),Path(args.state).parent/"raw")
    if args.command in {"review","retry","run"}:review(policy)
    if args.command in {"verify","run"}:
        verify_links(store)
    render(store,policy,args.model,args.output)


if __name__=="__main__":main()
