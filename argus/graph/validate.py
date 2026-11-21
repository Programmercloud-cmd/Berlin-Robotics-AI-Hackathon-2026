"""Whole-graph structural validation.

Per-contract checks (units, populations, time semantics, dimensions, cycles on registration)
live in MetricGraph.register. This module checks properties of the graph as a whole:
  * arithmetic edges form a DAG (the graph overall may have cycles),
  * every business metric is connected to a configured objective (else: `unplaced`),
  * objectives that are not registered yet are reported as unresolved,
  * provisional / rejected edges are surfaced with their reasons.

Data-based checks (subset invariants, partition reconciliation, shared denominators,
observational tests of possible drivers) run against the observation service and are
added with it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

from .graph import STRUCTURAL, MetricGraph
from .model import EdgeType


@dataclass
class Issue:
    severity: str   # error | warning | info
    code: str
    target: str
    message: str

    def __str__(self):
        return f"[{self.severity.upper():7}] {self.code:22} {self.target}: {self.message}"


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    def add(self, *a):
        self.issues.append(Issue(*a))

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self):
        return not self.errors


def validate_structure(g: MetricGraph) -> Report:
    r = Report()
    ag = nx.DiGraph()
    for e in g.edges.values():
        if e.type == EdgeType.ARITHMETIC and e.status != "rejected":
            ag.add_edge(e.source, e.target)
    for cyc in nx.simple_cycles(ag):
        r.add("error", "arithmetic_cycle", cyc[0], " -> ".join(cyc + [cyc[0]]))

    for o in g.unresolved_objectives():
        r.add("info", "unresolved_objective", o, "configured objective is not registered yet")

    ug = nx.Graph()
    for m in g.active_metrics():
        ug.add_node(m.id)
    for e in g.edges.values():
        if e.type in STRUCTURAL and e.status != "rejected" and e.source in g.metrics and e.target in g.metrics:
            ug.add_edge(e.source, e.target)
    anchors = [o for o in g.objective_ids() if o in ug]
    reachable = set().union(*(nx.node_connected_component(ug, a) for a in anchors)) if anchors else set()
    for m in g.active_metrics():
        if m.id in reachable:
            continue
        if m.role == "data_quality":
            r.add("info", "data_quality_metric", m.id, "monitors source health; linked through sourced_from")
        else:
            r.add("warning", "unplaced_metric", m.id, "not connected to any objective yet (awaiting inference)")

    for e in g.edges.values():
        if e.status == "rejected":
            r.add("warning", "edge_rejected", e.id, "; ".join(c.detail for c in e.checks if c.passed is False)
                  or "; ".join(e.reasons))
    return r


def edge_summary(g: MetricGraph) -> dict:
    out: dict[str, dict[str, int]] = {}
    for e in g.edges.values():
        d = out.setdefault(e.type.value, {})
        d[e.status] = d.get(e.status, 0) + 1
    return out
