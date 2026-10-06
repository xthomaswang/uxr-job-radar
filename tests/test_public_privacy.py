"""Public export guards use fictional markers, never real applicant identities."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("audit_public", Path(__file__).parents[1] / "scripts/audit_public.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


@pytest.fixture
def files():
    policy = {"purpose": "Anonymous research-job collection", "targets": ["UX research"],
        "preferred_experience_max_exclusive": 3, "include_qualification_gaps": True}
    version = "uxr-v5-anonymous-staged"
    row = {"key": "example:1", "prompt_version": version, "policy_hash": audit.digest(policy),
        "assessment": {"notes": ["Applicants must hold a relevant degree."], "evidence": [{"field": "education", "quote": "A degree is required."}]}}
    return {audit.POLICY_PATH: json.dumps(policy).encode(),
        audit.CORE_PATH: f'PROMPT_VERSION = "{version}"\n'.encode(),
        "positions.jsonl": (json.dumps(row) + "\n").encode(),
        "README.md": f'Generated: today · Prompt: `{version}` · Policy: `{audit.digest(policy)}`\n'.encode()}


def run_git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def stage_files(repo, files):
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    run_git(repo, "add", "--all")


def test_anonymous_export_with_employer_education_requirements_passes(files):
    assert audit.audit_files(files) == []


@pytest.mark.parametrize("path", ["config/profile.json", "private/notes.txt", "state/jobs.db",
    "raw/feed.json", "models/weights.safetensors", ".env", "config/.env.local"])
def test_private_paths_are_rejected(files, path):
    files[path] = b"{}"
    assert any(issue["path"] == path for issue in audit.audit_files(files))


def test_policy_rejects_personal_fields_and_unknown_keys(files):
    policy = json.loads(files[audit.POLICY_PATH])
    policy["school_name"] = "Fictional Test Academy"
    files[audit.POLICY_PATH] = json.dumps(policy).encode()
    issues = audit.audit_files(files)
    assert any(issue["path"] == audit.POLICY_PATH for issue in issues)
    assert "Fictional Test Academy" not in json.dumps(issues)


@pytest.mark.parametrize("narrative", ["Candidate holds a degree.", "Candidate is pursuing a doctorate.",
    "The applicant meets US work requirements.", "Candidate's specific experience is not provided."])
def test_candidate_narratives_do_not_escape_in_public_exports(files, narrative):
    row = json.loads(files["positions.jsonl"])
    row["assessment"]["notes"] = [narrative]
    files["positions.jsonl"] = json.dumps(row).encode()
    assert any("narrative" in issue["reason"] for issue in audit.audit_files(files))


@pytest.mark.parametrize("requirement", ["Candidates must hold a relevant degree.",
    "The ideal candidate has experience conducting interviews.",
    "Applicants should be pursuing a master's degree.",
    "Applicants must be eligible to work in the role's country."])
def test_generic_employer_requirements_are_not_biographies(files, requirement):
    row = json.loads(files["positions.jsonl"])
    row["assessment"]["notes"] = [requirement]
    files["positions.jsonl"] = json.dumps(row).encode()
    assert audit.audit_files(files) == []


def test_denied_identity_and_credentials_are_not_printed(files):
    marker = "Fictional Applicant Marker"
    credential = "ghp_" + "x" * 30
    files["README.md"] += (marker + "\n" + credential).encode()
    issues = audit.audit_files(files, [marker])
    assert any("denied" in issue["reason"] for issue in issues)
    assert any("credential" in issue["reason"] for issue in issues)
    assert marker not in json.dumps(issues)
    assert credential not in json.dumps(issues)


@pytest.mark.parametrize("field,value", [("prompt_version", "legacy-v1"), ("policy_hash", "stale")])
def test_old_assessments_cannot_pass_anonymous_publication_gate(files, field, value):
    row = json.loads(files["positions.jsonl"])
    row[field] = value
    files["positions.jsonl"] = json.dumps(row).encode()
    assert any(issue["path"] == "positions.jsonl" for issue in audit.audit_files(files))


def test_readme_must_match_current_policy(files):
    files["README.md"] = files["README.md"].replace(audit.digest(json.loads(files[audit.POLICY_PATH])).encode(), b"f" * 64)
    assert any(issue["path"] == "README.md" for issue in audit.audit_files(files))


def test_nested_personal_field_is_blocked(files):
    row = json.loads(files["positions.jsonl"])
    row["assessment"]["details"] = {"candidate_name": "Fictional Example"}
    files["positions.jsonl"] = json.dumps(row).encode()
    assert any("field found" in issue["reason"] for issue in audit.audit_files(files))


def test_audit_uses_index_not_cleaner_unstaged_worktree(tmp_path, files):
    run_git(tmp_path, "init", "-q")
    marker = "Fictional Private Marker"
    files["README.md"] += marker.encode()
    stage_files(tmp_path, files)
    (tmp_path / "README.md").write_text("clean worktree")
    assert any("denied" in issue["reason"] for issue in audit.audit_files(audit.staged_files(tmp_path), [marker]))


def test_history_still_fails_after_worktree_removes_private_data(tmp_path, files, capsys):
    run_git(tmp_path, "init", "-q")
    stage_files(tmp_path, {**files, "config/profile.json": b'{"candidate_name":"Fictional Example"}'})
    run_git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Initial fixture")
    (tmp_path / "config/profile.json").unlink()
    run_git(tmp_path, "add", "--all")
    run_git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Remove fixture")
    assert audit.main(["--repo", str(tmp_path)]) == 0
    capsys.readouterr()
    assert audit.main(["--repo", str(tmp_path), "--history"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert any(issue["path"].startswith("history/") for issue in output["errors"])
    assert "Fictional Example" not in json.dumps(output)


def test_denylist_must_be_ignored_or_external_and_never_prints_values(tmp_path, files, capsys):
    run_git(tmp_path, "init", "-q")
    stage_files(tmp_path, {**files, ".gitignore": b"state/\n"})
    state = tmp_path / "state"
    state.mkdir()
    denylist = state / "denylist.json"
    denylist.write_text(json.dumps({"terms": ["Fictional Secret Marker"]}))
    assert audit.main(["--repo", str(tmp_path), "--denylist", str(denylist)]) == 0
    assert "Fictional Secret Marker" not in capsys.readouterr().out
    unsafe = tmp_path / "not-ignored.json"
    unsafe.write_text(denylist.read_text())
    assert audit.main(["--repo", str(tmp_path), "--denylist", str(unsafe)]) == 2
    assert "Fictional Secret Marker" not in capsys.readouterr().out


def test_policy_cannot_hide_a_biography_in_nested_preference_fields(files):
    policy = json.loads(files[audit.POLICY_PATH])
    policy["targets"] = [{"school": "Fictional Test Academy"}]
    files[audit.POLICY_PATH] = json.dumps(policy).encode()
    assert any(issue["path"] == audit.POLICY_PATH for issue in audit.audit_files(files))


def test_employer_quotes_are_not_mistaken_for_applicant_biography(files):
    text="The ideal candidate is pursuing a relevant degree."
    row=json.loads(files["positions.jsonl"])
    row["assessment"]["evidence"]=[{"field":"education","quote":text}]
    files["positions.jsonl"]=json.dumps(row).encode()
    files["README.md"]+=f"| Evidence: {text}. Model notes to verify: none. |".encode()
    assert audit.audit_files(files)==[]
    # Actual known identifiers still fail, even inside an employer excerpt.
    assert any("denied" in e["reason"] for e in audit.audit_files(files,["relevant degree"]))


def test_experience_mapping_and_anonymous_capabilities_are_allowed(files):
    policy=json.loads(files[audit.POLICY_PATH])
    policy.update(experience_levels={"junior_max":3,"senior_min":5,"staff_min":8},anonymous_capabilities=["interviews","SQL"])
    old_hash=audit.digest(json.loads(files[audit.POLICY_PATH]))
    files[audit.POLICY_PATH]=json.dumps(policy).encode()
    files["README.md"]=files["README.md"].replace(old_hash.encode(),audit.digest(policy).encode())
    row=json.loads(files["positions.jsonl"]);row["policy_hash"]=audit.digest(policy)
    files["positions.jsonl"]=json.dumps(row).encode()
    assert audit.audit_files(files)==[]


def test_experience_mapping_rejects_ambiguous_or_personal_fields(files):
    policy=json.loads(files[audit.POLICY_PATH]);policy["experience_levels"]={"junior_max":5,"senior_min":3,"staff_min":8}
    files[audit.POLICY_PATH]=json.dumps(policy).encode()
    assert any(e["path"]==audit.POLICY_PATH for e in audit.audit_files(files))


@pytest.mark.parametrize("text,flagged", [("Join QZX alumni events.", True), ("qzx-2027 cohort", True),
    ("Lead aqzxb discussions with partners.", False), ("Fictional QZXcorp campus", False)])
def test_short_denylist_terms_match_whole_tokens_only(files, text, flagged):
    """A fictional three-letter code must not fail on ordinary words that contain it."""
    files["README.md"] += text.encode()
    issues = audit.audit_files(files, ["QZX"])
    assert any("denied" in e["reason"] for e in issues) is flagged
    assert "QZX" not in json.dumps(issues)


def test_longer_denylist_terms_still_match_anywhere(files):
    files["README.md"] += b"See fictionalprivatemarkerxyz.example for details."
    assert any("denied" in e["reason"] for e in audit.audit_files(files, ["fictionalprivatemarker"]))
