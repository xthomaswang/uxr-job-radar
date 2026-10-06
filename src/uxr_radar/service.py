"""Background roles: collector, analyzer worker and health reporting.

launchd starts these roles (see launchd.py). Each records its state in the
service_status table, so `uxr-radar status` shows whether the pipeline is
working without reading logs. Model-host outages pause the queue instead of
charging jobs with failed attempts.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .core import Job, now
from .pipeline import HEADERS, BackendUnavailable, assessment_key, fetch_all, pending_tasks, priority, process_task
from .store import try_lock

# Hosts that most configured sources depend on; one answer means the network is up.
PROBE_URLS = ("https://boards-api.greenhouse.io/", "https://api.ashbyhq.com/", "https://www.google.com/")


class _LogStream(io.TextIOBase):
    """Route the pipeline's JSON-line prints into a rotating log."""

    def __init__(self, logger):
        self.logger, self.pending, self.lock = logger, "", threading.Lock()

    def write(self, text):
        with self.lock:  # concurrent review threads print through this stream
            self.pending += text
            *lines, self.pending = self.pending.split("\n")
        for line in lines:
            if line.strip():self.logger.info(line)
        return len(text)


@contextlib.contextmanager
def daemon_logging(component, log_dir):
    """Rotating per-role log (5 x 5 MB). launchd's own output file then only sees crashes."""
    if not log_dir:
        yield
        return
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("uxr_radar." + component)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.handlers.RotatingFileHandler(Path(log_dir) / f"{component}.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    try:
        with contextlib.redirect_stdout(_LogStream(logger)):
            yield
    except Exception:
        logger.exception("%s failed", component)
        raise
    finally:
        logger.removeHandler(handler)
        handler.close()


def stop_on_sigterm():
    """launchd stops agents with SIGTERM; exit through finally blocks instead of dying mid-write."""
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))


class KeepAwake:
    """Hold an AC-power-only sleep assertion while queued work is being processed.

    `caffeinate -s` has no effect on battery, and `-w` ends it with this process.
    """

    def __init__(self, enabled=True):
        self.enabled = enabled and sys.platform == "darwin" and Path("/usr/bin/caffeinate").exists()
        self.process = None

    def hold(self):
        if self.enabled and (self.process is None or self.process.poll() is not None):
            self.process = subprocess.Popen(["/usr/bin/caffeinate", "-s", "-w", str(os.getpid())])

    def release(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(5)
        self.process = None


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def on_ac_power():
    """False only when macOS reports battery power; unknown counts as AC."""
    try:
        output = subprocess.run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return True
    return "'Battery Power'" not in output


def client_pids(lsof_fields, port, own_pid):
    """PIDs holding client-side connections to `port` in `lsof -Fpn` output, except own_pid."""
    pids, pid = set(), None
    for line in lsof_fields.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and "->" in line and line.endswith(f":{port}") and pid != own_pid:
            pids.add(pid)
    return pids


def other_clients(base_url):
    """Other local processes connected to a loopback model host (for example a game client).
    The analyzer yields to them: mlx_lm.server has no request priorities."""
    url = urlparse(base_url)
    if url.hostname not in LOOPBACK or not url.port:return set()
    try:
        output = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{url.port}", "-sTCP:ESTABLISHED", "-Fpn"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return client_pids(output, url.port, os.getpid())


def listening_pids(port):
    try:
        return {int(p) for p in subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=10).stdout.split()}
    except (OSError, subprocess.TimeoutExpired):
        return set()


class ManagedServer:
    """Start this project's mlx_lm server only while jobs are due; stop it after an idle period.

    A host this analyzer did not start (another project's, or one started by hand) is
    used but never stopped. A PID file lets a restarted analyzer adopt the server its
    predecessor started instead of leaving ~32 GB resident.
    """

    def __init__(self, repo, base_url, model, *, idle_stop_seconds=600, startup_seconds=600,
                 popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep):
        self.repo, self.model = Path(repo).resolve(), model
        self.port = urlparse(base_url).port or 8013
        self.pid_file = self.repo / "state" / "model-server.pid"
        self.log_path = self.repo / "state" / "logs" / "model-server.log"
        self.idle_stop_seconds, self.startup_seconds = idle_stop_seconds, startup_seconds
        self.popen, self.clock, self.sleep = popen, clock, sleep
        self.process = self.pid = self.idle_since = None
        try:
            pid = int(self.pid_file.read_text())
            if pid in listening_pids(self.port):
                self.pid = pid
        except (OSError, ValueError):
            pass

    def owned(self):
        if self.process is not None:
            return self.process.poll() is None
        if self.pid is None:return False
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def ensure(self, check):
        """(ok, reason): use any healthy host; start ours only when nothing answers."""
        ok, reason = check()
        if ok or not (reason or "").startswith("model host unreachable"):
            return ok, reason
        if not self.owned():
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "ab") as log:
                self.process = self.popen(["/bin/zsh", str(self.repo / "scripts" / "serve-local.sh")], stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True,
                    env={**os.environ, "UXR_LLM_PORT": str(self.port), "UXR_MODEL_PATH": self.model})
            self.pid = self.process.pid
            self.pid_file.write_text(str(self.pid))
            print(json.dumps({"model_server": "started", "pid": self.pid}), flush=True)
        deadline = self.clock() + self.startup_seconds
        while self.clock() < deadline:
            self.sleep(5)
            if not self.owned():
                return False, "model server exited during startup; see state/logs/model-server.log"
            ok, reason = check()
            if ok:
                return True, None
        return False, "model server did not become healthy in time"

    def busy(self):
        self.idle_since = None

    def idle(self):
        """Stop our server once no job has been due for idle_stop_seconds."""
        if not self.owned():return
        if self.idle_since is None:
            self.idle_since = self.clock()
        elif self.clock() - self.idle_since >= self.idle_stop_seconds:
            self.stop("idle")

    def stop(self, reason="stopped"):
        if self.owned():
            os.kill(self.pid, signal.SIGTERM)
            for _ in range(30):
                if not self.owned():break
                self.sleep(1)
            else:
                os.kill(self.pid, signal.SIGKILL)
            print(json.dumps({"model_server": reason, "pid": self.pid}), flush=True)
        self.process = self.pid = self.idle_since = None
        self.pid_file.unlink(missing_ok=True)


def served_model(command):
    """`--model` of an mlx_lm server command line; "" when it has none, None for other hosts."""
    if "mlx_lm" not in command:return None
    args = command.split()
    for i, arg in enumerate(args):
        if arg == "--model" and i + 1 < len(args):return args[i + 1]
        if arg.startswith("--model="):return arg.split("=", 1)[1]
    return ""


def listener_commands(port):
    """Command lines of local processes listening on a TCP port (macOS lsof/ps)."""
    try:
        pids = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=10).stdout.split()
        return [subprocess.run(["/bin/ps", "-ww", "-o", "command=", "-p", pid], capture_output=True, text=True,
            timeout=10).stdout.strip() for pid in dict.fromkeys(pids)]
    except (OSError, subprocess.TimeoutExpired):
        return []


def backend_check(base_url, model, *, timeout=5):
    """(ok, reason) without sending a chat request.

    mlx_lm.server reloads weights whenever a request names a different model, so on
    a shared local host a mismatched name would evict another project's model.
    """
    try:
        response = httpx.get(base_url.rstrip("/").removesuffix("/v1") + "/health", timeout=timeout)
        if response.status_code == 404:
            response = httpx.get(base_url.rstrip("/") + "/models", timeout=timeout)
        if response.status_code != 200:
            return False, f"model host health check returned HTTP {response.status_code}"
    except httpx.HTTPError as error:
        return False, "model host unreachable: " + type(error).__name__
    url = urlparse(base_url)
    if url.hostname in LOOPBACK and url.port:
        for command in listener_commands(url.port):
            served = served_model(command)
            if served is not None and served != model:
                return False, (f"shared mlx_lm host serves {served or 'no fixed model'!r}, not {model!r}; "
                    "requests are withheld because a different model name would make it swap models")
    return True, None


def online(urls=PROBE_URLS, timeout=10):
    """True when any public job host answers; offline runs leave source intervals unclaimed."""
    with httpx.Client(headers=HEADERS, timeout=timeout) as client:
        for url in urls:
            try:
                client.head(url)
                return True
            except httpx.HTTPError:
                continue
    return False


def run_collector(store, sources, raw_dir, *, check=online):
    store.set_status("collector", state="running", started_at=now(), pid=os.getpid())
    if not check():
        store.set_status("collector", state="offline", finished_at=now(), error="network unavailable; no source fetched or claimed")
        print(json.dumps({"collector": "offline"}), flush=True)
        return {"state": "offline"}
    summary = fetch_all(store, sources, raw_dir)
    error = "failed sources: " + ", ".join(sorted(summary["failed"])) if summary["failed"] else None
    fields = {"last_success_at": now()} if summary["fetched"] else {}
    store.set_status("collector", state="partial" if error else "ok", finished_at=now(), last_run=summary, error=error, **fields)
    print(json.dumps({"collector": "done", **summary}), flush=True)
    return {"state": "partial" if error else "ok", **summary}


def load_screener():
    """(screener, None) when the optional CLM screen is installed, else (None, reason)."""
    try:
        from .screen import HEADS_PATH, Screener
        if not HEADS_PATH.exists():
            return None, f"CLM heads not found at {HEADS_PATH}"
        return Screener(), None
    except Exception as error:  # missing mlx extra, bad checksum, ...
        return None, f"screen unavailable: {type(error).__name__}: {str(error)[:200]}"


def screen_pending(store, screener, policy, model, *, limit=256, chunk=32, stop=None):
    """Score queued jobs lacking a current screen score, title-priority order first.
    The score only orders the queue; returns how many jobs were scored. `stop` is
    checked between chunks so another GPU client is not kept waiting."""
    scored = store.screen_scores(screener.version)
    todo = []
    for row in store.rows():
        if row["missing"] or row["key"] in scored:continue
        job = Job.model_validate_json(row["data"])
        if row["assessment_key"] != assessment_key(job, policy, model):
            todo.append((priority(row), job))
    todo = [job for _, job in sorted(todo, key=lambda item: item[0])[:limit]]
    started, scored = time.monotonic(), 0
    for i in range(0, len(todo), chunk):
        if stop and stop():break
        part = todo[i:i + chunk]
        store.save_screen_scores(screener.version, [(j.key, j.content_hash(), s) for j, s in zip(part, screener.score(part))])
        scored += len(part)
    if scored:
        print(json.dumps({"screen": "scored", "jobs": scored, "seconds": round(time.monotonic() - started, 1)}), flush=True)
    if scored < limit:
        screener.encoder.close()  # nothing left (or yielding): free the encoder until needed
    return scored


def next_retry_wait(store, idle_seconds):
    """Seconds until the earliest future retry, capped by the idle interval."""
    due = store.db.execute("SELECT min(next_retry_at) FROM inference_queue WHERE status='retry' AND next_retry_at>?", (now(),)).fetchone()[0]
    if not due:return idle_seconds
    return max(30, min(idle_seconds, (datetime.fromisoformat(due) - datetime.now(timezone.utc)).total_seconds()))


def pause(store, state, wait, *, awake, server=None, screener=None, **fields):
    """Release GPU-related resources and record why the analyzer is not working."""
    awake.release()
    if server is not None:
        server.stop(state)
    if screener is not None:
        screener.encoder.close()
    store.set_status("analyzer", state=state, **fields)
    print(json.dumps({"analyzer": state, **fields}, default=list), flush=True)
    return state, wait


def work_once(store, policy, state_path, base_url, model, *, batch=10, attempts_per_stage=3, idle_seconds=300,
              awake=None, check=backend_check, screener=None, screen_limit=256, server=None,
              power=None, others=None):
    """One analyzer step -> (state, seconds to wait; None means apply outage backoff).

    `others` lists other clients of the shared host; model work (CLM screen and Qwen)
    pauses while there are any, so they effectively go first. `power` gates model
    work on AC power. The screen only reorders work. `server`, when given, starts
    the local model host on demand and stops it when idle or on battery.
    """
    awake = awake or KeepAwake(False)
    others = others or (lambda: set())
    busy = others()
    if busy:  # checked first: never stop a host while another client is using it
        return pause(store, "yielding", 60, awake=awake, screener=screener, error=None, other_clients=sorted(busy))
    if power is not None and not power():
        return pause(store, "waiting_for_ac_power", 300, awake=awake, server=server, screener=screener, error=None)
    scores = None
    if screener is not None:
        screened = screen_pending(store, screener, policy, model, limit=screen_limit, stop=lambda: bool(others()))
        scores = store.screen_scores(screener.version)
        store.set_status("analyzer", screen={"enabled": True, "version": screener.version, "scored_now": screened, "scored_total": len(scores)})
    with try_lock(str(state_path) + ".inference.lock") as held:
        if not held:
            store.set_status("analyzer", state="waiting_for_lock", error=None)
            return "waiting_for_lock", 60
        tasks = pending_tasks(store, policy, model, scores=scores)
        if not tasks:
            awake.release()
            if server is not None:
                server.idle()
            store.set_status("analyzer", state="idle", eligible=0, error=None, last_success_at=now())
            return "idle", next_retry_wait(store, idle_seconds)
        ok, reason = server.ensure(lambda: check(base_url, model)) if server is not None else check(base_url, model)
        if not ok:
            return pause(store, "waiting_for_model", None, awake=awake, error=reason)
        if server is not None:
            server.busy()
        awake.hold()
        selected, attempted, completed, yielded = tasks[:batch], 0, 0, set()
        started = time.monotonic()
        try:
            with httpx.Client(timeout=240) as client:
                for row, key, _ in selected:
                    if attempted and (yielded := others()):
                        break  # another client arrived: let its requests go first
                    attempted += 1
                    completed += int(process_task(store, client, policy, base_url, model, row, key, attempts_per_stage))
        except BackendUnavailable as error:
            return pause(store, "waiting_for_model", None, awake=awake, error=str(error))
    last = {"attempted": attempted, "completed": completed, "seconds": round(time.monotonic() - started, 1)}
    store.set_status("analyzer", state="working", eligible=len(tasks) - attempted, last_batch=last,
        error=None, last_success_at=now())
    print(json.dumps({"analyzer": "batch", **last, "eligible_remaining": len(tasks) - attempted}), flush=True)
    if yielded:
        return pause(store, "yielding", 60, awake=awake, error=None, other_clients=sorted(yielded))
    return "working", 0


def run_worker(store, policy_path, state_path, base_url, model, *, batch=10, attempts_per_stage=3,
               idle_seconds=300, max_backoff=900, keep_awake=True, once=False, screen=True,
               manage_server=True, idle_stop_seconds=600, require_ac=True, yield_to_others=True,
               repo=".", sleep=time.sleep):
    """Serial analyzer loop: one request at a time against the shared model host."""
    awake = KeepAwake(keep_awake)
    backoff = 60
    screener, screen_note = load_screener() if screen else (None, "disabled")
    # Only a loopback endpoint can be started here; a remote host is used as it is.
    local_host = urlparse(base_url).hostname in LOOPBACK
    server = ManagedServer(repo, base_url, model, idle_stop_seconds=idle_stop_seconds) if manage_server and local_host else None
    store.set_status("analyzer", state="starting", pid=os.getpid(), started_at=now(), base_url=base_url, model=model,
        error=None, screen={"enabled": screener is not None, "note": screen_note},
        managed_server=manage_server, require_ac=require_ac, yield_to_others=yield_to_others)
    try:
        while True:
            policy = json.loads(Path(policy_path).read_text())
            state, wait = work_once(store, policy, state_path, base_url, model, batch=batch,
                attempts_per_stage=attempts_per_stage, idle_seconds=idle_seconds, awake=awake, screener=screener,
                server=server, power=on_ac_power if require_ac else None,
                others=(lambda: other_clients(base_url)) if yield_to_others else None)
            if state == "waiting_for_model":
                wait, backoff = backoff, min(backoff * 2, max_backoff)
            else:
                backoff = 60
            if once:
                return state
            if wait:
                sleep(wait)
    finally:
        awake.release()
        if server is not None:
            server.stop("analyzer_stopped")
        if screener is not None:
            screener.close()
        store.set_status("analyzer", state="stopped", pid=None)


def queue_summary(store, policy, model):
    tasks = {(r["job_key"], r["input_key"]): r for r in store.db.execute("SELECT job_key,input_key,status,error,next_retry_at FROM inference_queue")}
    timestamp = now()
    counts = {"active": 0, "assessed": 0, "queued": 0, "retry_due": 0, "retry_waiting": 0, "next_retry_at": None}
    for row in store.rows():
        if row["missing"]:continue
        counts["active"] += 1
        key = assessment_key(Job.model_validate_json(row["data"]), policy, model)
        if row["assessment_key"] == key:
            counts["assessed"] += 1
            continue
        task = tasks.get((row["key"], key))
        if task and (task["status"] == "retry" or task["error"]):
            if task["next_retry_at"] and task["next_retry_at"] > timestamp:
                counts["retry_waiting"] += 1
                counts["next_retry_at"] = min(counts["next_retry_at"] or task["next_retry_at"], task["next_retry_at"])
            else:
                counts["retry_due"] += 1
        else:
            counts["queued"] += 1
    return counts


def status_report(store, repo, policy, model, base_url, *, probe=True):
    from .launchd import agent_states
    from .publish import git_summary
    sources = store.db.execute("SELECT id,succeeded_at,error FROM sources ORDER BY id").fetchall()
    report = {"generated_at": now(), "components": store.statuses(), "queue": queue_summary(store, policy, model),
        "sources": {"total": len(sources), "with_errors": [s["id"] for s in sources if s["error"]],
            "latest_success": max((s["succeeded_at"] for s in sources if s["succeeded_at"]), default=None)},
        "screen_scores": dict(store.db.execute("""SELECT s.version, count(*) FROM screen_scores s JOIN jobs j
            ON j.key=s.job_key AND j.content_hash=s.content_hash WHERE j.missing=0 GROUP BY s.version""").fetchall()),
        "git": git_summary(repo), "agents": agent_states()}
    if probe:
        ok, reason = backend_check(base_url, model)
        report["model_host"] = {"base_url": base_url, "model": model, "ok": ok, "reason": reason}
    return report
