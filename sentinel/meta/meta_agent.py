"""4. Meta-Agent — creativity: extend the graph when an investigation hits a dead end, or when a
contributor submits a new metric.

explore(focus, segment, window, reasons)        (triggered by DEAD_END)
  1. read the data dictionary (Data.schema())
  2. cross dimensions: a dimension the source table has but the metric does not expose
     (e.g. feed metrics are stored per channel but registered without `channel`)
  3. derived metrics: measure columns no metric uses yet, turned into a share of their closest parent
     column (feed_oos_impressions / feed_impressions)
  4. sandbox: compute every candidate on history under a temporary id: no errors, sane values,
     slices reconcile with the total; then test whether it explains the current incident
  5. graph mutation: only candidates that pass are written into the graph (GRAPH_MUTATION), with
     validated / proposed status and the evidence behind it

register(spec)                                   (a contributor adds a metric, no placement given)
  infers kind, unit and dimensions, rejects duplicates, discovers formulas by matching SQL to
  existing metrics and checking the identity numerically (payment_costs = gmv x payment_cost_rate)
"""
from __future__ import annotations

import re
from datetime import timedelta

import numpy as np
import pandas as pd
from scipy import stats

from sentinel.brain.validate import FORMULA_TOL, check_breakdown, check_formula
from sentinel.bus import EventBus
from typing import get_args

from sentinel.contracts import Dim, Edge, MetricNode, seg_key


ALLOWED_DIMS = set(get_args(Dim))   # empty when Dim is an open vocabulary: any data dimension is allowed


def _cols(sql: str | None) -> set[str]:
    return set(re.findall(r"\b[a-z_][a-z0-9_]*\b", re.sub(r"\bfact_[a-z]+\.", "", (sql or "").lower()))) - {"sum"}


class MetaAgent:
    def __init__(self, graph, data, monitor, bus: EventBus):
        self.g, self.data, self.mon, self.bus = graph, data, monitor, bus
        self.tried: set[str] = set()

    # ================================================================ dead-end exploration
    def explore(self, focus: str, seg: dict, start, end, reasons: list[str], incident_id: str | None = None) -> list[dict]:
        schema = self.data.schema()
        self.bus.publish("META_PROPOSAL", "meta", {"stage": "read_dictionary", "text":
            f"Dead end on {focus} [{seg_key(seg) or 'total'}]: {'; '.join(reasons)}. Reading the data dictionary "
            f"({len(schema)} tables) for unused dimensions and columns."}, incident_id)
        wanted = sorted({d for r in reasons for d in re.findall(r"'([a-z_]+)'", r.split("no dimension")[-1])
                         if "no dimension" in r and (not ALLOWED_DIMS or d in ALLOWED_DIMS)})
        applied = []
        applied += self._cross_dimensions(focus, schema, start, end, incident_id, wanted)
        applied += self._derived_metrics(focus, seg, schema, start, end, incident_id)
        if not applied:
            self.bus.publish("META_RESULT", "meta", {"text": "no candidate passed the sandbox; the graph is unchanged"},
                             incident_id)
        return applied

    def _cross_dimensions(self, focus: str, schema: dict, start, end, incident_id, wanted: list[str]) -> list[dict]:
        """Only the focus metric and its siblings (same table, same owning service) are widened, and only by the
        dimension the dead end asked for when it named one."""
        out = []
        fnode = self.g.nodes[focus]
        if not fnode.table:
            return out
        family = {focus} | {n.id for n in self.g.nodes.values() if n.table == fnode.table
                            and set(n.services) & set(fnode.services)}
        table = fnode.table
        if True:
            for dim in schema.get(table, {}).get("dimensions", []):
                if (ALLOWED_DIMS and dim not in ALLOWED_DIMS) or (wanted and dim not in wanted):
                    continue
                nodes = [n for n in self.g.nodes.values() if n.id in family and dim not in n.dimensions
                         and set(self.g.tables_of(n.id)) == {table}]
                key = f"dim:{table}:{dim}"
                if not nodes or key in self.tried:
                    continue
                self.tried.add(key)
                ids = [n.id for n in nodes]
                self.bus.publish("META_PROPOSAL", "meta", {"stage": "cross_dimension", "text":
                    f"{table} stores '{dim}' but {', '.join(ids)} do not expose it. Sandbox-test adding it."},
                    incident_id)
                ok, failed = [], []
                for n in nodes:
                    cand = n.model_copy(update={"dimensions": list(n.dimensions) + [dim]})
                    self.data.register_node(cand)            # sandbox: same id, wider dims; reverted on failure
                    try:
                        status, reason, st = check_breakdown(self.g_with(cand), self.data, n.id, dim, start - timedelta(days=7), start)
                    except Exception as ex:                # noqa: BLE001 - sandbox must never crash the agent
                        status, reason = "rejected", str(ex)[:120]
                    if status == "validated":
                        ok.append(cand)
                    else:
                        failed.append(f"{n.id}: {reason}")
                        self.data.register_node(n)
                for cand in ok:
                    self.g.add_node(cand, actor="meta-agent")
                    for e in self.g.edges_where("breakdown", src=cand.id, usable=False):
                        if e.dimension == dim:
                            self.g.set_status(e, "validated", f"added by meta-agent: slices by {dim} reconcile with the total",
                                              actor="meta-agent")
                if ok:
                    m = {"kind": "cross_dimension", "dimension": dim, "metrics": [c.id for c in ok],
                         "summary": f"added dimension '{dim}' to {', '.join(c.id for c in ok)} (slices reconcile)"}
                    self.bus.publish("GRAPH_MUTATION", "meta", {**m, "failed": failed}, incident_id)
                    out.append(m)
        return out

    def _derived_metrics(self, focus: str, seg: dict, schema: dict, start, end, incident_id) -> list[dict]:
        out = []
        used = set()
        for n in self.g.nodes.values():
            for s in (n.value_sql, n.numerator_sql, n.denominator_sql):
                used |= _cols(s)
        tables = [self.g.nodes[focus].table] if self.g.nodes[focus].table else []
        for table in tables:
            for col in schema.get(table, {}).get("measures", []):
                key = f"col:{col}:{incident_id}"
                if col in used or key in self.tried:
                    continue
                self.tried.add(key)
                toks = set(col.split("_"))
                parents = sorted((c for c in schema[table]["measures"] if c != col and c in used),
                                 key=lambda c: -len(toks & set(c.split("_"))))
                if not parents or not toks & set(parents[0].split("_")):
                    continue
                parent = parents[0]
                pnode = next((n for n in self.g.nodes.values() if n.value_sql and _cols(n.value_sql) == {parent}), None)
                extra = "_".join(t for t in col.split("_") if t not in parent.split("_"))
                new_id = f"{parent.split('_')[0]}_{extra}_share" if extra else f"{col}_share"
                cand = MetricNode(id=new_id, name=f"{col.replace('_', ' ')} share of {parent.replace('_', ' ')}",
                                  description=f"Derived by meta-agent: SUM({col}) / SUM({parent}) from {table}",
                                  kind="ratio", unit="share", table=table, numerator_sql=f"SUM({col})",
                                  denominator_sql=f"SUM({parent})",
                                  dimensions=list(pnode.dimensions) if pnode else [d for d in schema[table]["dimensions"]],
                                  services=list(pnode.services) if pnode else [], owner="meta-agent", status="proposed")
                self.bus.publish("META_PROPOSAL", "meta", {"stage": "derived_metric", "text":
                    f"Column {table}.{col} is not used by any metric. Candidate {new_id} = SUM({col}) / SUM({parent})."},
                    incident_id)
                res = self._sandbox_and_test(cand, focus, seg, start, end, incident_id)
                if res:
                    out.append(res)
        return out

    def _sandbox_and_test(self, cand: MetricNode, focus: str, seg: dict, start, end, incident_id) -> dict | None:
        self.data.register_node(cand)
        hist = self.data.metric(cand.id, start - timedelta(days=14), start)
        v = hist.value.dropna()
        if v.empty or not v.between(0, 1).all() or v.std() == 0:
            self.bus.publish("META_RESULT", "meta", {"text": f"{cand.id} rejected in sandbox: values not a valid share"},
                             incident_id)
            return None
        cseg = {k: x for k, x in seg.items() if k in cand.dimensions}
        from .._sandbox import baseline_for
        w = baseline_for(cand, self.data, self.mon, cseg).window(self.data.metric(cand.id, start, end, filters=cseg))
        # does its DEVIATION from normal move with the focus metric's deviation (incident window + 2 days before)?
        # Raw values would mostly correlate through the shared daily cycle.
        s0 = start - timedelta(days=2)
        cb = baseline_for(cand, self.data, self.mon, cseg)
        ca = self.data.metric(cand.id, s0, end, filters=cseg).dropna(subset=["value"])
        a = pd.Series([cb.score(r.ts, r.value, r.denominator)["z"] for r in ca.itertuples()], index=ca.ts)
        fseg = {k: x for k, x in seg.items() if k in self.g.nodes[focus].dimensions}
        fb = self.mon.baseline(focus, fseg)
        fa = self.data.metric(focus, s0, end, filters=fseg).dropna(subset=["value"])
        b = pd.Series([fb.score(r.ts, r.value, r.denominator)["z"] for r in fa.itertuples()], index=fa.ts)
        al = pd.concat([a, b], axis=1, join="inner").dropna()
        al = al.clip(-10, 10)   # co-movement of the deviations; clipping keeps one huge hour from dominating
        rho, p = stats.pearsonr(al.iloc[:, 0], al.iloc[:, 1]) if len(al) > 10 and al.std().min() > 0 else (0.0, 1.0)
        moved = abs(w["z"]) >= 4
        self.bus.publish("META_RESULT", "meta", {"text":
            f"sandbox ok: {cand.id} in [{seg_key(cseg) or 'total'}] {w['actual']:.3f} vs expected {w['expected']:.3f} "
            f"(z={w['z']:+.1f}); correlation of hourly deviations (z, clipped) with {focus} rho={rho:+.2f} (p={p:.1e})"}, incident_id)
        if not (moved and p < 1e-3 and abs(rho) >= 0.3):
            self.bus.publish("META_RESULT", "meta", {"text": f"{cand.id} does not explain this incident: not written "
                                                     "to the graph (may be retried by a later investigation)"}, incident_id)
            return None
        self.g.add_node(cand, actor="meta-agent")                  # sandbox passed and it explains: mutate the graph
        sign = "+" if rho > 0 else "-"
        edge = Edge(type="causal", src=cand.id, dst=focus, sign=sign, lag_h=(0, 1), prior=0.3,
                    confidence=round(min(0.9, 0.3 + abs(rho) * 0.6), 3), status="proposed")
        self.g.add_edge(edge, actor="meta-agent")
        self.g.set_status(edge, "proposed", f"meta-agent: {cand.id} {w['rel']:+.0%} at the incident, rho={rho:+.2f} "
                                            f"with {focus}; mechanism to be confirmed", actor="meta-agent")
        cand.status = "validated"
        self.mon.baselines.pop((cand.id, seg_key(cseg)), None)
        m = {"kind": "derived_metric", "metric": cand.id, "explains": True, "edge": f"{cand.id} -({sign})-> {focus}",
             "rel": w["rel"], "z": w["z"], "rho": rho,
             "summary": f"new metric {cand.id} ({w['rel']:+.0%}, z={w['z']:+.1f}) with causal edge {cand.id} "
                        f"-({sign})-> {focus} (rho={rho:+.2f})"}
        self.bus.publish("GRAPH_MUTATION", "meta", m, incident_id)
        return m

    def g_with(self, node: MetricNode):
        """A view of the graph with one node replaced (sandbox for checks that read node definitions)."""
        class _View:
            def __init__(s, g, n):
                s.nodes = {**g.nodes, n.id: n}
        return _View(self.g, node)

    # ================================================================ process mining: unmeasured activities
    FAILURE_WORDS = ("failed", "timed_out", "declined", "error", "rejected")

    def mine_unmeasured_activities(self, engine, store, as_of, history_start) -> list[dict]:
        """Step 4: activities in the discovered process (PM4Py OC-DFG) that no metric measures become new
        metrics. A failure activity is divided by the step it follows (failure rate); a request activity
        is completed by the step that follows it (completion rate). Each candidate is computed in a sandbox
        on history before it is registered in the agent-only table sentinel.metrics."""
        if not hasattr(self.data, "process_activities"):
            return []
        from sentinel.adapters.eye import node_from_catalog
        acts = self.data.process_activities()
        by = {a["activity"]: a for a in acts}
        todo = [a for a in acts if not a["measured"] and a["events"] > 0]
        self.bus.publish("META_PROPOSAL", "meta", {"stage": "process_mining", "text":
            f"PM4Py OC-DFG has {len(acts)} activities; {len(todo)} are not measured by any metric: "
            f"{', '.join(a['activity'] for a in todo) or 'none'}"})
        hist = engine.windows(history_start, as_of)
        dims_all = sorted({d for n in self.g.nodes.values() for d in n.dimensions})
        made = []
        for a in todo:
            act = a["activity"]
            if any(w in act for w in self.FAILURE_WORDS) and a["predecessor"]:
                num, den, kind = act, a["predecessor"], "failure"
                mid, direction = f"{act}_rate", "up"
                desc = f"Share of {den} events followed by {act} (failure rate mined from the process map)"
            elif (succ := next((x for x in a.get("successors", [a["successor"]])
                                if x and not any(w in x for w in self.FAILURE_WORDS)), None))                     and by.get(succ, {}).get("events", 0) <= a["events"] * 1.05:
                num, den, kind = succ, act, "completion"   # success path only; failures get their own rate
                mid, direction = f"{act.replace('_requested', '')}_completion_rate", "down"
                desc = f"Share of {act} events completed by {num} (completion rate mined from the process map)"
            else:
                continue
            if mid in self.g.nodes:
                continue
            dims = self.data.activity_dimensions(act, ["country", "platform", "acquisition_channel"])
            spec = {"id": mid, "name": mid.replace("_", " ").capitalize(), "definition": desc,
                    "calculation": {"op": "event_ratio", "activity": num, "denominator_activity": den, "pattern": kind},
                    "dimensions": dims, "access": {"unit": "ratio", "source": "events"}, "direction": direction,
                    "provenance": "meta.process_mining", "kind": "ratio"}
            rows = engine.compute(spec, hist)                       # sandbox: computed, not stored
            vals = [r[3] for r in rows if not r[2] and r[3] is not None]
            if len(vals) < max(3, len(hist) // 2) or not all(0 <= v <= 1.0 + 1e-9 for v in vals):
                self.bus.publish("META_RESULT", "meta", {"text": f"{mid} rejected in sandbox: "
                                 f"{len(vals)} hourly values, range check {'ok' if vals else 'empty'}"})
                continue
            spec["status"] = "validated"
            store.register_metric(spec)
            self.data.add_agent_metric(spec)
            engine.backfill(spec)
            node = node_from_catalog(spec)
            node.owner, node.status = "meta-agent", "validated"
            self.g.add_node(node, actor="meta-agent")
            for e in self.g.edges_where("breakdown", src=mid, usable=False):
                self.g.set_status(e, "validated", "dimensions of the mined events", actor="meta-agent")
            self.mon.fit([mid])
            m = {"kind": "mined_metric", "metric": mid, "pattern": kind,
                 "summary": f"mined {mid} = {num} / {den} from the process map "
                            f"(baseline {sum(vals) / len(vals):.2%}, {len(vals)} hours)"}
            self.bus.publish("GRAPH_MUTATION", "meta", m)
            made.append(m)
            # where does it act on the funnel? the step rate whose numerator measures the activity that
            # follows the request in the process map
            if kind == "failure":   # the success path after the request (skip the failure itself)
                target_act = next((x for x in by.get(den, {}).get("successors", [])
                                   if x != num and not any(w in x for w in self.FAILURE_WORDS)), None)
            else:
                target_act = num
            step = next((v for v, (n_id, _) in getattr(self.data, "virtual", {}).items()
                         if target_act and self.data.catalog.get(n_id, {}).get("calculation", {}).get("activity") == target_act),
                        None)
            if step:
                sign = "-" if kind == "failure" else "+"
                edge = Edge(type="causal", src=mid, dst=step, sign=sign, lag_h=(0, 1), prior=0.4,
                            confidence=0.4, status="proposed")
                self.g.add_edge(edge, actor="meta-agent")
                from sentinel.brain.validate import check_causal
                verdict, reason, st, conf = check_causal(self.g, self.data, edge, pd.Timestamp(history_start),
                                                         pd.Timestamp(as_of))
                status = {"supported": "validated", "contradicted": "needs_review"}.get(verdict, "proposed")
                self.g.set_status(edge, status, f"process map: {den} -> {target_act}; {reason}", st,
                                  confidence=conf, actor="meta-agent")
                self.bus.publish("GRAPH_MUTATION", "meta", {"kind": "causal", "edge": f"{mid} -({sign})-> {step}",
                                 "summary": f"proposed {mid} -({sign})-> {step} from the process map: {verdict}"})
        return made

    # ================================================================ contributor registration
    def register(self, spec: dict, as_of) -> dict:
        """A contributor submits {id, name, description, table, numerator_sql/denominator_sql | value_sql}.
        No dimensions, kind or graph placement are needed: the agent infers and validates them."""
        sid = spec["id"]
        self.bus.publish("META_PROPOSAL", "meta", {"stage": "register", "text": f"New metric submitted: {sid} "
                                                   f"({spec.get('description', '')}). Inferring kind, dimensions and links."})
        if sid in self.g.nodes:
            return self._reg_result(sid, "rejected", [f"id {sid} already exists"], [])
        kind = "ratio" if spec.get("numerator_sql") else "sum" if "eur" in (spec.get("value_sql") or "") else "count"
        unit = spec.get("unit") or ("share" if kind == "ratio" else "eur" if kind == "sum" else "count")
        table = spec["table"]
        dims = self.data.schema()[table]["dimensions"]
        node = MetricNode(id=sid, name=spec.get("name", sid), description=spec.get("description", ""), kind=kind,
                          unit=unit, table=table, value_sql=spec.get("value_sql"), numerator_sql=spec.get("numerator_sql"),
                          denominator_sql=spec.get("denominator_sql"),
                          dimensions=[d for d in dims if (not ALLOWED_DIMS or d in ALLOWED_DIMS) and d != "sku"],
                          owner=spec.get("owner", "contributor"), status="proposed")
        self.data.register_node(node)
        warnings, edges = [], []
        # duplicate check: same definition as an existing metric
        for n in self.g.nodes.values():
            if (n.value_sql, n.numerator_sql, n.denominator_sql) == (node.value_sql, node.numerator_sql, node.denominator_sql) \
                    and n.table == node.table:
                return self._reg_result(sid, "rejected", [f"duplicate of {n.id}"], [])
        self.g.add_node(node, actor="contributor")
        # formula discovery: numerator / denominator match existing metrics -> num = den x new
        if kind == "ratio":
            num = next((n for n in self.g.nodes.values() if n.id != sid and n.value_sql and n.table == table
                        and _cols(n.value_sql) == _cols(node.numerator_sql)), None)
            den = next((n for n in self.g.nodes.values() if n.id != sid and n.value_sql and n.table == table
                        and _cols(n.value_sql) == _cols(node.denominator_sql)), None)
            if num and den and num.id not in self.g.formulas:
                expr = f"{den.id} * {sid}"
                try:
                    self.g.add_formula(num.id, expr, status="proposed", actor="meta-agent")
                    status, reason, st = check_formula(self.g, self.data, num.id, pd.Timestamp(as_of) - timedelta(days=7),
                                                       pd.Timestamp(as_of) - timedelta(hours=2))
                    for e in self.g.edges_where("formula", dst=num.id, usable=False):
                        self.g.set_status(e, "validated" if status == "validated" else "needs_review", reason, st,
                                          actor="meta-agent")
                    edges.append(f"{num.id} = {expr} ({reason})")
                    self.bus.publish("GRAPH_MUTATION", "meta", {"kind": "formula", "output": num.id, "expr": expr,
                                                                "status": status, "summary": f"discovered {num.id} = {expr}: {reason}"})
                except ValueError as ex:
                    warnings.append(str(ex))
            elif num and den:
                warnings.append(f"{num.id} already has a formula; {sid} kept as a ratio of {num.id} / {den.id}")
        # breakdowns
        for d in node.dimensions:
            status, reason, _ = check_breakdown(self.g, self.data, sid, d, pd.Timestamp(as_of) - timedelta(days=7),
                                                pd.Timestamp(as_of) - timedelta(hours=2))
            e = next(e for e in self.g.edges_where("breakdown", src=sid, usable=False) if e.dimension == d)
            self.g.set_status(e, status, reason, actor="meta-agent")
        self.mon.fit([sid])
        node.status = "validated"
        self.bus.publish("GRAPH_MUTATION", "meta", {"kind": "new_metric", "metric": sid, "dimensions": node.dimensions,
                                                    "summary": f"registered {sid} ({kind}, dims {', '.join(node.dimensions)}); "
                                                               f"now monitored"})
        return self._reg_result(sid, "validated", warnings, edges)

    def _reg_result(self, sid, status, warnings, edges):
        r = {"metric": sid, "status": status, "warnings": warnings, "edges": edges}
        self.bus.publish("META_RESULT", "meta", {"text": f"registration of {sid}: {status}", **r})
        return r
