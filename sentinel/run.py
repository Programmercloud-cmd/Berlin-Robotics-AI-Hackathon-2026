"""Run the whole agent end to end on the simulated marketplace.

  python -m sentinel.run                          silent mode: reports go to outbox/ (and Slack/e-mail if configured)
  python -m sentinel.run --mode live              live mode: open http://127.0.0.1:8765 and watch it think
  python -m sentinel.run --until 2026-10-27T18:00 --speed 1.5

Pipeline per simulated hour:  Data.step -> Monitor.tick -> IncidentManager.handle (-> Investigator ->
Meta-Agent on dead ends) -> follow_up; everything is published on the Event Bus.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from pathlib import Path

import pandas as pd

LIVE_START = pd.Timestamp("2026-10-26")
CONTRIBUTOR_METRIC = {   # submitted live during the run: no dimensions, no graph placement
    "id": "payment_cost_rate", "name": "Payment cost rate", "table": "fact_commerce",
    "description": "PSP costs as a share of GMV", "numerator_sql": "SUM(payment_costs_eur)",
    "denominator_sql": "SUM(gmv_eur)", "unit": "share", "owner": "payments"}


class Runtime:
    finished = False

    def __init__(self, args):
        from dev.fake_data import FakeData
        from sentinel.brain.graph import BrainGraph
        from sentinel.brain.validate import validate_graph
        from sentinel.bus import EventBus
        from sentinel.investigator.incidents import IncidentManager
        from sentinel.investigator.react import Investigator
        from sentinel.meta.meta_agent import MetaAgent
        from sentinel.monitor.engine import MonitorEngine

        self.bus = EventBus(getattr(args, "events_path", "state/events.jsonl"))
        self.bus.sim_ts = str(LIVE_START)
        self.bus.publish("SYSTEM", "runtime", {"message": "starting: building the metric graph from the seed"})
        self.graph = BrainGraph.from_seed()
        source = FakeData
        if getattr(args, "data", "fake") == "shoply":     # Chaimaa's Shoply simulator (branch chaimaa)
            from sentinel.adapters.shoply import ShoplyData as source
        self.data = source(list(self.graph.nodes.values()), self.graph.formulas,
                           scenarios=getattr(args, "scenarios", None), seed=getattr(args, "seed", 42),
                           overrides=getattr(args, "overrides", None))
        self.data.now = LIVE_START
        summary = validate_graph(self.graph, self.data, LIVE_START)
        self.bus.publish("GRAPH_LOADED", "graph", {"metrics": len(self.graph.nodes), "edges": len(self.graph.edges),
                                                   "verdicts": summary.counts(),
                                                   "needs_review": [k for k, e in self.graph.edges.items()
                                                                    if e.status == "needs_review"]})
        self.monitor = MonitorEngine(self.graph, self.data, self.bus, LIVE_START - pd.Timedelta(days=56), LIVE_START,
                                     features=getattr(args, "features", None))
        self.monitor.fit()
        if self.monitor.features.get("calibrate", True):      # 0. study the data before trusting defaults
            from sentinel.calibrate import DataProfile
            self.profile = DataProfile(self.graph, self.data, self.monitor, self.bus).run()
        self.meta = None if args.no_meta else MetaAgent(self.graph, self.data, self.monitor, self.bus)
        self.investigator = Investigator(self.graph, self.data, self.monitor, self.bus, meta=self.meta)
        self.incidents = IncidentManager(self.graph, self.monitor, self.investigator, self.bus)

    def run(self, until: pd.Timestamp, speed: float, add_metric_at: pd.Timestamp | None):
        now = LIVE_START
        while now < until:
            now += pd.Timedelta(hours=1)
            self.data.now = now
            self.bus.sim_ts = str(now)
            if add_metric_at is not None and now == add_metric_at and self.meta is not None:
                self.meta.register(CONTRIBUTOR_METRIC, now)
            anomalies = self.monitor.tick(now)
            self.incidents.handle(anomalies, now)
            self.incidents.follow_up(now)
            if speed:
                time.sleep(speed)
        self.finished = True
        self.bus.publish("SYSTEM", "runtime", {"message": f"run finished at {now}"})


def main(argv=None):
    warnings.filterwarnings("ignore")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    p = argparse.ArgumentParser(prog="sentinel.run")
    p.add_argument("--mode", choices=["silent", "live"], default="silent")
    p.add_argument("--until", default="2026-10-27T18:00")
    p.add_argument("--speed", type=float, default=None, help="wall-clock seconds per simulated hour")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--add-metric-at", default="2026-10-26T23:00", help="when a contributor submits payment_cost_rate")
    p.add_argument("--no-meta", action="store_true")
    p.add_argument("--demo", action="store_true",
                   help="short demo: day one until 19:00 (campaign, late data, payment fault, red-herring release), "
                        "about 3 minutes")
    p.add_argument("--email-to", default=None, help="digest recipient(s), comma-separated (overrides ALERT_EMAIL_TO)")
    p.add_argument("--email-each", action="store_true", help="also mail every alert when it happens")
    p.add_argument("--data", choices=["fake", "shoply"], default="fake",
                   help="fake: dev/fake_data.py; shoply: Chaimaa's Shoply simulator (.shoply-chaimaa checkout)")
    a = p.parse_args(argv)
    if a.demo:                      # the short story: everything that matters on day one happens before 19:00
        a.until, a.add_metric_at = "2026-10-26T19:00", None
        if a.speed is None:
            a.speed = 0.3 if a.mode == "live" else 0.0
    speed = a.speed if a.speed is not None else (1.5 if a.mode == "live" else 0.0)
    if a.email_to:
        os.environ["ALERT_EMAIL_TO"] = a.email_to

    t0 = time.time()
    print("building graph, validating it on history and fitting baselines …")
    rt = Runtime(a)
    from sentinel.outputs.silent import SilentNotifier
    notifier = SilentNotifier(rt.bus, echo=True, email_each=a.email_each)
    server = None
    if a.mode == "live":
        from sentinel.outputs.live import serve
        server = serve(rt.bus, rt, a.port)
        print(f"live UI: http://127.0.0.1:{a.port}   (replays from the start, then streams)")
        time.sleep(2)
    print(f"ready in {time.time() - t0:.0f}s; running {LIVE_START} → {a.until}")
    rt.run(pd.Timestamp(a.until), speed, pd.Timestamp(a.add_metric_at) if a.add_metric_at else None)

    notifier.finish(rt, str(LIVE_START), str(pd.Timestamp(a.until)))     # silent mode: the digest e-mail
    incs = rt.incidents.incidents.values()
    print(f"\n{len(incs)} incidents, {len(notifier.delivered)} reports delivered, run took {time.time() - t0:.0f}s")
    for i in incs:
        print(f"  {i.id} {i.state:10} {str(i.label):16} first cause {i.first_cause} [{i.segment or 'total'}] "
              f"impact {i.impact_eur_total:+,.0f} EUR")
    Path("state").mkdir(exist_ok=True)
    Path("state/incidents.json").write_text(json.dumps([json.loads(i.model_dump_json()) for i in incs], indent=1),
                                            encoding="utf-8")
    rt.graph.save("state/brain_graph.json")
    if server is not None:
        print("run finished; the live UI stays up — press Ctrl+C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
