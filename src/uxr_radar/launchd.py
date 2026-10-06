"""Per-user LaunchAgents for the collector, analyzer and publisher (no sudo).

launchd does not read shell profiles or direnv, so each agent carries an explicit
environment and runs the project's .venv entry point (no dependency resolution).
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import time
from pathlib import Path

PREFIX = "io.github.xthomaswang.uxr-radar"
AGENTS = {
    # Hourly; per-source minimum intervals (6/24 h) still apply inside fetch_all.
    "collector": {"args": ["collect"], "StartInterval": 3600, "RunAtLoad": True, "ProcessType": "Background"},
    # Long-running serial worker; launchd restarts it at most once a minute.
    "analyzer": {"args": ["worker"], "KeepAlive": True, "RunAtLoad": True, "ThrottleInterval": 60, "ProcessType": "Standard"},
    # First run one interval after load, so a fresh collection precedes publication.
    "publisher": {"args": ["publish"], "StartInterval": 3600, "RunAtLoad": False, "ProcessType": "Background"},
}
DEFAULT_ROLES = tuple(AGENTS)
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def label(role):
    return f"{PREFIX}.{role}"


def agent_environment(base_url, model, hf_home):
    return {"PATH": PATH, "LANG": "en_US.UTF-8", "PYTHONUNBUFFERED": "1", "HF_HOME": str(hf_home),
        "HF_HUB_OFFLINE": "1", "UXR_LLM_BASE_URL": base_url, "UXR_LLM_MODEL": model}


def agent_plist(role, repo, environment):
    spec = AGENTS[role]
    repo = Path(repo).resolve()
    logs = repo / "state" / "logs"
    plist = {"Label": label(role),
        "ProgramArguments": [str(repo / ".venv" / "bin" / "uxr-radar"), *spec["args"], "--log-dir", str(logs)],
        "WorkingDirectory": str(repo), "EnvironmentVariables": dict(environment),
        "StandardOutPath": str(logs / f"{role}.launchd.log"), "StandardErrorPath": str(logs / f"{role}.launchd.log"),
        "ProcessType": spec["ProcessType"], "RunAtLoad": spec["RunAtLoad"]}
    for key in ("StartInterval", "KeepAlive", "ThrottleInterval"):
        if key in spec:
            plist[key] = spec[key]
    return plist


def launchctl(*args):
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=60)


def install(repo, environment, *, roles=DEFAULT_ROLES, directory=None):
    repo = Path(repo).resolve()
    if not (repo / ".venv" / "bin" / "uxr-radar").exists():
        raise RuntimeError("Missing .venv/bin/uxr-radar; run `uv sync --extra inference` first")
    directory = Path(directory or Path.home() / "Library" / "LaunchAgents")
    directory.mkdir(parents=True, exist_ok=True)
    (repo / "state" / "logs").mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    installed = []
    for role in roles:
        path = directory / f"{label(role)}.plist"
        path.write_bytes(plistlib.dumps(agent_plist(role, repo, environment)))
        launchctl("bootout", f"{domain}/{label(role)}")  # replace an existing definition
        for attempt in range(5):
            result = launchctl("bootstrap", domain, str(path))
            if result.returncode == 0:
                break
            time.sleep(1 + attempt)  # bootout can still be tearing the old job down
        else:
            raise RuntimeError(f"launchctl bootstrap {role} failed: {result.stderr.strip()[-300:]}")
        launchctl("enable", f"{domain}/{label(role)}")
        installed.append(str(path))
    return installed


def uninstall(*, roles=tuple(AGENTS), directory=None):
    directory = Path(directory or Path.home() / "Library" / "LaunchAgents")
    removed = []
    for role in roles:
        launchctl("bootout", f"gui/{os.getuid()}/{label(role)}")
        path = directory / f"{label(role)}.plist"
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


def agent_states():
    states = {}
    for role in AGENTS:
        try:
            result = launchctl("print", f"gui/{os.getuid()}/{label(role)}")
        except (OSError, subprocess.TimeoutExpired):
            states[role] = {"loaded": None}
            continue
        if result.returncode:
            states[role] = {"loaded": False}
            continue
        info = {"loaded": True}
        for key in ("state", "pid", "runs", "last exit code"):
            match = re.search(rf"^\s*{key} = (.+)$", result.stdout, re.M)
            if match:
                info[key.replace(" ", "_")] = match.group(1).strip()
        states[role] = info
    return states
