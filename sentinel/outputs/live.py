"""5b. Live mode: FastAPI server pushing the bus over Server-Sent Events to a single-page UI.

  GET /                 the agent page: architecture pipeline lighting up live, incidents, investigation, report
  GET /classic          the earlier dense dashboard (chain of thought, dynamic graph)
  GET /api/run          run status: simulated clock, finished, silent-mode e-mail configuration (masked)
  GET /api/stream       SSE: every bus event, replayed from ?since=<seq>
  GET /api/graph        current graph (nodes + edges with status), changes when the meta-agent mutates it
  GET /api/incidents    incidents with state, label, report
  GET /api/series       actual / expected / band for one monitored series
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from sentinel.bus import EventBus

PAGE = Path(__file__).with_name("ui.html")
CLASSIC = Path(__file__).with_name("live.html")


def create_app(bus: EventBus, runtime):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

    app = FastAPI(title="Sentinel live")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE.read_text(encoding="utf-8")

    @app.get("/workflow", response_class=HTMLResponse)
    def workflow_page():
        return Path(__file__).with_name("workflow.html").read_text(encoding="utf-8")

    @app.get("/api/events")
    def all_events():
        return JSONResponse([json.loads(e.to_json()) for e in bus.history])

    @app.get("/mail", response_class=HTMLResponse)
    def mail_page():
        return Path(__file__).with_name("mail.html").read_text(encoding="utf-8")

    @app.get("/api/outbox")
    def outbox():
        from sentinel.outputs.silent import email_configured, masked_recipients, read_outbox
        return JSONResponse({"mails": read_outbox(getattr(runtime, "outbox", "outbox")),
                             "email": {"configured": email_configured(), "to": masked_recipients()},
                             "finished": bool(runtime.finished)})

    @app.get("/classic", response_class=HTMLResponse)
    def classic():
        return CLASSIC.read_text(encoding="utf-8")

    @app.get("/api/run")
    def run_status():
        from sentinel.outputs.silent import email_configured, masked_recipients
        return JSONResponse({"sim_ts": bus.sim_ts, "finished": bool(runtime.finished), "events": len(bus.history),
                             "email": {"configured": email_configured(), "to": masked_recipients()}})

    @app.get("/api/stream")
    async def stream(since: int = 0):
        async def gen():
            i = since
            while True:
                evs = bus.history[i:]
                for e in evs:
                    yield f"id: {e.seq}\ndata: {e.to_json()}\n\n"
                i += len(evs)
                if runtime.finished and i >= len(bus.history):
                    yield "event: done\ndata: {}\n\n"
                    await asyncio.sleep(5)
                await asyncio.sleep(0.15)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/graph")
    def graph():
        g = runtime.graph
        nodes = [{"id": n.id, "name": n.name, "kind": n.kind, "unit": n.unit, "status": n.status,
                  "dimensions": n.dimensions, "owner": n.owner} for n in g.nodes.values()]
        edges = [{"src": e.src, "dst": e.dst, "type": e.type, "status": e.status, "sign": e.sign, "op": e.op,
                  "confidence": e.confidence, "reason": g.notes.get(k, {}).get("reason", "")}
                 for k, e in g.edges.items() if e.type in ("formula", "causal")]
        return JSONResponse({"nodes": nodes, "edges": edges, "roots": g.roots()})

    @app.get("/api/incidents")
    def incidents():
        out = []
        for inc in runtime.incidents.incidents.values():
            out.append(json.loads(inc.model_dump_json()))
        return JSONResponse(out)

    @app.get("/api/series")
    def series(metric: str, segment: str = ""):
        rows = runtime.monitor.series.get((metric, segment), [])
        return JSONResponse(rows[-72:])

    return app


def serve(bus: EventBus, runtime, port: int = 8765):
    import uvicorn
    app = create_app(bus, runtime)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return server
