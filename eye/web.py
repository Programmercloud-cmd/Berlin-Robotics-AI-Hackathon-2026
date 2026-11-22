from contextlib import asynccontextmanager
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from eye import investigation, runtime
from eye.db import connect, state
from eye.metric_catalog import CATALOG, LEGACY
from eye.metric_readings import BERLIN, Facts, MAX_MATURITY, metric_readings
from eye.observation import make_mcp

STATIC = Path(__file__).with_name("static")


class MetricRegistration(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    definition: str
    calculation: dict
    dimensions: list[str] = Field(default_factory=list)
    access: dict = Field(default_factory=dict)
    direction: str = "both"


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
            return {
                "last_window": state(db, "last_window"),
                "baseline_through": state(db, "baseline_through"),
                "graph": {
                    "nodes": db.execute("select * from graph_nodes order by id").fetchall(),
                    "edges": edges,
                },
                "anomalies": db.execute(
                    "select * from anomalies order by window_start desc, score desc limit 250"
                ).fetchall(),
                "incidents": db.execute(
                    """select i.*, exists (
                           select 1 from pending_reports p where p.incident_id = i.id
                       ) as investigation_pending
                       from incidents i order by (i.id = %s) desc nulls last,
                           (recovered_at is null) desc, opened_at desc, id desc limit 50""",
                    (incident,),
                ).fetchall(),
            }

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
            if alerts_only:
                return db.execute(
                    """select kind, payload from evidence where incident_id = %s
                       and kind in ('anomaly', 'related anomaly', 'ongoing anomaly')
                       and payload ? 'id' order by window_start desc, id desc""",
                    (incident_id,),
                ).fetchall()
            return db.execute(
                """select * from evidence where incident_id = %s
                   order by (kind in ('branch evidence', 'first observable break', 'observed events', 'anomaly')) desc,
                            window_start desc, id desc limit 120""",
                (incident_id,),
            ).fetchall()

    @app.get("/api/catalog")
    def catalog():
        with connect(dsn) as db:
            rows = db.execute(
                """select m.id, m.direction, latest.value, latest.details, latest.window_start
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
                "alerts_enabled": bool(row and row["direction"] != "none"), "latest": row,
            })
        return entries

    @app.post("/api/catalog/{metric_id}/register")
    def register_catalog_metric(metric_id: str):
        if metric_id not in CATALOG:
            raise HTTPException(404, "Unknown catalog metric.")
        return runtime.register(dsn, CATALOG[metric_id], model)

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
