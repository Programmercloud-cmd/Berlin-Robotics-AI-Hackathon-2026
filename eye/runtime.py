"""The monitor consumes committed observation windows from PostgreSQL."""

import logging
from datetime import datetime

from eye import curation, graph, investigation, monitor
from eye.db import connect, set_state, setup, state

log = logging.getLogger(__name__)


def initialize(dsn):
    with connect(dsn) as db:
        setup(db)
        if state(db, "baseline_through"):
            return True
        baseline = db.execute("select max(start_at) as value from windows").fetchone()["value"]
        if baseline is None:
            return False
        graph.discover(db)
        for metric in monitor.INITIAL_METRICS:
            curation.curate(db, metric)
        monitor.materialize(db)
        graph.associate(db)
        for key in ("baseline_through", "last_window", "last_discovery"):
            set_state(db, key, baseline.isoformat())
        log.info("Established historical baseline through %s", baseline)
    return True


def register(dsn, metric, model=None):
    monitor.validate_calculation(metric["calculation"])
    with connect(dsn) as db:
        result = curation.curate(db, metric, model)
        monitor.materialize(db, metric_id=metric["id"])
        graph.associate(db)
    return result


def step(dsn, model=None):
    with connect(dsn) as db:
        baseline = state(db, "baseline_through")
        if baseline is None:
            return False
        window = db.execute(
            "select start_at from windows where start_at > %s order by start_at limit 1",
            (datetime.fromisoformat(state(db, "last_window")),),
        ).fetchone()
        if window is None:
            return False
        start = window["start_at"]
        new_types = db.execute("""
            select exists (
                select 1 from events e
                join event_objects eo on eo.event_id = e.id
                join objects o on o.id = eo.object_id
                where e.window_start = %s and (
                    not exists (select 1 from graph_nodes where id = 'activity:' || e.activity)
                    or not exists (select 1 from graph_nodes where id = 'process:' || o.type)
                )
            ) as value
            """, (start,)).fetchone()["value"]
        discover = new_types or (
            start - datetime.fromisoformat(state(db, "last_discovery"))
        ).total_seconds() >= 43200
        if discover:
            graph.discover(db)
            set_state(db, "last_discovery", start.isoformat())
        monitor.materialize(db, window_start=start)
        if discover:
            graph.associate(db)
        anomalies = monitor.detect(db, start, datetime.fromisoformat(baseline))
        investigation.track(db, start, anomalies, model)
        set_state(db, "last_window", start.isoformat())
        log.info("Observed %s · %d anomalies", start, len(anomalies))
        return True


def watch(dsn, stop, model=None, poll=0.5):
    ready = False
    while not stop.is_set():
        try:
            if not ready:
                ready = initialize(dsn)
            if ready and step(dsn, model):
                continue
        except Exception:
            log.exception("Monitoring window failed and will be retried")
            stop.wait(2)
        stop.wait(poll)
