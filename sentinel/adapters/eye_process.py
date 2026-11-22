"""Agent-side metrics computed from Eye's OCEL events with PM4Py, stored in sentinel.observations.

Two families, both stored as numerator / denominator so every slice re-aggregates exactly:

  event ratios      mined by the meta-agent from activities no metric measures yet (step 4), e.g.
                    product_page_load_failed / product_page_requested
  process metrics   (step 5)
    conformance     a reference Petri net is discovered from history with PM4Py's inductive miner per
                    object lifecycle (payment, session); each hour's traces are replayed against it
                    (token-based replay) -> average trace fitness per segment
    performance     PM4Py-flattened payment lifecycle: seconds from payment_attempted to its outcome
                    -> mean duration per segment

Nothing here writes to Eye's tables; Eye's simulator and metric graph never read these rows.
"""
from __future__ import annotations

import warnings
from collections import defaultdict

import pandas as pd

from .eye import _aware, _naive

LIFECYCLES = {
    "payment": {"dims": ["country", "platform", "payment_provider"],
                "outcomes": ("payment_succeeded", "payment_timed_out", "payment_declined")},
    "session": {"dims": ["country", "platform", "acquisition_channel"]},
}


def scopes_for(attrs: dict, dims: list[str]) -> list[tuple]:
    fields = [(d, str(attrs[d])) for d in dims if attrs.get(d) is not None]
    out = [()] + [(f,) for f in fields]
    if len(fields) == len(dims) and len(dims) > 1:
        out.append(tuple(sorted(fields)))
    return out


def process_metric_defs() -> list[dict]:
    pay, ses = LIFECYCLES["payment"]["dims"], LIFECYCLES["session"]["dims"]
    return [
        {"id": "payment_process_fitness", "name": "Payment process conformance",
         "definition": "Average token-replay fitness of hourly payment lifecycles against the Petri net "
                       "discovered from history (PM4Py inductive miner).",
         "calculation": {"op": "process_fitness", "object_type": "payment"}, "dimensions": pay,
         "access": {"unit": "ratio", "source": "events"}, "direction": "down",
         "provenance": "pm4py.conformance", "kind": "ratio"},
        {"id": "session_process_fitness", "name": "Session process conformance",
         "definition": "Average token-replay fitness of hourly session lifecycles against the discovered model.",
         "calculation": {"op": "process_fitness", "object_type": "session"}, "dimensions": ses,
         "access": {"unit": "ratio", "source": "events"}, "direction": "down",
         "provenance": "pm4py.conformance", "kind": "ratio"},
        {"id": "payment_outcome_seconds", "name": "Payment outcome time",
         "definition": "Mean seconds from payment_attempted to its outcome in the payment lifecycle "
                       "(PM4Py performance view).",
         "calculation": {"op": "process_duration", "object_type": "payment", "from": "payment_attempted",
                         "to": list(LIFECYCLES["payment"]["outcomes"])}, "dimensions": pay,
         "access": {"unit": "seconds per attempt", "source": "events"}, "direction": "up",
         "provenance": "pm4py.performance", "kind": "ratio"},
    ]


class AgentMetricsEngine:
    def __init__(self, data, store):
        self.data, self.store, self.db = data, store, data.db
        self.models: dict[str, tuple] = {}

    # ------------------------------------------------------------------ OCEL access
    def lifecycle_log(self, object_type: str, windows: list) -> pd.DataFrame:
        """Flattened lifecycle log (one case per object) with the case's segment attributes."""
        rows = self.db.execute(
            """select o.id as case_id, e.activity, e.occurred_at, e.window_start, e.attrs
               from events e join event_objects eo on eo.event_id = e.id join objects o on o.id = eo.object_id
               where o.type = %s and e.window_start = any(%s) order by o.id, e.occurred_at, e.id""",
            (object_type, [_aware(w) for w in windows])).fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).drop_duplicates(["case_id", "activity", "occurred_at"])
        df["occurred_at"] = pd.to_datetime(df.occurred_at, utc=True)
        return df.rename(columns={"case_id": "case:concept:name", "activity": "concept:name",
                                  "occurred_at": "time:timestamp"})

    def fit_models(self, history_windows: list):
        import pm4py
        warnings.filterwarnings("ignore")
        for ot in LIFECYCLES:
            log = self.lifecycle_log(ot, history_windows)
            if log.empty:
                continue
            net, im, fm = pm4py.discover_petri_net_inductive(
                log[["case:concept:name", "concept:name", "time:timestamp"]], noise_threshold=0.2)
            self.models[ot] = (net, im, fm)

    # ------------------------------------------------------------------ computation
    def compute(self, metric: dict, windows: list) -> list[tuple]:
        op = metric["calculation"]["op"]
        if op == "event_ratio":
            return self._event_ratio(metric, windows)
        if op == "process_fitness":
            return self._fitness(metric, windows)
        if op == "process_duration":
            return self._duration(metric, windows)
        return []

    def _emit(self, metric, groups) -> list[tuple]:
        out = []
        for (ws, scope), (num, den) in groups.items():
            out.append((metric["id"], _aware(ws), dict(scope), (num / den) if den else None, int(den),
                        {"numerator": num, "denominator": den, "quality": "complete" if den else "zero_denominator"}))
        return out

    def _event_ratio(self, metric, windows) -> list[tuple]:
        c = metric["calculation"]
        rows = self.db.execute(
            """select window_start, activity, attrs from events where activity in (%s, %s) and window_start = any(%s)""",
            (c["activity"], c["denominator_activity"], [_aware(w) for w in windows])).fetchall()
        groups = defaultdict(lambda: [0.0, 0.0])
        for w in windows:
            groups[(_naive(w), ())]          # keep empty windows visible as zero counts
        for r in rows:
            for s in scopes_for(r["attrs"], metric["dimensions"]):
                g = groups[(_naive(r["window_start"]), s)]
                if r["activity"] == c["activity"]:
                    g[0] += 1
                if r["activity"] == c["denominator_activity"]:
                    g[1] += 1
        return self._emit(metric, groups)

    def _traces(self, ot: str, windows: list):
        log = self.lifecycle_log(ot, windows)
        if log.empty:
            return log, pd.DataFrame()
        first = log.groupby("case:concept:name").agg(window_start=("window_start", "first"),
                                                       attrs=("attrs", "first")).reset_index()
        return log, first

    def _fitness(self, metric, windows) -> list[tuple]:
        import pm4py
        from pm4py.algo.conformance.tokenreplay import algorithm as token_replay
        ot = metric["calculation"]["object_type"]
        if ot not in self.models:
            return []
        net, im, fm = self.models[ot]
        log, cases = self._traces(ot, windows)
        if log.empty:
            return []
        event_log = pm4py.convert_to_event_log(log[["case:concept:name", "concept:name", "time:timestamp"]])
        res = token_replay.apply(event_log, net, im, fm, parameters={"show_progress_bar": False})
        fit = {t.attributes["concept:name"]: r["trace_fitness"] for t, r in zip(event_log, res)}
        groups = defaultdict(lambda: [0.0, 0.0])
        for row in cases.itertuples():
            f = fit.get(row[1])
            if f is None:
                continue
            for s in scopes_for(row.attrs, metric["dimensions"]):
                g = groups[(_naive(row.window_start), s)]
                g[0] += f
                g[1] += 1
        return self._emit(metric, groups)

    def _duration(self, metric, windows) -> list[tuple]:
        c = metric["calculation"]
        log, cases = self._traces(c["object_type"], windows)
        if log.empty:
            return []
        start = log[log["concept:name"] == c["from"]].groupby("case:concept:name")["time:timestamp"].min()
        end = log[log["concept:name"].isin(c["to"])].groupby("case:concept:name")["time:timestamp"].min()
        dur = (end - start).dt.total_seconds().dropna()
        groups = defaultdict(lambda: [0.0, 0.0])
        for row in cases.itertuples():
            d = dur.get(row[1])
            if d is None or d < 0:
                continue
            for s in scopes_for(row.attrs, metric["dimensions"]):
                g = groups[(_naive(row.window_start), s)]
                g[0] += d
                g[1] += 1
        return self._emit(metric, groups)

    # ------------------------------------------------------------------ bookkeeping
    def windows(self, start=None, end=None) -> list:
        q, a = "select start_at from windows", []
        if start is not None:
            q += " where start_at >= %s and start_at < %s"
            a = [_aware(start), _aware(end)]
        return [_naive(r["start_at"]) for r in self.db.execute(q + " order by start_at", a)]

    def backfill(self, metric: dict, windows: list | None = None):
        self.store.write_observations(self.compute(metric, windows or self.windows()))
        self.data.forget(metric["id"])

    def update_window(self, window_start):
        for m in self.store.metrics():
            if m["status"] != "rejected":
                self.store.write_observations(self.compute(m, [window_start]))
                self.data.forget(m["id"])


def add_process_metrics(graph, data, store, engine, bus, history_start, as_of) -> list[str]:
    """Step 5: discover reference models from history (PM4Py), register conformance and performance
    metrics in sentinel.metrics, backfill them, add them to the agent's graph and propose causal links
    from the process structure (validated on history)."""
    from sentinel.brain.validate import check_causal
    from sentinel.contracts import Edge
    from .eye import node_from_catalog
    hist = engine.windows(history_start, as_of)
    engine.fit_models(hist)
    bus.publish("META_PROPOSAL", "pm4py", {"stage": "process_models", "text":
        f"PM4Py inductive miner: reference Petri nets for {', '.join(engine.models)} lifecycles from "
        f"{len(hist)} historical hours"})
    added = []
    for spec in process_metric_defs():
        if spec["calculation"].get("object_type") not in engine.models and spec["calculation"]["op"] == "process_fitness":
            continue
        spec["status"] = "validated"
        store.register_metric(spec)
        data.add_agent_metric(spec)
        engine.backfill(spec, hist)
        node = node_from_catalog(spec)
        node.owner, node.status = spec["provenance"], "validated"
        graph.add_node(node, actor=spec["provenance"])
        for e in graph.edges_where("breakdown", src=spec["id"], usable=False):
            graph.set_status(e, "validated", "segment attributes of the lifecycle's events", actor="pm4py")
        added.append(spec["id"])
        bus.publish("GRAPH_MUTATION", spec["provenance"], {"kind": "process_metric", "metric": spec["id"],
                    "summary": f"added {spec['id']}: {spec['definition']}"})
    links = [("payment_outcome_seconds", "payment_timeouts_share", "+",
              "timed-out payments take longer to reach an outcome"),
             ("payment_process_fitness", "payment_success_rate", "+",
              "payment lifecycles that deviate from the discovered process fail more often")]
    for src, dst, sign, why in links:
        if src in graph.nodes and dst in graph.nodes:
            e = Edge(type="causal", src=src, dst=dst, sign=sign, lag_h=(0, 1), prior=0.4, confidence=0.4,
                     status="proposed")
            graph.add_edge(e, actor="pm4py")
            verdict, reason, st, conf = check_causal(graph, data, e, pd.Timestamp(history_start), pd.Timestamp(as_of))
            status = {"supported": "validated", "contradicted": "needs_review"}.get(verdict, "proposed")
            graph.set_status(e, status, f"{why}; {reason}", st, confidence=conf, actor="pm4py")
            bus.publish("GRAPH_MUTATION", "pm4py", {"kind": "causal", "edge": f"{src} -({sign})-> {dst}",
                        "summary": f"proposed {src} -({sign})-> {dst}: {verdict}"})
    return added
