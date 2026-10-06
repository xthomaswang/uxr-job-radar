"""Backlog hand-off to a notebook GPU: export, simulated remote run, untrusted import."""
import json
import sqlite3

import httpx
import pytest

from uxr_radar.batch import export_batch, import_batch
from uxr_radar.core import Job, cache_model
from uxr_radar.pipeline import BackendUnavailable, assessment_key, render, review_pending
from uxr_radar.store import Store

LOCAL = "mlx-community/Qwen3.8-27B-8bit"
REMOTE = "Qwen/Qwen3.8-27B-FP8"


def posting(n, description="Conduct user interviews. Requires 5 years of UX research."):
    return Job(key=f"acme:{n}", source="acme", source_id=str(n), company="Acme", company_kind="large",
        title="UX Researcher", location="Worldwide", url=f"https://jobs.example.com/{n}", description=description)


def fake_infer(*args, **kwargs):
    if kwargs["stage"] == "overview":
        return json.dumps({"role": "uxr", "summary": "Conducts user research interviews.", "quote": "Conduct user interviews."}), 0.1, {}
    return json.dumps({"decision": "recommend", "role": "uxr", "required_years": 5, "experience": "at_least_3",
        "employment": "unknown", "reason": "Directly relevant research duties.", "uncertainties": [], "notes": [],
        "evidence": [{"field": "experience", "quote": "Requires 5 years of UX research."}]}), 0.1, {}


def test_official_releases_share_cache_keys_with_the_local_conversion():
    job = posting(1)
    assert cache_model(REMOTE) == cache_model("Qwen/Qwen3.8-27B") == LOCAL
    assert assessment_key(job, {}, REMOTE) == assessment_key(job, {}, LOCAL)
    assert assessment_key(job, {}, "some/other-model") != assessment_key(job, {}, LOCAL)


def test_concurrent_review_assesses_every_job_and_records_weights(monkeypatch, tmp_path):
    store = Store(tmp_path / "jobs.sqlite3")
    store.snapshot("acme", [posting(n) for n in range(12)])
    monkeypatch.setattr("uxr_radar.pipeline.infer", fake_infer)
    assert review_pending(store, {}, "http://gpu/v1", REMOTE, 100, concurrency=4) == 12
    rows = store.db.execute("SELECT assessed_by, assessment_key FROM jobs").fetchall()
    assert {r[0] for r in rows} == {REMOTE} and all(r[1] for r in rows)


def test_concurrent_review_stops_cleanly_when_the_host_goes_down(monkeypatch, tmp_path):
    store = Store(tmp_path / "jobs.sqlite3")
    store.snapshot("acme", [posting(n) for n in range(20)])
    def down(*args, **kwargs):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr("uxr_radar.pipeline.infer", down)
    with pytest.raises(BackendUnavailable):
        review_pending(store, {}, "http://gpu/v1", REMOTE, 100, concurrency=4)
    assert store.db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0
    assert store.db.execute("SELECT sum(total_attempts) FROM inference_queue").fetchone()[0] == 0


@pytest.fixture
def exported(tmp_path):
    main = Store(tmp_path / "main.sqlite3")
    main.snapshot("acme", [posting(n) for n in range(6)])
    out = tmp_path / "drive"
    manifest = export_batch(main, {}, LOCAL, out)
    return main, out, manifest


def run_remote(monkeypatch, out):
    """What the notebook does: the normal staged review against vLLM, inside the batch file."""
    monkeypatch.setattr("uxr_radar.pipeline.infer", fake_infer)
    remote = Store(out / "batch.sqlite3")
    review_pending(remote, {}, "http://127.0.0.1:8000/v1", REMOTE, 100, concurrency=3)
    remote.db.close()
    return out / "batch.sqlite3"


def test_export_contains_only_queued_public_job_facts(exported):
    main, out, manifest = exported
    batch = sqlite3.connect(out / "batch.sqlite3")
    assert batch.execute("SELECT count(*) FROM jobs").fetchone()[0] == manifest["jobs"] == 6
    assert batch.execute("SELECT count(*) FROM jobs WHERE assessment IS NOT NULL").fetchone()[0] == 0
    assert batch.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0
    assert manifest["cache_model"] == LOCAL and REMOTE in manifest["accepted_models"]
    assert json.loads((out / "policy.json").read_text()) == {}


def test_import_revalidates_and_records_provenance(monkeypatch, exported, tmp_path):
    main, out, _ = exported
    path = run_remote(monkeypatch, out)
    counts = import_batch(main, path, {}, LOCAL)
    assert counts["imported"] == 6
    assert {r[0] for r in main.db.execute("SELECT assessed_by FROM jobs")} == {REMOTE}
    assert main.db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 12
    assert {r[0] for r in main.db.execute("SELECT status FROM inference_queue")} == {"complete"}
    render(main, {}, LOCAL, tmp_path / "README.md")
    rows = [json.loads(l) for l in (tmp_path / "positions.jsonl").read_text().splitlines()]
    assert {r["model"] for r in rows} == {REMOTE}
    # A second import finds nothing new.
    assert import_batch(main, path, {}, LOCAL)["already_assessed"] == 6


def test_import_rejects_tampered_changed_and_foreign_results(monkeypatch, exported):
    main, out, _ = exported
    path = run_remote(monkeypatch, out)
    batch = sqlite3.connect(path)
    forged = json.loads(batch.execute("SELECT assessment FROM jobs WHERE key='acme:0'").fetchone()[0])
    forged["evidence"][0]["quote"] = "Requires 1 year of experience."  # not in the posting
    batch.execute("UPDATE jobs SET assessment=? WHERE key='acme:0'", (json.dumps(forged),))
    batch.execute("UPDATE jobs SET assessed_by='someone/else' WHERE key='acme:1'")
    batch.commit()
    batch.close()
    main.snapshot("acme", [posting(n) for n in range(6) if n != 2] + [posting(2, "Changed duties. Conduct user interviews.")])
    monkeypatch.setattr("uxr_radar.pipeline.infer", fake_infer)
    review_pending(main, {}, "http://127.0.0.1:8013/v1", LOCAL, 100, job_keys={"acme:3"})
    counts = import_batch(main, path, {}, LOCAL)
    assert counts == {"imported": 2, "already_assessed": 1, "changed_since_export": 1, "unknown_job": 0,
        "untrusted_model": 1, "stale_input": 0, "invalid": 1, "not_assessed": 0}
    assert main.db.execute("SELECT assessed_by FROM jobs WHERE key='acme:3'").fetchone()[0] == LOCAL
    assert main.db.execute("SELECT assessment FROM jobs WHERE key='acme:0'").fetchone()[0] is None
