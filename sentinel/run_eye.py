"""Run the Sentinel agent on the Eye of the System marketplace (jacob/v1: PostgreSQL + OCEL + PM4Py).

  python -m sentinel.run_eye                       silent run of Eye's rehearsal timeline
  python -m sentinel.run_eye --mode live           live UI on http://127.0.0.1:8765
  python -m sentinel.run_eye --history-days 14     history generated once into state/eye_pg

Timeline (Eye's own rehearsal, extended): 2 normal hours, 2 hours of +65% traffic (unannounced),
5 hours of the DE / mobile / Stripe payment fault, 6 healthy hours, 2 hours of the FR / web search
disruption, 4 healthy hours. Eye's simulator produces the events; Sentinel only reads observations.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import warnings
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EYE = ROOT / ".eye-jacob"

TIMELINE = ([{"label": "normal"}] * 2 + [{"label": "traffic +65%", "traffic": 1.65}] * 2
            + [{"label": "payment fault", "fault": True}] * 5 + [{"label": "normal"}] * 6
            + [{"label": "search disruption", "search_disruption": True}] * 2 + [{"label": "normal"}] * 4)


def start_database(pg_dir: Path, eye_path: Path, history_days: int, log):
    import pgserver
    from sentinel.adapters.eye import import_eye
    import_eye(eye_path)
    from eye import runtime, simulator
    from eye.db import connect, setup
    pg_dir.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(pg_dir))
    dsn = server.get_uri()
    with connect(dsn) as db:
        setup(db)
        populated = db.execute("select exists (select 1 from windows) as p").fetchone()["p"]
        if not populated:
            log(f"generating {history_days} days of OCEL history with Eye's simulator (one-time) …")
            simulator.seed(db, days=history_days)
    log("Eye initialisation: registering catalog metrics, PM4Py discovery, materialising observations …")
    runtime.initialize(dsn)
    return server, dsn


class EyeRuntime:
    finished = False

    def __init__(self, dsn: str, eye_path: Path, no_meta: bool = False, events_path: str = "state/events_eye.jsonl",
                 features: dict | None = None):
        from sentinel.adapters.eye import EyeData, build_graph
        from sentinel.brain.validate import validate_graph
        from sentinel.bus import EventBus
        from sentinel.investigator.incidents import IncidentManager
        from sentinel.investigator.react import Investigator
        from sentinel.meta.meta_agent import MetaAgent
        from sentinel.monitor.engine import MonitorEngine

        self.data = EyeData(dsn, eye_path)
        live_start = self.data.now
        self.bus = EventBus(events_path)
        self.bus.sim_ts = str(live_start)
        self.bus.publish("SYSTEM", "runtime", {"message": "Sentinel on Eye: building the metric graph from Eye's catalog"})
        self.graph = build_graph(self.data)
        try:
            from sentinel.adapters.eye_formulas import add_eye_formulas
            add_eye_formulas(self.graph, self.data, self.bus)
        except ImportError:
            pass
        first = pd.Timestamp(self.data.db.execute("select min(start_at) as t from windows").fetchone()["t"])
        first = first.tz_convert("UTC").tz_localize(None)
        summary = validate_graph(self.graph, self.data, live_start, formula_days=3,
                                 causal_days=max(3, (live_start - first).days - 1))
        self.bus.publish("GRAPH_LOADED", "graph", {"metrics": len(self.graph.nodes), "edges": len(self.graph.edges),
                                                   "verdicts": summary.counts()})
        # agent-only tables (schema sentinel) and agent-side metrics (PM4Py conformance / performance)
        from sentinel.adapters.eye_process import AgentMetricsEngine, add_process_metrics
        from sentinel.adapters.eye_store import AgentStore
        self.store = AgentStore(self.data.db)
        self.engine = AgentMetricsEngine(self.data, self.store)
        add_process_metrics(self.graph, self.data, self.store, self.engine, self.bus, first, live_start)
        self.monitor = MonitorEngine(self.graph, self.data, self.bus, first, live_start, features=features)
        self.monitor.fit()
        if self.monitor.features.get("calibrate", True):      # 0. study the data before trusting defaults
            from sentinel.calibrate import DataProfile
            self.profile = DataProfile(self.graph, self.data, self.monitor, self.bus).run()
        self.meta = None if no_meta else MetaAgent(self.graph, self.data, self.monitor, self.bus)
        if self.meta is not None:   # step 4: activities of the process map that no metric measures yet
            self.meta.mine_unmeasured_activities(self.engine, self.store, live_start, first)
        self.investigator = Investigator(self.graph, self.data, self.monitor, self.bus, meta=self.meta)
        self.incidents = IncidentManager(self.graph, self.monitor, self.investigator, self.bus)
        self.store.attach(self.bus, self.incidents)
        self.live_start = live_start

    def run(self, timeline, speed: float, log):
        clock = self.live_start
        for step in timeline:
            kwargs = {k: v for k, v in step.items() if k != "label"}
            self.data.emit_and_observe(clock, **kwargs)
            self.engine.update_window(clock)          # agent-side metrics for the new hour (sentinel schema)
            now = clock + timedelta(hours=1)
            self.bus.sim_ts = str(now)
            self.bus.publish("SYSTEM", "operator", {"message": f"world: {step['label']} in window {clock:%m-%d %H:%M}"})
            anomalies = self.monitor.tick(now)
            self.incidents.handle(anomalies, now)
            self.incidents.follow_up(now)
            log(f"{clock:%m-%d %H:%M} {step['label']:18} anomalies={len(anomalies):3d} "
                f"incidents={len(self.incidents.incidents)}")
            clock = now
            if speed:
                time.sleep(speed)
        self.finished = True
        self.bus.publish("SYSTEM", "runtime", {"message": f"run finished at {clock}"})


def main(argv=None):
    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    p = argparse.ArgumentParser(prog="sentinel.run_eye")
    p.add_argument("--mode", choices=["silent", "live"], default="silent")
    p.add_argument("--eye-path", default=str(DEFAULT_EYE))
    p.add_argument("--pg-dir", default=str(ROOT / "state" / "eye_pg"))
    p.add_argument("--history-days", type=int, default=14)
    p.add_argument("--speed", type=float, default=None)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-meta", action="store_true")
    p.add_argument("--hours", type=int, default=len(TIMELINE))
    a = p.parse_args(argv)
    t0 = time.time()
    log = lambda m: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True)
    server, dsn = start_database(Path(a.pg_dir), Path(a.eye_path), a.history_days, log)
    log("building Sentinel on Eye's data …")
    rt = EyeRuntime(dsn, Path(a.eye_path), a.no_meta)
    from sentinel.outputs.silent import SilentNotifier
    notifier = SilentNotifier(rt.bus, outbox="outbox_eye")
    srv = None
    if a.mode == "live":
        from sentinel.outputs.live import serve
        srv = serve(rt.bus, rt, a.port)
        log(f"live UI: http://127.0.0.1:{a.port}")
    log(f"live from {rt.live_start}")
    rt.run(TIMELINE[:a.hours], a.speed if a.speed is not None else (1.5 if a.mode == "live" else 0.0), log)
    for i in rt.incidents.incidents.values():
        print(f"  {i.id} {i.state:10} {str(i.label):16} {i.first_cause} [{i.segment or 'total'}] "
              f"impact {i.impact_eur_total:+,.0f} EUR")
    print(f"{len(notifier.delivered)} reports delivered; {time.time() - t0:.0f}s")
    Path("state").mkdir(exist_ok=True)
    Path("state/incidents_eye.json").write_text(json.dumps([json.loads(i.model_dump_json())
                                                            for i in rt.incidents.incidents.values()], indent=1))
    if srv is not None:
        print("live UI stays up — Ctrl+C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
