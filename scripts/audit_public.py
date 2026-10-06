#!/usr/bin/env python3
"""Audit the Git index before publication; never print matched private values."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

POLICY_KEYS = frozenset({"purpose", "targets", "preferred_experience_max_exclusive",
    "employment", "locations", "company_preference", "timing", "include_qualification_gaps",
    "experience_levels", "anonymous_capabilities"})
POLICY_PATH = "config/search_policy.json"
CORE_PATH = "src/uxr_radar/core.py"
SENSITIVE_FIELDS = frozenset({"candidate", "candidate_profile", "candidate_name", "applicant_name",
    "full_name", "first_name", "last_name", "school_name", "university_name", "alma_mater",
    "education_history", "graduation_date", "date_of_birth", "birth_date", "citizenship",
    "nationality", "resume", "resume_url", "portfolio", "portfolio_url", "personal_website",
    "email", "phone", "phone_number", "home_address", "work_history"})
CREDENTIALS = [re.compile(pattern, re.I) for pattern in (
    r"\bgh[pousr]_[a-z0-9]{20,}\b", r"\bgithub_pat_[a-z0-9_]{30,}\b",
    r"\bAKIA[0-9A-Z]{16}\b", r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"\bsk-(?:proj-)?[a-z0-9_-]{24,}\b",
    r"(?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*[\"']?[a-z0-9_+/=-]{24,}",
)]
NARRATIVES = [re.compile(pattern, re.I) for pattern in (
    r"\b(?:candidate|applicant)\s+(?:is\s+(?:pursuing|enrolled|studying|a\s+(?:citizen|student|graduate))|holds|possesses|has\s+(?:a|an|completed|worked)|meets|graduated|studied|worked)\b",
    r"\b(?:candidate|applicant)(?:'s|’s)\s+(?:specific\s+)?(?:name|school|university|citizenship|nationality|resume|education|background|experience|work\s+history)\b",
    r"\b(?:her|his|their)\s+(?:alma\s+mater|citizenship|graduation\s+date)\b",
    r"候选人(?:目前|正在|已|毕业|就读|拥有|具备|持有|符合|是|的(?:姓名|学校|学历|经历|国籍))",
)]


class AuditSetupError(Exception):
    pass


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode:
        raise AuditSetupError("Git operation failed; check repository and index state")
    return result.stdout


def staged_files(repo: Path) -> dict[str, bytes]:
    files = {}
    for record in git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        mode, oid, stage = header.split()
        if stage != b"0":
            raise AuditSetupError("Unmerged index; resolve conflicts before auditing")
        if mode != b"100644" and mode != b"100755":
            raise AuditSetupError("Symlinks and submodules require a separate publication review")
        files[path.decode()] = git(repo, "cat-file", "blob", oid.decode())
    return files


def history_snapshots(repo: Path):
    cache = {}
    for commit in git(repo, "rev-list", "--all").decode().splitlines():
        files = {}
        for record in git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
            if not record:
                continue
            header, path = record.split(b"\t", 1)
            mode, kind, oid = header.split()
            if kind != b"blob" or mode == b"120000":
                raise AuditSetupError("History contains a symlink or submodule requiring review")
            if oid not in cache:
                cache[oid] = git(repo, "cat-file", "blob", oid.decode())
            files[path.decode()] = cache[oid]
        yield commit, files


def forbidden_path(path: str) -> bool:
    p = PurePosixPath(path.lower())
    private_parts = {"private", "state", "raw", "models", "weights", "datasets", ".venv", "__pycache__", ".pytest_cache"}
    return (path.lower() == "config/profile.json" or bool(private_parts.intersection(p.parts))
        or p.name == ".env" or p.name.startswith(".env.")
        or p.name.startswith(("raw-", "raw_", "resume_", "resume-"))
        or p.suffix in {".db", ".sqlite", ".sqlite3", ".safetensors", ".gguf", ".pt", ".pth", ".pkl", ".pickle"})


def sensitive_fields(value) -> bool:
    if isinstance(value, dict):
        return any(str(k).casefold() in SENSITIVE_FIELDS or sensitive_fields(v) for k, v in value.items())
    return isinstance(value, list) and any(sensitive_fields(item) for item in value)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prompt_version(files: dict[str, bytes]) -> str | None:
    try:
        tree = ast.parse(files[CORE_PATH].decode())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PROMPT_VERSION" for t in node.targets):
                value = ast.literal_eval(node.value)
                return value if isinstance(value, str) else None
    except (KeyError, UnicodeDecodeError, SyntaxError, ValueError):
        pass
    return None


def valid_policy_types(policy: dict) -> bool:
    for key, value in policy.items():
        if key in {"targets", "employment", "anonymous_capabilities"}:
            if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v.strip() for v in value):
                return False
        elif key == "experience_levels":
            if not isinstance(value,dict) or set(value)!={"junior_max","senior_min","staff_min"}:
                return False
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) for v in value.values()):return False
            if not 0 <= value["junior_max"] < value["senior_min"] < value["staff_min"] <= 100:return False
        elif key == "preferred_experience_max_exclusive":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 100:
                return False
        elif key == "include_qualification_gaps":
            if not isinstance(value, bool):
                return False
        elif not isinstance(value, str) or not value.strip():
            return False
    return True


def denied(folded: str, term: str) -> bool:
    """Short ASCII terms (abbreviations such as a school code) match whole tokens only,
    so ordinary words that contain the letters do not fail the audit; longer or
    non-ASCII terms match anywhere."""
    term = term.casefold().strip()
    if len(term) <= 4 and term.isascii() and term.isalnum():
        return re.search(r"(?<![0-9a-z])" + re.escape(term) + r"(?![0-9a-z])", folded) is not None
    return term in folded


def audit_files(files: dict[str, bytes], denylist: list[str] | tuple[str, ...] = ()) -> list[dict[str, str]]:
    errors = []
    def fail(path, reason):
        issue = {"path": path, "reason": reason}
        if issue not in errors:
            errors.append(issue)

    policy = None
    try:
        policy = json.loads(files[POLICY_PATH])
        if not isinstance(policy, dict) or not policy or set(policy) - POLICY_KEYS or not valid_policy_types(policy):
            fail(POLICY_PATH, "Search policy must contain only approved anonymous preference fields")
            policy = None
        elif sensitive_fields(policy):
            fail(POLICY_PATH, "Candidate-specific fields are forbidden in search policy")
            policy = None
    except (KeyError, ValueError, UnicodeDecodeError):
        fail(POLICY_PATH, "Valid anonymous search policy is required")
    version = prompt_version(files)
    if not version or "anonymous" not in version:
        fail(CORE_PATH, "Current anonymous prompt version is required")
    policy_hash = digest(policy) if policy is not None else None

    for path, content in files.items():
        if forbidden_path(path):
            fail(path, "Private configuration, runtime state, raw data or model artifact is tracked")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            fail(path, "Binary artifact requires a separate public-content review")
            continue
        folded = text.casefold()
        if any(denied(folded, term) for term in denylist):
            fail(path, "Contains a locally denied private identifier")
        if any(pattern.search(text) for pattern in CREDENTIALS):
            fail(path, "Possible credential or private key")
        if path == "README.md":
            # Exact source excerpts may describe an ideal applicant generically. Keep
            # denylist/credential scans over all bytes, but audit biography prose outside quotes.
            prose=re.sub(r"Evidence:[^\n]*?(?=\. Model notes to verify:)","Evidence: [source excerpt]",text)
            if any(pattern.search(prose) for pattern in NARRATIVES):
                fail(path, "Candidate-specific narrative found in public export")
        values = []
        if path.endswith(".json"):
            try:
                values = [json.loads(text)]
            except ValueError:
                fail(path, "Invalid JSON prevents structured privacy audit")
        elif path.endswith(".jsonl"):
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    values.append(json.loads(line))
                except ValueError:
                    fail(path, "Invalid JSONL prevents structured privacy audit")
        if path.endswith(".jsonl"):
            def narrative_strings(value):
                if isinstance(value,dict):
                    for key,item in value.items():
                        if key!="evidence":yield from narrative_strings(item)
                elif isinstance(value,list):
                    for item in value:yield from narrative_strings(item)
                elif isinstance(value,str):yield value
            if any(pattern.search(value) for item in values for value in narrative_strings(item) for pattern in NARRATIVES):
                fail(path,"Candidate-specific narrative found in public export")
        if any(sensitive_fields(value) for value in values):
            fail(path, "Candidate-specific or personal-contact field found")
        if path == "positions.jsonl":
            for row in values:
                if not isinstance(row, dict):
                    fail(path, "Position row must be an object")
                    continue
                if not version or row.get("prompt_version") != version:
                    fail(path, "Position row was not generated with the current anonymous prompt")
                if not policy_hash or row.get("policy_hash") != policy_hash:
                    fail(path, "Position row was not generated with the current anonymous policy")
        if path == "README.md" and "Generated:" in text:
            tick = chr(96)
            prompt_match = re.search(r"Prompt:\s*" + tick + r"([^" + tick + r"]+)" + tick, text)
            policy_match = re.search(r"Policy(?: hash)?:\s*" + tick + r"([a-f0-9]{64})" + tick, text, re.I)
            if not prompt_match or prompt_match.group(1) != version:
                fail(path, "Generated README prompt metadata is stale or missing")
            if not policy_match or policy_match.group(1) != policy_hash:
                fail(path, "Generated README policy metadata is stale or missing")
    return errors


def load_denylist(repo: Path, filename: str | None) -> list[str]:
    if filename is None:
        return []
    path = Path(filename).expanduser().resolve()
    try:
        relative = path.relative_to(repo)
    except ValueError:
        relative = None
    if relative is not None:
        tracked = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", str(relative)], capture_output=True)
        ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "--quiet", "--", str(relative)], capture_output=True)
        if tracked.returncode == 0 or ignored.returncode != 0:
            raise AuditSetupError("Local denylist must be ignored and untracked, or outside the repository")
    try:
        value = json.loads(path.read_text())
        if isinstance(value, dict) and set(value) == {"terms"}:
            value = value["terms"]
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError
        return value
    except (OSError, ValueError, UnicodeDecodeError):
        raise AuditSetupError("Local denylist must be a readable JSON list of nonempty strings") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="Repository to audit; the default target is its Git index")
    parser.add_argument("--history", action="store_true", help="Also scan every reachable commit snapshot (old private history must fail)")
    parser.add_argument("--denylist", help="Ignored/local JSON string list, or {terms: [...]}; matches never appear in output")
    args = parser.parse_args(argv)
    try:
        repo = Path(args.repo).resolve()
        terms = load_denylist(repo, args.denylist)
        files = staged_files(repo)
        errors = audit_files(files, terms)
        commits = 0
        if args.history:
            for commit, snapshot in history_snapshots(repo):
                commits += 1
                for issue in audit_files(snapshot, terms):
                    errors.append({"path": "history/" + commit[:12] + "/" + issue["path"], "reason": issue["reason"]})
        print(json.dumps({"ok": not errors, "target": "git-index", "files_checked": len(files),
            "history_commits_checked": commits, "errors": errors}, ensure_ascii=False))
        return 1 if errors else 0
    except (AuditSetupError, UnicodeDecodeError):
        print(json.dumps({"ok": False, "errors": [{"path": ".", "reason": "Audit setup failed; check repository/index, file encoding and ignored JSON denylist"}]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
