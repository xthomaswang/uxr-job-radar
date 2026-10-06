"""Audited publication of the generated README.md and positions.jsonl.

The publisher commits only those two generated files. The commit is built in a
temporary index from HEAD plus the fresh files, audited exactly as it will be
committed, and pushed only when every unpushed commit is a publisher commit.
It never force-pushes, merges or rebases, and it refuses to publish output that
uncommitted code or configuration could have produced.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .core import digest, now
from .pipeline import load_exclusions, render, verify_links
from .store import try_lock

GENERATED = ("README.md", "positions.jsonl")
# Paths whose uncommitted state could change generated output or its audit.
GENERATION_INPUTS = ("src", "config", "prompts", "scripts/audit_public.py", "pyproject.toml", "uv.lock")
TRAILER = "Uxr-Radar-Publish: auto"
# Per-run observation times; changes in them alone do not warrant a commit.
VOLATILE = frozenset({"link_checked_at", "last_seen", "retry_at", "inference_attempts"})
DISPLAY_TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} [A-Z]{2,5}")
IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG")


class Skip(Exception):
    """A deliberate refusal to publish. Blocking skips need a human; others clear by themselves."""

    def __init__(self, reason, detail=None, *, blocking=False):
        super().__init__(reason)
        self.reason, self.detail, self.blocking = reason, detail, blocking


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, repo):
        self.repo = Path(repo)

    def run(self, *args, check=True, input=None, env=None, timeout=120):
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True,
            input=input, env=env, timeout=timeout)
        if check and result.returncode:
            lines = result.stderr.strip().splitlines()
            raise GitError(f"git {args[0]} failed: {lines[-1][:300] if lines else result.returncode}")
        return result

    def out(self, *args, **kwargs):
        return self.run(*args, **kwargs).stdout.strip()


def fingerprint(readme, jsonl):
    """Digest of a publication without observation times; equal means nothing material changed."""
    rows = [{k: v for k, v in json.loads(line).items() if k not in VOLATILE} for line in jsonl.splitlines() if line.strip()]
    return digest([DISPLAY_TIME.sub("<time>", readme), rows])


def commit_message(readme, jsonl):
    rows = [json.loads(line) for line in jsonl.splitlines() if line.strip()]
    tracked = re.search(r"Tracked: (\d+)", readme)
    pending = re.search(r"Pending current model/policy review: \*\*(\d+)\*\*", readme)
    return (f"Update job radar: {len(rows)} listed, {sum(r.get('verified') is True for r in rows)} verified links, "
        f"{pending[1] if pending else '?'} of {tracked[1] if tracked else '?'} tracked pending review\n\n{TRAILER}\n")


def preflight(git, repo, branch):
    if Path(git.out("rev-parse", "--show-toplevel")).resolve() != repo:
        raise Skip("not_repository_root", str(repo), blocking=True)
    head = git.run("symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
    if head != f"refs/heads/{branch}":
        raise Skip("not_on_branch", f"HEAD is {head or 'detached'}; the publisher only commits on {branch}")
    gitdir = Path(git.out("rev-parse", "--absolute-git-dir"))
    if any((gitdir / name).exists() for name in IN_PROGRESS):
        raise Skip("git_operation_in_progress")
    if not git.run("config", "user.email", check=False).stdout.strip().endswith("@users.noreply.github.com"):
        raise Skip("identity_not_noreply", "set a GitHub noreply user.email in this public repository", blocking=True)
    dirty = git.out("status", "--porcelain=v1", "--untracked-files=all", "--", *GENERATION_INPUTS)
    if dirty:
        raise Skip("uncommitted_generation_inputs", [line[3:] for line in dirty.splitlines()][:20])


def unpushed(git, remote_ref):
    """Commits on HEAD that the remote lacks, after checking HEAD contains the remote."""
    if git.run("merge-base", "--is-ancestor", remote_ref, "HEAD", check=False).returncode:
        raise Skip("remote_has_new_commits", f"{remote_ref} is not contained in HEAD; pull --ff-only first", blocking=True)
    commits = git.out("rev-list", "--reverse", f"{remote_ref}..HEAD").split()
    foreign = [c[:12] for c in commits if TRAILER not in git.out("log", "-1", "--format=%B", c)]
    if foreign:
        raise Skip("unpushed_manual_commits", f"push or drop {', '.join(foreign)} manually; the publisher only pushes its own commits", blocking=True)
    return commits


def source_guard(store, *, max_age_seconds=3 * 3600):
    """Do not publish a degraded snapshot caused by a local outage or a stalled collector."""
    rows = store.db.execute("SELECT succeeded_at,error FROM sources").fetchall()
    if not rows:
        raise Skip("no_sources")
    if sum(bool(r["error"]) for r in rows) * 2 > len(rows):
        raise Skip("sources_degraded", f"{sum(bool(r['error']) for r in rows)} of {len(rows)} sources failing")
    latest = max((r["succeeded_at"] for r in rows if r["succeeded_at"]), default=None)
    if not latest or (datetime.now(timezone.utc) - datetime.fromisoformat(latest)).total_seconds() > max_age_seconds:
        raise Skip("collector_stale", f"latest successful fetch {latest or 'never'}")


def stage_and_audit(git, repo, denylist):
    """Build HEAD + generated files in a private index, then audit that exact tree."""
    index = Path(git.out("rev-parse", "--absolute-git-dir")) / "uxr-publish.index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    try:
        git.run("read-tree", "HEAD", env=env)
        git.run("add", "--", *GENERATED, env=env)
        changed = git.out("diff", "--cached", "--name-only", "HEAD", env=env).split()
        if set(changed) - set(GENERATED):
            raise Skip("unexpected_staged_paths", changed, blocking=True)
        command = [sys.executable, str(repo / "scripts" / "audit_public.py"), "--repo", str(repo)]
        if denylist and Path(denylist).exists():
            command += ["--denylist", str(denylist)]
        audit = subprocess.run(command, capture_output=True, text=True, env=env, timeout=300)
        try:
            report = json.loads(audit.stdout)
        except ValueError:
            report = {"ok": False, "errors": [{"path": ".", "reason": "audit produced no JSON report"}]}
        if audit.returncode or not report.get("ok"):
            raise Skip("audit_failed", report.get("errors"), blocking=True)
        return git.out("write-tree", env=env), changed, git.out("diff", "--cached", "--stat", "HEAD", env=env)
    finally:
        index.unlink(missing_ok=True)


def publish(repo, store, policy, model, *, push=True, dry_run=False, remote="origin", branch="main",
            refresh_seconds=6 * 3600, verify_age_seconds=6 * 3600, denylist=None, verify=True, exclude_path=None):
    repo = Path(repo).resolve()
    git = Git(repo)
    preflight(git, repo, branch)
    remote_ref = f"refs/remotes/{remote}/{branch}"
    result = {"state": "unchanged", "commit": None, "pushed": False}
    if push or dry_run:
        fetched = git.run("fetch", "--quiet", "--no-tags", remote, branch, check=False, timeout=120)
        result["fetch_ok"] = fetched.returncode == 0
    has_remote = git.run("rev-parse", "--verify", "--quiet", remote_ref, check=False).returncode == 0
    if push and not has_remote:
        raise Skip("remote_branch_unknown", remote_ref, blocking=True)
    if has_remote:
        unpushed(git, remote_ref)
    source_guard(store)
    if verify:
        result["links"] = verify_links(store, min_age_seconds=verify_age_seconds)
    render(store, policy, model, repo / "README.md", excluded=load_exclusions(exclude_path))
    new = {path: (repo / path).read_text() for path in GENERATED}
    old = {}
    for path in GENERATED:
        shown = git.run("show", f"HEAD:{path}", check=False)
        old[path] = shown.stdout if shown.returncode == 0 else ""
    material = fingerprint(new["README.md"], new["positions.jsonl"]) != fingerprint(old["README.md"], old["positions.jsonl"])
    last = git.out("log", "-1", "--format=%ct", "--", "README.md")
    refresh_due = time.time() - int(last or 0) >= refresh_seconds
    result.update(material_change=material, refresh_due=refresh_due)
    if material or refresh_due:
        tree, changed, stat = stage_and_audit(git, repo, denylist)
        result.update(audit="passed", changed=changed, diffstat=stat)
        if dry_run:
            result["state"] = "dry_run"
            return result
        if changed:
            parent = git.out("rev-parse", "HEAD")
            commit = git.out("commit-tree", "--no-gpg-sign", tree, "-p", parent,
                input=commit_message(new["README.md"], new["positions.jsonl"]))
            # Compare-and-swap: a concurrent manual commit makes this fail instead of being lost.
            git.run("update-ref", "-m", "uxr-radar publish", f"refs/heads/{branch}", commit, parent)
            git.run("reset", "-q", "--", *GENERATED, check=False)
            result.update(state="committed", commit=commit[:12])
    elif dry_run:
        result["state"] = "dry_run"
        return result
    if push:
        pending = unpushed(git, remote_ref)
        result["unpushed"] = len(pending)
        if pending:
            pushed = git.run("push", "--porcelain", remote, f"refs/heads/{branch}:refs/heads/{branch}", check=False, timeout=180)
            if pushed.returncode:
                lines = pushed.stderr.strip().splitlines()
                result.update(state="push_failed", push_error=lines[-1][:300] if lines else str(pushed.returncode))
                return result
            result.update(state="published", pushed=True, pushed_head=git.out("rev-parse", "--short=12", "HEAD"))
    return result


def run_publisher(repo, store, policy, model, *, state_path, **options):
    """Publish once and record the outcome; returns a process exit code."""
    with try_lock(str(state_path) + ".publish.lock") as held:
        if not held:
            print(json.dumps({"publish": "skipped", "reason": "publish_in_progress"}), flush=True)
            return 0
        store.set_status("publisher", state="running", started_at=now(), pid=os.getpid())
        try:
            result = publish(repo, store, policy, model, **options)
        except Skip as skip:
            state = "blocked" if skip.blocking else "skipped"
            store.set_status("publisher", state=state, reason=skip.reason, detail=skip.detail, finished_at=now(),
                error=skip.reason if skip.blocking else None)
            print(json.dumps({"publish": state, "reason": skip.reason, "detail": skip.detail}, ensure_ascii=False), flush=True)
            return 2 if skip.blocking else 0
        except Exception as error:
            store.set_status("publisher", state="error", error=f"{type(error).__name__}: {str(error)[:300]}", finished_at=now())
            raise
        fields = {"last_success_at": now()} if result["state"] in {"published", "committed", "unchanged", "dry_run"} else {}
        if result["pushed"]:
            fields["last_push_at"] = now()
        store.set_status("publisher", state=result["state"], reason=None, detail=None, finished_at=now(),
            last_result={k: v for k, v in result.items() if k != "diffstat"}, error=result.get("push_error"), **fields)
        print(json.dumps({"publish": result["state"], **result}, ensure_ascii=False), flush=True)
        return 1 if result["state"] == "push_failed" else 0


def git_summary(repo, remote="origin", branch="main"):
    """Local view only (no fetch): branch, head and publisher commits not yet pushed."""
    git = Git(repo)
    try:
        head = git.out("rev-parse", "--short=12", "HEAD")
        branch_name = git.run("symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip() or "detached"
        ahead = behind = None
        counts = git.run("rev-list", "--left-right", "--count", f"refs/remotes/{remote}/{branch}...HEAD", check=False)
        if counts.returncode == 0:
            behind, ahead = map(int, counts.stdout.split())
        last = git.out("log", "-1", f"--grep={TRAILER}", "--fixed-strings", "--format=%h %cI", "HEAD")
        return {"branch": branch_name, "head": head, "ahead": ahead, "behind": behind, "last_publisher_commit": last or None}
    except (GitError, OSError, subprocess.TimeoutExpired) as error:
        return {"error": str(error)}
