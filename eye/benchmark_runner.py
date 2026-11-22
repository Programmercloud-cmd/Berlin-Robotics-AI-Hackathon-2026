"""Execute the committed benchmark in isolated local databases."""

import json
import logging
import shutil
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import Literal

from pgserver import get_server
from psycopg import sql
from pydantic import BaseModel, ConfigDict

from eye import investigation, monitor, runtime, simulator
from eye.benchmark import write_run
from eye.benchmark_cases import CASE_HOURS, CASES
from eye.db import connect, setup
from eye.devin import SESSION_TIMEOUT
from eye.intervals import CADENCE, DAY, HOUR
from eye.model import configured_devin, configured_model

log = logging.getLogger(__name__)
START = simulator.DEMO_START
REVIEW_RUBRIC_VERSION = 2
REVIEW_PROMPT = """Score Eye's incident reports for a seeded Test Lab benchmark. Use each case rubric as an
expectation, never as evidence that Eye observed. Return exactly one verdict per supplied report ID.
Use supported, partial, unsupported or insufficient for observable_break, scope and evidence.
Assess an initial report using only its own captured telemetry. For deeper findings, use the report
and telemetry referenced by initial_report_id as context for that same incident. Carry forward a
break or scope established by evidence unless the deeper findings revise or contradict it. The
deeper findings need not restate those facts. Assess the evidence for what the deeper findings add,
confirm, rule out or revise, and describe that contribution in the notes. If there is no linked
initial report, use only the deeper findings and their telemetry. Reward well-calibrated uncertainty
when the mechanism or scope remains unknown. Cite concrete observations and list only unsupported
claims made in the phase being scored."""
Verdict = Literal["supported", "partial", "unsupported", "insufficient"]


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    observable_break: Verdict
    scope: Verdict
    evidence: Verdict
    unsupported_claims: list[str]
    notes: str


def seed_template(dsn, seed):
    # Only the matching hours and the clean hour before the replay are needed.
    with connect(dsn) as db:
        setup(db)
        for day in (3, 2, 1):
            for hour in range(-1, CASE_HOURS):
                simulator.emit(db, START - day * DAY + hour * HOUR, seed=seed, patterned=True)
        simulator.emit(db, START - HOUR, seed=seed, patterned=True)
    runtime.initialize(dsn)
    for tick in range(1, CASE_HOURS * (HOUR // CADENCE) + 1):
        with connect(dsn) as db:
            monitor.materialize_rolling(db, START - HOUR + tick * CADENCE, START - HOUR, history_only=True)


def run_case(dsn, seed, case):
    identifier, name, control, benign, _ = case
    model = configured_model()
    stop = Event()
    writer = Thread(target=investigation.watch_reports, args=(dsn, stop, model), daemon=True) if model else None
    if writer:
        writer.start()
    try:
        for hour in range(CASE_HOURS):
            with connect(dsn) as db:
                simulator.emit(db, START + hour * HOUR, seed=seed, patterned=True, **(control if hour == 0 else {}))
            while runtime.step(dsn, model):
                pass
            while investigation.finish_next_branch(dsn, stop):
                pass
        if writer:
            until = monotonic() + SESSION_TIMEOUT + 30
            while monotonic() < until:
                with connect(dsn) as db:
                    if not db.execute("select 1 from pending_reports limit 1").fetchone():
                        break
                stop.wait(1)
        return case_result(dsn, identifier, name, benign)
    finally:
        stop.set()
        if writer:
            writer.join(timeout=2)


def case_result(dsn, identifier, name, benign):
    with connect(dsn) as db:
        first = db.execute(
            """select a.metric_id, a.scope, a.window_start, a.window_end, a.observed_at, a.observed, a.expected
               from anomalies a order by a.observed_at, a.id limit 1""",
        ).fetchone()
        incidents = db.execute(
            """select id, metric_id, scope, report->'break'->>'label' as step,
                      (report->>'recovery_observed_at')::timestamptz as recovered_at from incidents order by id""",
        ).fetchall()
        reports = db.execute("select * from report_snapshots order by incident_id, phase desc").fetchall()
        pending = db.execute("select count(*) as value from pending_reports").fetchone()["value"]
    recovered = [i["recovered_at"] for i in incidents if i["recovered_at"]]
    return {
        "id": identifier, "name": name, "benign": benign,
        "first_alert": first,
        "detection_minutes": (first["observed_at"] - START).total_seconds() / 60 if first else None,
        "false_incidents": len(incidents) if benign else None,
        "incident_count": len(incidents), "recovered_incidents": len(recovered),
        "recovery_minutes": (max(recovered) - START - HOUR).total_seconds() / 60
        if not benign and recovered and len(recovered) == len(incidents) else None,
        "incidents": incidents, "reports": reports, "pending_reports": pending,
    }


def review_item(case, snapshot, has_initial=False):
    evidence = snapshot["telemetry"]
    if snapshot["phase"] == "initial":
        shared = evidence.get("shared_explanation")
        evidence = {"anomaly": evidence["anomaly"], "transition": evidence.get("transition"),
                    "readings": [{key: row[key] for key in ("metric_id", "scope", "observed", "expected", "sample_size")}
                                 for row in evidence["metric_evidence"]],
                    "segments": evidence["segment_comparison"], "window": evidence["context"],
                    "events": [event for sample in evidence["event_evidence"] for event in sample["events"][:1]]}
        if shared:
            evidence["shared_explanation"] = shared
    else:
        evidence = [item for index, item in enumerate(evidence) if item not in evidence[:index]]
    prefix = f"{case['id']}/{snapshot['incident_id']}"
    phase = snapshot["phase"]
    item = {"id": f"{prefix}/{phase}", "phase": phase,
            "report": snapshot["report"], "telemetry": evidence,
            "observed_through": snapshot["observed_through"]}
    if phase == "deeper" and has_initial:
        item["initial_report_id"] = f"{prefix}/initial"
    return item


def score(run):
    rubrics = {case[0]: case[-1] for case in CASES}
    cases = []
    for case in run["cases"]:
        reports = case.get("reports", ())
        if reports:
            initial = {report["incident_id"] for report in reports if report["phase"] == "initial"}
            cases.append({"id": case["id"], "rubric": rubrics[case["id"]],
                          "reports": [review_item(case, report, report["incident_id"] in initial)
                                      for report in reports]})
    expected = {report["id"] for case in cases for report in case["reports"]}
    if not expected:
        raise RuntimeError("The suite produced no reports for Devin to review.")
    answer, provenance = configured_devin().reason("benchmark_review", REVIEW_PROMPT, {"cases": cases})
    reviews = [Review.model_validate(row).model_dump() for row in answer.get("reviews", ())]
    if len(reviews) != len(expected) or {review["id"] for review in reviews} != expected:
        raise RuntimeError("Devin did not return exactly one verdict for each report.")
    for case in run["cases"]:
        case["reviews"] = [review for review in reviews if review["id"].split("/", 1)[0] == case["id"]]
    run["review"] = {**provenance, "rubric_version": REVIEW_RUBRIC_VERSION}


def replay(path, run):
    by_case = {}
    with tempfile.TemporaryDirectory(prefix="eye-benchmark-db-") as directory:
        server = get_server(Path(directory) / "data")
        try:
            with connect(server.get_uri()) as db:
                db.commit()
                db.autocommit = True
                db.execute("create database benchmark_baseline")
            seed_template(server.get_uri(database="benchmark_baseline"), run["seed"])
            run["status"] = "replaying"
            write_run(path, run)
            with ProcessPoolExecutor(max_workers=4, mp_context=get_context("spawn")) as pool:
                jobs = {}
                for case in CASES:
                    database = "case_" + case[0]
                    with connect(server.get_uri()) as db:
                        db.commit()
                        db.autocommit = True
                        db.execute(sql.SQL("create database {} template benchmark_baseline").format(sql.Identifier(database)))
                    jobs[pool.submit(run_case, server.get_uri(database=database), run["seed"], case)] = case
                for job in as_completed(jobs):
                    identifier, name, _, benign, _ = jobs[job]
                    try:
                        by_case[identifier] = job.result()
                    except Exception:
                        log.exception("Benchmark case %s failed", identifier)
                        by_case[identifier] = {"id": identifier, "name": name, "benign": benign, "error": "Replay failed."}
                    run["cases"] = [by_case[case[0]] for case in CASES if case[0] in by_case]
                    write_run(path, run)
        finally:
            server.cleanup()


def main(path):
    run = json.loads(path.read_text())
    try:
        replay(path, run)
        run["status"] = "reviewing"
        write_run(path, run)
        score(run)
        run["status"] = "incomplete" if any(case.get("error") or case.get("pending_reports") for case in run["cases"]) else "scored"
    except Exception as exc:
        log.exception("Benchmark ended during %s", run["status"])
        run["error"] = str(exc) if type(exc) is RuntimeError else "See this run's local log."
        run["status"] = "review_failed" if run["status"] == "reviewing" else "failed"
    finally:
        run["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_run(path, run)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("pgserver").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        main(Path(sys.argv[1]))
    finally:
        source = Path(__file__).resolve().parents[1]
        if source.parent == Path(tempfile.gettempdir()) and source.name.startswith("eye-benchmark-source-"):
            shutil.rmtree(source)
