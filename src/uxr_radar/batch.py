"""Hand the review backlog to a rented notebook GPU and import the results.

`export_batch` writes only queued public job facts (no reviews, assessments or
local state) plus a manifest pinning the prompt, policy and code. The notebook in
notebooks/colab_batch.ipynb runs the normal staged review on that file with vLLM.
`import_batch` treats the returned file as untrusted: a judgment is imported only
when the job content is unchanged, the weights are an accepted release of the
configured model, and the stored assessment passes local validation again.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from .core import MODEL_ALIASES, PROMPT_VERSION, Job, anonymous_policy, cache_model, digest, now, validate_assessment
from .pipeline import assessment_key
from .store import Store


def export_batch(store, policy, model, out_dir, *, repo="."):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "batch.sqlite3"
    path.unlink(missing_ok=True)
    rows = [r for r in store.rows() if not r["missing"]
        and r["assessment_key"] != assessment_key(Job.model_validate_json(r["data"]), policy, model)]
    batch = Store(path)
    with batch.db:
        batch.db.executemany("INSERT INTO jobs(key,source,data,content_hash,first_seen,last_seen) VALUES (?,?,?,?,?,?)",
            [(r["key"], r["source"], r["data"], r["content_hash"], r["first_seen"], r["last_seen"]) for r in rows])
    batch.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    batch.db.execute("PRAGMA journal_mode=DELETE")  # a single self-contained file for Drive
    batch.db.close()
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    manifest = {"created_at": now(), "jobs": len(rows), "prompt_version": PROMPT_VERSION,
        "policy_hash": digest(anonymous_policy(policy)), "cache_model": cache_model(model),
        "accepted_models": sorted(m for m, canonical in MODEL_ALIASES.items() if canonical == cache_model(model)),
        "code_commit": commit}
    (out / "policy.json").write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def import_batch(store, path, policy, model):
    """Import validated judgments from a returned batch file; returns counts per outcome."""
    accepted = {m for m in [*MODEL_ALIASES, model] if cache_model(m) == cache_model(model)}
    batch = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    batch.row_factory = sqlite3.Row
    counts = {"imported": 0, "already_assessed": 0, "changed_since_export": 0, "unknown_job": 0,
              "untrusted_model": 0, "stale_input": 0, "invalid": 0, "not_assessed": 0}
    for row in batch.execute("SELECT key, content_hash, assessment, assessment_key, assessed_at, assessed_by FROM jobs"):
        if not row["assessment"]:
            counts["not_assessed"] += 1
            continue
        main = store.db.execute("SELECT data, content_hash, assessment_key FROM jobs WHERE key=?", (row["key"],)).fetchone()
        if main is None:
            counts["unknown_job"] += 1
            continue
        if main["content_hash"] != row["content_hash"]:
            counts["changed_since_export"] += 1
            continue
        if row["assessed_by"] not in accepted:
            counts["untrusted_model"] += 1
            continue
        job = Job.model_validate_json(main["data"])  # local source facts are authoritative
        key = assessment_key(job, policy, row["assessed_by"])
        if main["assessment_key"] == key:
            counts["already_assessed"] += 1
            continue
        if row["assessment_key"] != key:
            counts["stale_input"] += 1
            continue
        try:
            assessment = validate_assessment(row["assessment"], job)
        except Exception:
            counts["invalid"] += 1
            continue
        reviews = batch.execute("SELECT at, model, input_key, duration, usage, raw, error, stage FROM reviews WHERE job_key=? AND input_key=?",
            (row["key"], key)).fetchall()
        with store.db:
            if store.db.execute("""UPDATE jobs SET assessment=?, assessment_key=?, assessed_at=?, assessed_by=?, error=NULL, attempts=0
                WHERE key=? AND content_hash=? AND assessment_key IS NOT ?""",
                (assessment.model_dump_json(), key, row["assessed_at"] or now(), row["assessed_by"], row["key"], row["content_hash"], key)).rowcount != 1:
                counts["already_assessed"] += 1
                continue
            store.db.executemany("INSERT INTO reviews(job_key,at,model,input_key,duration,usage,raw,error,stage) VALUES (?,?,?,?,?,?,?,?,?)",
                [(row["key"], *tuple(r)) for r in reviews])
            store.db.execute("INSERT OR IGNORE INTO inference_queue(job_key,input_key,updated_at) VALUES (?,?,?)", (row["key"], key, now()))
            store.db.execute("""UPDATE inference_queue SET stage='complete', status='complete', error=NULL, next_retry_at=NULL, updated_at=?
                WHERE job_key=? AND input_key=?""", (now(), row["key"], key))
        counts["imported"] += 1
    batch.close()
    return counts
