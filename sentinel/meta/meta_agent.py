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


ALLOWED_DIMS = set(get_args(Dim))


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
                         if "no dimension" in r and d in ALLOWED_DIMS})
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
                if dim not in ALLOWED_DIMS or (wanted and dim not in wanted):
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
                          denominator_sql=spec.get("denominator_sql"), dimensions=[d for d in dims if d in ALLOWED_DIMS],
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
