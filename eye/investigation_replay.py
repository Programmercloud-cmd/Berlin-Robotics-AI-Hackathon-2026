"""Isolated seeded verification of Eye's report worker in two instrumented workflows."""

import argparse
import json
import logging
import os
from pathlib import Path
from threading import Event, Thread
from tempfile import mkdtemp
from time import monotonic

import uvicorn
from dotenv import load_dotenv
from pgserver import get_server
from psycopg.types.json import Jsonb

from eye import runtime, simulator
from eye.db import connect, set_state, setup, state
from eye.intervals import DAY, HOUR
from eye.metric_catalog import CATALOG
from eye.model import configured_model
from eye.scenario import LAB_SEED, Scenario
from eye.support_fixture import INTAKE_DEFINITION, RESOLUTION, RESOLUTION_DEFINITION, TICKET_SCOPE, support_events
from eye.web import make_app

log = logging.getLogger(__name__)
SEED = LAB_SEED
START = simulator.DEMO_START
PAYMENT = "payment_success_rate"
PAYMENT_SCOPE = {"country": "DE", "platform": "mobile", "payment_provider": "Stripe"}


def observe_until(dsn, check, seconds, stop):
    deadline = monotonic() + seconds
    while not stop.is_set() and monotonic() < deadline:
        with connect(dsn) as db:
            if result := check(db):
                return result
        stop.wait(.7)
    raise RuntimeError("Replay timed out waiting for the expected observation.")


def snapshot(dsn, incident):
    with connect(dsn) as db:
        return {
            "last_observed_at": state(db, "last_observed_at"),
            "incident": db.execute("select * from incidents where id=%s", (incident,)).fetchone(),
            "pending": bool(db.execute("select 1 from pending_reports where incident_id=%s", (incident,)).fetchone()),
            "evidence": db.execute("select * from evidence where incident_id=%s order by id", (incident,)).fetchall(),
        }


def seed(dsn, workflow, generic):
    with connect(dsn) as db:
        setup(db)
        if generic:
            set_state(db, "instrumentation", {"catalog": "none"})
        emit = support_events if workflow == "support" else simulator.emit
        options = {} if workflow == "support" else {"seed": SEED, "patterned": True}
        for day in (3, 2, 1):
            for hour in range(-1, 5):
                emit(db, START - day * DAY + hour * HOUR, **options)
        emit(db, START - HOUR, **options)
    runtime.initialize(dsn)


def drive(dsn, workflow, model, generic, stop, output):
    resolution = workflow == "support"
    metric, scope = (RESOLUTION, TICKET_SCOPE) if resolution else (PAYMENT, PAYMENT_SCOPE)
    emit = support_events if resolution else simulator.emit
    options = {} if resolution else {"seed": SEED, "patterned": True}
    if resolution:
        runtime.register(dsn, INTAKE_DEFINITION)
        print("Registering the resolution formula without graph placement.", flush=True)
        runtime.register(dsn, RESOLUTION_DEFINITION, model)
    elif generic:
        runtime.register(dsn, {**CATALOG[PAYMENT], "cadence_minutes": 5})
    if resolution:
        with connect(dsn) as db:
            emit(db, START, volume=1.65)
        observe_until(dsn, lambda db: state(db, "last_window") == START.isoformat(), 180, stop)
        with connect(dsn) as db:
            assert not db.execute("select 1 from anomalies where metric_id=%s", (metric,)).fetchone()
            healthy = db.execute("""select metric_id, scope, value, sample_size from observations
                                  where window_start=%s and metric_id=any(%s)
                                  order by metric_id, scope""",
                                 (START, [metric, INTAKE_DEFINITION["id"]])).fetchall()
        print("Healthy ticket surge produced no resolution alerts.", flush=True)
    else:
        healthy = None
    fault_start = START + HOUR if resolution else START
    payment_fault = Scenario(effect="payment_timeout", transition="payment", severity=.85, duration_hours=1,
                             scope={"country": "DE", "platform": "mobile", "provider": "Stripe"})
    controls = {"regression": True, "volume": 1.65} if resolution else {"scenarios": (payment_fault,)}
    with connect(dsn) as db:
        emit(db, fault_start, **options, **controls)

    def matching(db):
        if generic:
            return db.execute("""select i.id from incidents i where i.metric_id=%s and i.scope=%s
                                 order by i.id limit 1""", (metric, Jsonb(scope))).fetchone()
        return db.execute("select id from incidents where report->'break'->>'id'='payment' order by id limit 1").fetchone()

    incident = observe_until(dsn, matching, 180, stop)["id"]
    before = snapshot(dsn, incident)
    print(f"Incident {incident} opened · pending={before['pending']} · {before['last_observed_at']}", flush=True)
    for offset in range(1, 4):
        with connect(dsn) as db:
            emit(db, fault_start + offset * HOUR, **options)
    observe_until(dsn, lambda db: db.execute("select recovered_at from incidents where id=%s and recovered_at is not null",
                                             (incident,)).fetchone(), 220, stop)
    recovery = snapshot(dsn, incident)
    print(f"Recovery recorded · pending={recovery['pending']} · {recovery['last_observed_at']}", flush=True)

    def write(final):
        output.write_text(json.dumps({"workflow": workflow, "seed": SEED, "focus": metric,
                                      "healthy_volume_change": healthy, "at_detection": before,
                                      "at_recovery": recovery, "final": final}, default=str, indent=2) + "\n")

    write(None)
    if generic:
        observe_until(dsn, lambda db: not db.execute("select 1 from pending_reports where incident_id=%s",
                                                    (incident,)).fetchone(), 2200, stop)
        final = snapshot(dsn, incident)
        write(final)
        report = final["incident"]["report"]
        assert report.get("investigation_method") == "devin_graph", report.get("reasoning")
        assert before["pending"] and recovery["last_observed_at"] > before["last_observed_at"]
        reads = {f"E{row['id']}" for row in final["evidence"] if row["kind"] == "investigation read"
                 and row["payload"].get("result")}
        assert reads & set(report["evidence_ids"]), report["evidence_ids"]
        assert any(row["kind"] == "investigation conclusion" for row in final["evidence"])
    else:
        write(snapshot(dsn, incident))
    print(f"Replay verified · incident {incident} · {output}", flush=True)


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow", choices=("marketplace", "support"))
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    generic = os.getenv("EYE_INVESTIGATION") == "generic"
    if args.workflow == "support" and not generic:
        parser.error("The support replay requires EYE_INVESTIGATION=generic.")
    model = configured_model()
    if generic and not hasattr(model, "investigate"):
        parser.error("Configure EYE_MODEL=devin and the service-user credentials.")
    logging.basicConfig(level=logging.INFO)
    for name in ("mcp", "httpx", "pgserver"):
        logging.getLogger(name).setLevel(logging.WARNING)
    output_dir = Path(".runtime/devin-investigation")
    output_dir.mkdir(parents=True, exist_ok=True)
    folder = Path(mkdtemp(prefix=f"{args.workflow}-{args.port}-", dir=output_dir))
    postgres = get_server(folder / "postgres")
    stop = Event()
    dsn = postgres.get_uri()
    try:
        seed(dsn, args.workflow, generic)

        def replay():
            try:
                drive(dsn, args.workflow, model, generic, stop, folder / "evidence.json")
            except Exception:
                log.exception("Investigation replay failed")

        Thread(target=replay, daemon=True).start()
        uvicorn.run(make_app(dsn, model), host="127.0.0.1", port=args.port, access_log=False)
    finally:
        stop.set()
        postgres.cleanup()


if __name__ == "__main__":
    main()
