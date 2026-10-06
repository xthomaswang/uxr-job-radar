"""Publisher behaviour against a throwaway repository and a local bare remote."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from uxr_radar.core import Job, PROMPT_VERSION
from uxr_radar.publish import TRAILER, Skip, fingerprint, publish
from uxr_radar.store import Store

ROOT = Path(__file__).parents[1]
POLICY = {"purpose": "Anonymous research-job collection", "targets": ["UX research"]}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def job(number, title="UX Researcher"):
    return Job(key=f"acme:{number}", source="acme", source_id=str(number), company="Acme", company_kind="large",
        title=title, location="Worldwide", url=f"https://jobs.example.com/{number}", description="Conduct user interviews.")


def assess(store, posting):
    """Store a validated relevant assessment so the posting is listed in the export."""
    from uxr_radar.core import Assessment, Evidence
    from uxr_radar.pipeline import assessment_key
    a = Assessment(decision="review", role="uxr", required_years=None, experience="not_stated", employment="unknown",
        reason="Relevant research duties.", uncertainties=[], evidence=[Evidence(field="role", quote="Conduct user interviews.")])
    store.db.execute("UPDATE jobs SET assessment=?,assessment_key=? WHERE key=?",
        (a.model_dump_json(), assessment_key(posting, POLICY, "test-model"), posting.key))
    store.db.commit()


@pytest.fixture
def world(tmp_path):
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.name", "Publisher Test")
    git(repo, "config", "user.email", "publisher-test@users.noreply.github.com")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "audit_public.py", repo / "scripts" / "audit_public.py")
    (repo / "config").mkdir()
    (repo / "config" / "search_policy.json").write_text(json.dumps(POLICY))
    (repo / "src" / "uxr_radar").mkdir(parents=True)
    (repo / "src" / "uxr_radar" / "core.py").write_text(f'PROMPT_VERSION = "{PROMPT_VERSION}"\n')
    (repo / ".gitignore").write_text("state/\n")
    store = Store(repo / "state" / "jobs.sqlite3")
    store.snapshot("acme", [job(1)])
    from uxr_radar.pipeline import render
    render(store, POLICY, "test-model", repo / "README.md")
    git(repo, "add", "--all")
    git(repo, "commit", "-q", "-m", "Initial public tree")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    return repo, remote, store


def run(world, **options):
    repo, _, store = world
    options.setdefault("verify", False)
    return publish(repo, store, POLICY, "test-model", **options)


def test_publishes_only_generated_files_and_keeps_user_staging(world):
    repo, remote, store = world
    (repo / "notes.txt").write_text("private draft")
    git(repo, "add", "notes.txt")
    store.snapshot("acme", [job(1), job(2)])
    assess(store, job(2))
    result = run(world)
    assert result["state"] == "published" and result["pushed"]
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["README.md", "positions.jsonl"]
    assert TRAILER in git(repo, "log", "-1", "--format=%B")
    assert git(remote, "rev-parse", "main") == git(repo, "rev-parse", "HEAD")
    assert git(repo, "diff", "--cached", "--name-only").split() == ["notes.txt"]
    assert "Tracked: 2" in git(repo, "show", "HEAD:README.md")
    assert json.loads(git(repo, "show", "HEAD:positions.jsonl"))["key"] == "acme:2"


def test_unchanged_publication_makes_no_commit(world):
    repo, _, store = world
    store.snapshot("acme", [job(1), job(2)])
    run(world)
    head = git(repo, "rev-parse", "HEAD")
    assert run(world)["state"] == "unchanged"
    assert git(repo, "rev-parse", "HEAD") == head


def test_refresh_commit_after_interval_even_without_material_change(world):
    repo, _, _ = world
    readme = repo / "README.md"
    lines = readme.read_text().splitlines(keepends=True)
    lines[4] = "Generated: 2000-01-01 00:00 EST" + lines[4][lines[4].index(" · Model"):]
    readme.write_text("".join(lines))
    git(repo, "commit", "-q", "-am", "Older generated timestamp")
    git(repo, "push", "-q", "origin", "main")
    head = git(repo, "rev-parse", "HEAD")
    assert run(world)["state"] == "unchanged"
    result = run(world, refresh_seconds=0)
    assert result["state"] == "published" and not result["material_change"] and result["refresh_due"]
    assert git(repo, "rev-parse", "HEAD^") == head


def test_dry_run_audits_without_committing(world):
    repo, _, store = world
    store.snapshot("acme", [job(1), job(2)])
    head = git(repo, "rev-parse", "HEAD")
    result = run(world, dry_run=True)
    assert result["state"] == "dry_run" and result["audit"] == "passed" and "README.md" in result["changed"]
    assert git(repo, "rev-parse", "HEAD") == head


def test_uncommitted_generation_input_blocks_publication(world):
    repo, _, store = world
    (repo / "config" / "search_policy.json").write_text(json.dumps({**POLICY, "targets": ["human factors"]}))
    store.snapshot("acme", [job(1), job(2)])
    with pytest.raises(Skip) as skip:
        run(world)
    assert skip.value.reason == "uncommitted_generation_inputs"


def test_audit_failure_blocks_commit_and_push(world):
    repo, remote, store = world
    (repo / "state" / "denylist.json").write_text(json.dumps(["Fictional Private Marker"]))
    store.snapshot("acme", [job(1), job(2, "Fictional Private Marker Researcher")])
    assess(store, job(2, "Fictional Private Marker Researcher"))
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(Skip) as skip:
        run(world, denylist=repo / "state" / "denylist.json")
    assert skip.value.reason == "audit_failed" and skip.value.blocking
    assert "Fictional Private Marker" not in json.dumps(skip.value.detail)
    assert git(repo, "rev-parse", "HEAD") == head == git(remote, "rev-parse", "main")


def test_failed_push_keeps_commit_and_retries_later(world):
    repo, remote, store = world
    git(repo, "remote", "set-url", "origin", str(repo.parent / "missing.git"))
    store.snapshot("acme", [job(1), job(2)])
    result = run(world)
    assert result["state"] == "push_failed" and result["commit"]
    git(repo, "remote", "set-url", "origin", str(remote))
    retried = run(world)
    assert retried["state"] == "published" and retried["commit"] is None
    assert git(remote, "rev-parse", "main") == git(repo, "rev-parse", "HEAD")


def test_manual_unpushed_commit_is_never_pushed_by_publisher(world):
    repo, remote, store = world
    (repo / "docs.md").write_text("work in progress")
    git(repo, "add", "docs.md")
    git(repo, "commit", "-q", "-m", "Local work")
    before = git(remote, "rev-parse", "main")
    store.snapshot("acme", [job(1), job(2)])
    with pytest.raises(Skip) as skip:
        run(world)
    assert skip.value.reason == "unpushed_manual_commits"
    assert git(remote, "rev-parse", "main") == before


def test_remote_ahead_requires_manual_pull(world, tmp_path):
    repo, remote, store = world
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    git(other, "config", "user.email", "other@users.noreply.github.com")
    git(other, "config", "user.name", "Other")
    (other / "CHANGELOG.md").write_text("elsewhere")
    git(other, "add", "CHANGELOG.md")
    git(other, "commit", "-q", "-m", "Pushed elsewhere")
    git(other, "push", "-q", "origin", "main")
    store.snapshot("acme", [job(1), job(2)])
    with pytest.raises(Skip) as skip:
        run(world)
    assert skip.value.reason == "remote_has_new_commits"


def test_publisher_requires_branch_and_noreply_identity(world):
    repo, _, _ = world
    git(repo, "config", "user.email", "person@example.com")
    with pytest.raises(Skip) as skip:
        run(world)
    assert skip.value.reason == "identity_not_noreply"
    git(repo, "config", "user.email", "publisher-test@users.noreply.github.com")
    git(repo, "checkout", "-q", "-b", "feature")
    with pytest.raises(Skip) as skip:
        run(world)
    assert skip.value.reason == "not_on_branch"


def test_stale_or_failing_sources_are_not_published(world):
    _, _, store = world
    store.db.execute("UPDATE sources SET succeeded_at='2000-01-01T00:00:00+00:00'")
    store.db.commit()
    with pytest.raises(Skip) as skip:
        run(world)
    assert skip.value.reason == "collector_stale"
    store.source_error("acme", "offline")
    with pytest.raises(Skip) as skip:
        run(world)
    assert skip.value.reason == "sources_degraded"


def test_readme_sections_are_capped_but_jsonl_is_complete(tmp_path, monkeypatch):
    from uxr_radar.pipeline import render
    monkeypatch.setattr("uxr_radar.pipeline.README_ROWS_PER_SECTION", 3)
    store = Store(tmp_path / "jobs.sqlite3")
    postings = [job(n) for n in range(5)]
    store.snapshot("acme", postings)
    for posting in postings:
        assess(store, posting)
    render(store, POLICY, "test-model", tmp_path / "README.md")
    readme = (tmp_path / "README.md").read_text()
    assert readme.count("](https://jobs.example.com/") == 3
    assert "2 more in this section are listed in [positions.jsonl](positions.jsonl)" in readme
    assert len((tmp_path / "positions.jsonl").read_text().splitlines()) == 5


def test_fingerprint_ignores_observation_times_only():
    row = {"key": "a:1", "verified": True, "link_checked_at": "2026-10-06T01:00:00+00:00", "last_seen": "x"}
    later = {**row, "link_checked_at": "2026-10-06T02:00:00+00:00", "last_seen": "y"}
    readme = "Generated: 2026-10-05 22:05 EDT\n| 2026-10-05 21:48 EDT | 35 |\n"
    assert fingerprint(readme, json.dumps(row)) == fingerprint(readme.replace("22:05", "23:05"), json.dumps(later))
    assert fingerprint(readme, json.dumps(row)) != fingerprint(readme, json.dumps({**row, "verified": False}))
