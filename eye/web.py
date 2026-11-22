from contextlib import asynccontextmanager
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from eye import investigation, runtime
from eye.db import connect, state
from eye.metric_catalog import CATALOG, LEGACY
from eye.metric_readings import BERLIN, Facts, MAX_MATURITY, metric_readings
from eye.metric_insights import DETECTION_OP, history as metric_history
from eye.metric_policy import Cadence
from eye.observation import make_mcp
from eye.report_assembly import assemble_report

STATIC = Path(__file__).with_name("static")


class MetricRegistration(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    definition: str
    calculation: dict
    dimensions: list[str] = Field(default_factory=list)
    access: dict = Field(default_factory=dict)
    direction: str = "both"
    cadence_minutes: Cadence | None = None


class MetricCadence(BaseModel):
    cadence_minutes: Cadence | None = None


def make_app(dsn, model=None):
    mcp = make_mcp(dsn)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        stop = Event()
        worker = Thread(target=runtime.watch, args=(dsn, stop, model), daemon=True)
        worker.start()
        Thread(target=investigation.watch_reports, args=(dsn, stop, model), daemon=True).start()
        Thread(target=investigation.watch_branches, args=(dsn, stop), daemon=True).start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            stop.set()

    app = FastAPI(title="Eye of the System", lifespan=lifespan)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def snapshot(incident: int | None = Query(default=None, gt=0, le=9223372036854775807)):
        with connect(dsn) as db:
            edges = db.execute(
                "select * from graph_edges order by source, target, kind"
            ).fetchall()
            payload = {
                "last_window": state(db, "last_window"),
                "baseline_through": state(db, "baseline_through"),
                "planned_events": db.execute("""select distinct on (series) * from planned_events
                                               order by series, starts_at desc""").fetchall(),
                "event_expectations": [row["reading"] for row in db.execute(
                    """select reading from planned_event_readings where window_start=(select max(window_start) from planned_event_readings)
                       and (scope='{}' or scope in (select scope from planned_events)) order by scope"""
                ).fetchall()],
                "graph": {
                    "nodes": db.execute("select * from graph_nodes order by id").fetchall(),
                    "edges": edges,
                },
                "anomalies": db.execute(
                    "select * from anomalies order by window_start desc, score desc limit 250"
                ).fetchall(),
                "incidents": db.execute(
                    """select i.id, i.metric_id, i.scope, i.opened_at, i.updated_at, g.recovered_at,
                       i.healthy_windows, i.report, g.group_id, g.recovery_observed_at, exists (
                           select 1 from pending_reports p where p.incident_id = i.id
                       ) as investigation_pending,
                       (select jsonb_agg(r.decision order by r.incident_id) from incident_relationships r
                        where r.parent_id=i.id or r.incident_id=i.id) as grouping_decisions
                       from incidents i join incident_groups g on g.id=i.id
                       where i.id=g.group_id or i.id=%s
                       order by (i.id = %s) desc nulls last, (g.recovered_at is null) desc,
                                i.opened_at desc, i.id desc limit 50""", (incident, incident),
                ).fetchall(),
            }
            for row in payload["incidents"]:
                row["report"] = assemble_report(row["report"], row["id"], row["grouping_decisions"])
            return payload

    @app.get("/api/events")
    def observed_events():
        with connect(dsn) as db:
            window = state(db, "last_window")
            if not window:
                return []
            cutoff = state(db, "last_observed_at") or db.execute(
                """select coalesce(observation_snapshot, end_at + interval '15 minutes') as cutoff
                   from windows where start_at = %s""", (window,),
            ).fetchone()["cutoff"]
            return db.execute(
                """select e.id, e.activity, e.occurred_at, e.ingested_at, e.attrs
                   from events e where e.occurred_at <= %s and e.ingested_at <= %s
                   order by e.occurred_at desc, e.id desc limit 40""",
                (cutoff, cutoff),
            ).fetchall()

    @app.get("/api/incidents/{incident_id}/evidence")
    def evidence(incident_id: int, alerts_only: bool = False):
        with connect(dsn) as db:
            group = db.execute("select linked_incident_ids from incident_groups where id=%s", (incident_id,)).fetchone()
            ids = [incident_id, *(group["linked_incident_ids"] if group else [])]
            if alerts_only:
                return db.execute(
                    """select kind, payload from evidence where incident_id = any(%s)
                       and kind in ('anomaly', 'related anomaly', 'ongoing anomaly')
                       and payload ? 'id' order by window_start desc, id desc""",
                    (ids,),
                ).fetchall()
            return db.execute(
                """select * from evidence where incident_id = any(%s)
                   order by (kind in ('incident grouping', 'branch evidence', 'first observable break', 'observed events', 'anomaly')) desc,
                            window_start desc, id desc limit 120""",
                (ids,),
            ).fetchall()

    @app.get("/api/incidents/{incident_id}/original-report")
    def original_report(incident_id: int):
        with connect(dsn) as db:
            row = db.execute(
                """select incident_id, written_at, observed_through, report from report_snapshots
                   where incident_id = %s and phase = 'initial'""", (incident_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Original report pending.")
            return row

    @app.get("/api/catalog")
    def catalog():
        with connect(dsn) as db:
            rows = db.execute(
                """select m.id, m.direction, m.access->'cadence_minutes' as cadence_minutes,
                          latest.value, latest.details, latest.window_start
                   from metrics m left join lateral (
                       select value, details, window_start from observations o
                       where o.metric_id = m.id and o.scope = '{}' order by window_start desc limit 1
                   ) latest on true where m.id = any(%s)""",
                (list(CATALOG),),
            ).fetchall()
        registry = {row["id"]: row for row in rows}
        entries = []
        for identifier, definition in CATALOG.items():
            row = registry.get(identifier)
            entries.append({
                **definition, "legacy_ids": [old for old, current in LEGACY.items() if current == identifier],
                "registered": row is not None, "readings_available": bool(row and row["value"] is not None),
                "cadence_minutes": (row or {}).get("cadence_minutes") or definition["access"]["cadence_minutes"],
                "alerts_enabled": bool(row and row["direction"] != "none"), "latest": row,
            })
        return entries

    @app.get("/api/metric-insights/{metric_id}")
    def metric_insights(metric_id: str, scope: str = "{}", anchor: datetime | None = None, theme: str = "bright"):
        with connect(dsn) as db:
            metric = db.execute(f"select m.*, {DETECTION_OP} as op from metrics m where m.id=%s", (metric_id,)).fetchone()
            if not metric:
                raise HTTPException(404, "Metric is awaiting registration.")
            try:
                filters = json.loads(scope)
                if (not isinstance(filters, dict) or not set(filters).issubset(metric["dimensions"])
                        or not all(isinstance(value, str) for value in filters.values())):
                    raise ValueError
            except ValueError as exc:
                raise HTTPException(422, "Use the metric's declared dimensions for scope.") from exc
            return metric_history(db, metric, filters, utc(anchor) if anchor else None, theme)

    @app.post("/api/catalog/{metric_id}/register")
    def register_catalog_metric(metric_id: str, policy: MetricCadence | None = None):
        if metric_id not in CATALOG:
            raise HTTPException(404, "Unknown catalog metric.")
        return runtime.register(dsn, {**CATALOG[metric_id], **(policy.model_dump() if policy else {})}, model)

    @app.patch("/api/metrics/{metric_id}/cadence")
    def update_cadence(metric_id: str, policy: MetricCadence):
        if policy.cadence_minutes is None:
            raise HTTPException(422, "Choose a check cadence.")
        with connect(dsn) as db:
            row = db.execute("""update metrics set access=jsonb_set(access,'{cadence_minutes}',%s)
                                where id=%s returning id, access->'cadence_minutes' as cadence_minutes""",
                             (Jsonb(policy.cadence_minutes), metric_id)).fetchone()
            if not row:
                raise HTTPException(404, "Register the metric before editing its cadence.")
            return row

    @app.get("/api/catalog/{metric_id}/readings")
    def catalog_readings(metric_id: str, start: datetime | None = None, end: datetime | None = None,
                         as_of: datetime | None = None, scope: str = "{}",
                         bucket_minutes: int = Query(5, ge=1)):
        definition = CATALOG.get(metric_id)
        if not definition:
            raise HTTPException(404, "Unknown catalog metric.")
        try:
            filters = json.loads(scope)
            if not isinstance(filters, dict) or not set(filters).issubset(definition["dimensions"]):
                raise ValueError("Scope must use the metric's declared dimensions.")
            if not all(isinstance(value, str) for value in filters.values()):
                raise ValueError("Dimension filters must be strings.")
            with connect(dsn) as db:
                if not db.execute("select 1 from metrics where id = %s", (metric_id,)).fetchone():
                    raise HTTPException(409, "This metric is awaiting registration.")
                latest = db.execute("select * from windows order by start_at desc limit 1").fetchone()
                if not latest:
                    return {"metric_id": metric_id, "readings": []}
                start = utc(start) if start else latest["start_at"]
                end = utc(end) if end else latest["end_at"]
                available = latest["published_through"] or latest["observation_snapshot"] or latest["end_at"] + MAX_MATURITY
                as_of = utc(as_of) if as_of else available
                if latest["published_through"] is not None:
                    as_of = min(as_of, available)
                if start >= end or any(t.second or t.microsecond for t in (start, end)):
                    raise ValueError("Use a positive interval with minute-aligned boundaries.")
                if (definition["calculation"]["method"] == "budget" and
                    start.astimezone(BERLIN).date() != (end - timedelta(microseconds=1)).astimezone(BERLIN).date()):
                    raise ValueError("Daily budget utilization supports one Berlin business day per request.")
                results = []
                while start < end:
                    boundary = min(start + timedelta(minutes=bucket_minutes), end)
                    facts = Facts(db, start, boundary, as_of)
                    _, value, size, details = next(metric_readings(definition, facts, scope=filters))
                    results.append({"window_start": start, "scope": filters, "value": value,
                                    "sample_size": size, "details": details})
                    start = boundary
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"metric_id": metric_id, "readings": results}

    @app.post("/api/metrics")
    def register_metric(metric: MetricRegistration):
        try:
            return runtime.register(dsn, metric.model_dump(), model)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    app.mount("/", mcp_app, name="observation")
    return app


def utc(timestamp):
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)
