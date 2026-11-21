from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Thread

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from eye import runtime
from eye.db import connect, state
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


def make_app(dsn, model=None, demo=None):
    mcp = make_mcp(dsn)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        stop = Event()
        worker = Thread(target=runtime.watch, args=(dsn, stop, model), daemon=True)
        worker.start()
        if demo:
            Thread(target=demo, args=(stop,), daemon=True).start()
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
    def snapshot():
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
                    "select * from incidents order by recovered_at nulls first, opened_at desc limit 50"
                ).fetchall(),
            }

    @app.get("/api/incidents/{incident_id}/evidence")
    def evidence(incident_id: int):
        with connect(dsn) as db:
            return db.execute(
                """select * from evidence where incident_id = %s
                   order by window_start desc, id desc limit 120""",
                (incident_id,),
            ).fetchall()

    @app.post("/api/metrics")
    def register_metric(metric: MetricRegistration):
        try:
            return runtime.register(dsn, metric.model_dump(), model)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    app.mount("/", mcp_app, name="observation")
    return app
