"""Launch committed benchmark runs and read their local scoreboard."""

import fcntl
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from uuid import uuid4

from eye.benchmark_cases import CASE_HOURS, CASES, SUITE_VERSION
from eye.intervals import CADENCE, GRACE, HOUR
from eye.model import configured_devin, configured_provider
from eye.scenario import LAB_SEED

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / ".runtime" / "benchmarks"


def git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT, stderr=subprocess.DEVNULL)


def write_run(path, run):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(run, default=str, indent=2) + "\n")
    temporary.replace(path)


def checkout():
    try:
        sha = git("rev-parse", "HEAD").decode().strip()
        clean = not git("status", "--porcelain", "--untracked-files=all").strip()
        return {"sha": sha, "clean": clean, "devin_enabled": bool(configured_devin())}
    except subprocess.CalledProcessError:
        return {"sha": None, "clean": False, "devin_enabled": bool(configured_devin())}


def model_settings():
    provider = configured_provider()
    return {"eye_provider": "statistical" if provider == "local" else provider,
            "eye_model": os.getenv("MODEL_NAME") if provider == "chat-completions" else None,
            "reviewer": "devin", "devin_mode": os.getenv("DEVIN_MODE") or "organization default"}


@lru_cache(maxsize=128)
def commit_subject(sha):
    if re.fullmatch(r"[0-9a-f]{40}", sha):
        try:
            return git("show", "-s", "--format=%s", sha, "--").decode(errors="replace").strip()
        except subprocess.CalledProcessError:
            pass
    return ""


def run_context(run):
    # The original v1 archives predate these fields.
    if "commit_subject" not in run:
        run["commit_subject"] = commit_subject(run["sha"])
    if "methodology" not in run and run.get("version") == "1":
        run["methodology"] = {"effect_hours": 1, "follow_up_hours": 3, "window_minutes": 60,
                              "evaluation_minutes": 5, "grace_minutes": 15, "recovery_evaluations": 12}
    return run


def start(description=""):
    current = checkout()
    if not current["clean"]:
        raise ValueError("Commit your changes and use a clean checkout before starting a benchmark.")
    if not current["devin_enabled"]:
        raise ValueError("Configure DEVIN_API_KEY and DEVIN_ORG_ID and relaunch the lab to score reports.")
    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A benchmark is already running.") from None
        sha = current["sha"]
        identifier = uuid4().hex
        path = RESULTS / (identifier + ".json")
        minute = timedelta(minutes=1)
        run = {
            "id": identifier, "sha": sha, "version": SUITE_VERSION, "seed": LAB_SEED, "models": model_settings(),
            "description": description.strip(), "commit_subject": commit_subject(sha),
            "methodology": {"effect_hours": 1, "follow_up_hours": CASE_HOURS - 1, "window_minutes": HOUR // minute,
                            "evaluation_minutes": CADENCE // minute, "grace_minutes": GRACE // minute,
                            "recovery_evaluations": HOUR // CADENCE},
            "started_at": datetime.now(timezone.utc).isoformat(), "status": "preparing", "case_count": len(CASES),
            "cases": [],
        }
        write_run(path, run)
        source = Path(tempfile.mkdtemp(prefix="eye-benchmark-source-"))
        try:
            with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", sha))) as archive:
                archive.extractall(source, filter="data")
            env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "DATABASE_URL"}}
            env.update(PYTHONUNBUFFERED="1", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
            with path.with_suffix(".log").open("w") as log:
                subprocess.Popen([sys.executable, "-m", "eye.benchmark_runner", str(path)], cwd=source,
                                 env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                 pass_fds=(lock.fileno(),))
        except Exception:
            shutil.rmtree(source)
            run.update(status="failed", error="The committed benchmark could not be launched.")
            write_run(path, run)
            raise
    return identifier


def read_run(identifier):
    if re.fullmatch(r"[0-9a-f]{32}", identifier):
        path = RESULTS / (identifier + ".json")
        if path.is_file():
            return run_context(json.loads(path.read_text()))
    return None


def read_commit(sha):
    if re.fullmatch(r"[0-9a-f]{40}", sha):
        try:
            return git("show", "--no-patch", "--format=fuller", sha, "--").decode()
        except subprocess.CalledProcessError:
            pass
    return None


def median_minutes(cases, key):
    measured = [case[key] for case in cases if not case["benign"] and case.get(key) is not None]
    return median(measured) if measured else None


def scoreboard():
    runs = []
    for path in RESULTS.glob("*.json"):
        run = run_context(json.loads(path.read_text()))
        cases = run.pop("cases")
        run.update(completed=len(cases), detected=sum(c.get("detection_minutes") is not None for c in cases if not c["benign"]),
                   false_incidents=sum(c.get("false_incidents") or 0 for c in cases),
                   incidents=sum(c.get("incident_count") or 0 for c in cases),
                   reviewed=sum(len(c.get("reviews", ())) for c in cases),
                   median_detection_minutes=median_minutes(cases, "detection_minutes"),
                   median_recovery_minutes=median_minutes(cases, "recovery_minutes"))
        runs.append(run)
    return sorted(runs, key=lambda row: row["started_at"], reverse=True)
