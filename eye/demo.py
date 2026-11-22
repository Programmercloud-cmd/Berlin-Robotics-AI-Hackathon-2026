"""Operator-only scenario. The runtime receives committed marketplace data."""

import logging
import time
from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from eye import monitor, runtime, simulator
from eye.db import connect, state
from eye.devin import SESSION_TIMEOUT, DevinReasoner

log = logging.getLogger(__name__)
HOUR = timedelta(hours=1)
PAYMENT_SCOPE = {key if key != "provider" else "payment_provider": value
                 for key, value in simulator.PAYMENT_SCOPE.items()}


def run(dsn, stop, model, delay=1.0, search=False, verify_devin=False):
    clock = simulator.DEMO_START

    def publish(traffic=1.0, fault=False, search_disruption=False):
        nonlocal clock
        with connect(dsn) as db:
            simulator.emit(db, clock, traffic=traffic, fault=fault,
                           search_disruption=search_disruption)
        deadline = time.monotonic() + 180
        while not stop.is_set() and time.monotonic() < deadline:
            with connect(dsn) as db:
                current = state(db, "last_window")
                if current and datetime.fromisoformat(current) >= clock:
                    clock += HOUR
                    stop.wait(delay)
                    return
            stop.wait(0.2)
        raise RuntimeError(f"Monitoring did not close window {clock}")

    log.info("Demo · normal operation")
    for _ in range(2):
        publish()
    log.info("Demo · traffic increases")
    for _ in range(2):
        publish(traffic=1.65)
    with connect(dsn) as db:
        benign_incidents = db.execute("select count(*) as n from incidents").fetchone()["n"]
    if benign_incidents:
        raise RuntimeError(f"Normal/traffic windows unexpectedly opened {benign_incidents} incidents")

    log.info("Demo · payment disruption")
    detected = None
    for _ in range(3):
        publish(fault=True)
        with connect(dsn) as db:
            detected = db.execute("""
                select a.* from anomalies a
                where a.metric_id = 'payment_success_rate'
                  and a.scope ?& array['country', 'platform', 'payment_provider']
                order by a.score desc limit 1
                """).fetchone()
        if detected:
            break
    if detected is None or detected["scope"] != PAYMENT_SCOPE:
        raise RuntimeError("No segmented payment anomaly was detected before registration")
    metric = monitor.TIMEOUT_RATE_METRIC
    with connect(dsn) as db:
        premature = db.execute("select 1 from metrics where id = %s", (metric["id"],)).fetchone()
    if premature:
        raise RuntimeError("Timeout rate was registered before the first payment investigation")
    log.info("Demo · detected %s in %s before registration",
             detected["metric_id"], detected["scope"])

    registration = runtime.register(dsn, metric)
    log.info("Demo · registered %s", metric["id"])
    publish(fault=True)
    publish(fault=True)

    log.info("Demo · healthy payment operation resumes")
    for _ in range(5):
        publish()
    with connect(dsn) as db:
        facts = db.execute("""
            select
                (select count(*) from graph_edges
                 where source = %s and kind = 'measures') as metric_links,
                (select count(*) from observations where metric_id = %s) as metric_observations,
                (select count(*) from anomalies a where a.metric_id = %s
                 and a.window_start > %s) as new_metric_anomalies,
                (select count(*) from incidents where recovered_at is not null) as recovered,
                (select count(*) from incidents where recovered_at is null
                 and scope = %s and metric_id = %s) as still_open,
                (select count(*) from evidence e join incidents i on i.id = e.incident_id
                 where i.recovered_at is not null and i.scope = %s
                   and i.metric_id = %s and i.report ? 'path') as evidence
            """, (f"metric:{metric['id']}", metric["id"], metric["id"], detected["window_start"],
                  Jsonb(detected["scope"]), detected["metric_id"],
                  Jsonb(detected["scope"]), detected["metric_id"])).fetchone()
    if facts["still_open"] or not all(value for key, value in facts.items() if key != "still_open"):
        raise RuntimeError(f"Demo did not complete graph extension and recovery: {facts}")
    if model:
        # Publish the rehearsal windows with the registered calculation available
        # even while the provider is still preparing an incident report.
        registration = runtime.enrich_registered_metric(dsn, metric, model)
        wait_for_reports(dsn, stop, model)
    log.info("Payment demo complete · %s · curation %s", facts, registration.get("reasoning", "unknown"))
    if verify_devin:
        check_devin(dsn, detected, metric["id"])
    if search:
        run_search(dsn, publish)
        wait_for_reports(dsn, stop, model)
    return facts


def wait_for_reports(dsn, stop, model):
    timeout = 180 + (SESSION_TIMEOUT if isinstance(model, DevinReasoner) else 0)
    deadline = time.monotonic() + timeout
    while not stop.is_set() and time.monotonic() < deadline:
        with connect(dsn) as db:
            if not db.execute("select 1 from pending_reports limit 1").fetchone():
                return
        stop.wait(0.25)
    if not stop.is_set():
        raise RuntimeError("Investigation reports are still pending.")


def check_devin(dsn, detected, metric_id):
    with connect(dsn) as db:
        investigation = db.execute(
            """select report->>'reasoning_session_id' as session_id from incidents
               where metric_id = %s and scope = %s and recovered_at is not null
                 and report->>'reasoning_provider' = 'devin' and report->>'reasoning' = 'model'""",
            (detected["metric_id"], Jsonb(detected["scope"])),
        ).fetchone()
        curation = db.execute(
            """select distinct properties->>'reasoning_session_id' as session_id from graph_edges
               where source = %s and provenance = 'curator.llm'
                 and properties->>'reasoning_provider' = 'devin'""",
            (f"metric:{metric_id}",),
        ).fetchone()
    if not investigation or not investigation["session_id"]:
        raise RuntimeError("Devin did not produce the accepted payment incident report.")
    if not curation or not curation["session_id"]:
        raise RuntimeError("Devin did not produce an accepted payment_timeout_rate graph proposal.")
    if curation["session_id"] == investigation["session_id"]:
        raise RuntimeError("Devin used the same session for investigation and curation.")
    log.info("Verified Devin incident report and payment_timeout_rate graph proposal from separate sessions")


def run_search(dsn, publish):
    log.info("Demo · search disruption")
    for _ in range(2):
        publish(search_disruption=True)
    log.info("Demo · healthy search operation resumes")
    for _ in range(4):
        publish()
    with connect(dsn) as db:
        incident = db.execute(
            """select i.id, i.recovered_at, i.report from incidents i
               where i.metric_id = 'search_click_rate' and i.scope = %s
                 and exists (select 1 from graph_nodes where id = 'process:search')
                 and exists (select 1 from evidence e where e.incident_id = i.id)
               order by i.opened_at desc limit 1""",
            (Jsonb(simulator.SEARCH_SCOPE),),
        ).fetchone()
    if not incident or not incident["recovered_at"]:
        raise RuntimeError(f"Search case did not complete detection and recovery: {incident}")
    log.info("Search demo complete · incident %s recovered at %s", incident["id"], incident["recovered_at"])
