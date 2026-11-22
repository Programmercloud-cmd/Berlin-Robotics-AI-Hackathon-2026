"""The monitor consumes committed observation windows from PostgreSQL."""

import logging
from datetime import datetime

from psycopg.types.json import Jsonb

from eye import curation, graph, investigation, monitor
from eye.db import connect, set_state, setup, state
from eye.intervals import CADENCE, GRACE, HOUR
from eye.metric_catalog import CATALOG, INITIAL_METRICS, LEGACY, STAGED
from eye.metric_policy import CADENCES

log = logging.getLogger(__name__)


def initialize(dsn):
    with connect(dsn) as db:
        setup(db)
        previous = state(db, "baseline_through")
        if previous and state(db, "metric_catalog_version") == 1:
            # Existing baselines already contain source and event-type observations.
            # Enable alerts on those readings without rebuilding their history.
            completeness = CATALOG["data_completeness_ratio"]["access"]["alert_dimensions"]
            db.execute(
                """update metrics set access = jsonb_set(access, '{alert_dimensions}', %s)
                   where id = 'data_completeness_ratio'
                     and access->'alert_dimensions' is distinct from %s""",
                (Jsonb(completeness), Jsonb(completeness)),
            )
            if not state(db, "metric_policy_version"):
                with db.cursor() as cursor:
                    cursor.executemany("""update metrics set access=jsonb_set(access,'{cadence_minutes}',%s),
                                          direction=case when direction='none' then %s else direction end
                                          where id=%s and not access ? 'cadence_minutes'""",
                                       [(Jsonb(entry["access"]["cadence_minutes"]), entry["direction"], entry["id"])
                                        for entry in CATALOG.values()])
                set_state(db, "metric_policy_version", 1)
            return True
        baseline = (datetime.fromisoformat(previous) if previous else
                    db.execute("select max(start_at) as value from windows").fetchone()["value"])
        if baseline is None:
            return False
        graph.discover(db)
        staged = db.execute("select id from metrics where id = any(%s)", (list(STAGED),)).fetchall()
        for metric in [*INITIAL_METRICS, *(CATALOG[row["id"]] for row in staged)]:
            curation.curate(db, metric)
        db.execute("update metrics set direction = 'none' where id = any(%s)", (list(LEGACY),))
        if previous:
            db.execute("""delete from observations where metric_id = any(%s)""", (list(CATALOG),))
        monitor.materialize(db)
        graph.associate(db)
        if not previous:
            for key in ("baseline_through", "last_window", "last_discovery"):
                set_state(db, key, baseline.isoformat())
        set_state(db, "metric_catalog_version", 1)
        set_state(db, "metric_policy_version", 1)
        log.info("Established historical baseline through %s", baseline)
    return True


def register(dsn, metric, model=None):
    cadence = metric.get("cadence_minutes")
    if cadence is not None and cadence not in CADENCES:
        raise ValueError("Choose a supported check cadence in minutes.")
    if definition := CATALOG.get(metric["id"]):
        metric = {**definition, "direction": metric.get("direction", definition["direction"])}
    metric = {**metric, "cadence_minutes": cadence, "infer_cadence": cadence is None}
    monitor.validate_calculation(metric["calculation"])
    with connect(dsn) as db:
        result = curation.curate(db, metric, model)
        monitor.materialize(db, metric_id=metric["id"])
        graph.associate(db)
    return result


def enrich_registered_metric(dsn, metric, model):
    with connect(dsn) as db:
        result = curation.curate(db, metric, model)
        graph.associate(db)
    return result


def step(dsn, model=None):
    with connect(dsn) as db:
        if not db.execute("select pg_try_advisory_xact_lock(hashtext('eye.monitor')) as claimed").fetchone()["claimed"]:
            return False
        baseline = state(db, "baseline_through")
        if baseline is None:
            return False
        published = db.execute(
            """select max(coalesce(published_through, observation_snapshot,
                                   end_at + interval '15 minutes')) as cutoff from windows"""
        ).fetchone()["cutoff"]
        start = datetime.fromisoformat(state(db, "last_window")) + CADENCE
        end = start + HOUR
        observed_at = end + GRACE
        if published is None or observed_at > published:
            return False
        new_types = db.execute("""
            select exists (
                select 1 from events e
                join event_objects eo on eo.event_id = e.id
                join objects o on o.id = eo.object_id
                where e.occurred_at >= %s and e.occurred_at < %s and e.ingested_at <= %s
                  and (
                    not exists (select 1 from graph_nodes where id = 'activity:' || e.activity)
                    or not exists (select 1 from graph_nodes where id = 'process:' || o.type)
                )
            ) as value
            """, (start, end, observed_at)).fetchone()["value"]
        discover = new_types or (
            start - datetime.fromisoformat(state(db, "last_discovery"))
        ).total_seconds() >= 43200
        if discover:
            graph.discover(db, through=end, cutoff=observed_at)
            set_state(db, "last_discovery", start.isoformat())
        dates, facts = monitor.materialize_rolling(db, start, datetime.fromisoformat(baseline))
        if discover:
            graph.associate(db)
        anomalies = monitor.detect(db, start, dates, observed_at)
        investigation.track(db, start, anomalies, model, history=dates, facts=facts, observed_at=observed_at)
        set_state(db, "last_window", start.isoformat())
        set_state(db, "last_observed_at", observed_at.isoformat())
        log.info("Observed %s–%s UTC · %d anomalies", start.strftime("%H:%M"), end.strftime("%H:%M"), len(anomalies))
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
