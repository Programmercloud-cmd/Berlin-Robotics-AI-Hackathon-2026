"""Brain's metric graph over the shared contracts.

Edges always point driver -> driven (src -> dst). The investigator walks them backwards to look for
causes and forwards to measure impact.

  formula    input -> output        from graph/formulas.yaml; op per input derived from the AST
  breakdown  metric -> metric@dim   generated from each node's dimensions ("dst is src restricted
                                    to one value of a dimension")
  causal     src -> dst             from graph/causal.yaml, sign + lag + prior; confidence updated by tests
  context    service:/table: -> metric   from each node's services and table fields

Edge has no free-text field in the contract, so the reason behind every status (shown in the UI for
needs_review) lives in `notes[edge_key]` together with the validation statistics.
"""
from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path
from typing import Iterable

import yaml

from sentinel.contracts import Edge, MetricNode

from .formula import Formula

SERVICE, TABLE = "service:", "table:"
ROLE_TO_OP = {"factor": "product", "divisor": "ratio", "addend": "sum", "subtrahend": "difference"}


def edge_key(e: Edge) -> str:
    base = f"{e.type}:{e.src}->{e.dst}"
    return base + (f"#{e.dimension}" if e.dimension else "")


def _read_yaml(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []


class BrainGraph:
    def __init__(self):
        self.nodes: dict[str, MetricNode] = {}
        self.formulas: dict[str, str] = {}          # output -> expression
        self.edges: dict[str, Edge] = {}
        self.notes: dict[str, dict] = {}            # edge key -> {"reason": str, "stats": dict}
        self.log: list[dict] = []
        # profile: which metrics carry money and volume (catalog-specific ids)
        self.profile: dict = {"money_metric": "contribution", "gmv_metric": "gmv", "orders_metric": "orders",
                              "money_scale": 1.0, "money_label": "contribution",
                              "key_metrics": ["contribution", "gmv", "orders", "conversion_rate",
                                              "payment_success_rate", "sessions"]}

    # ================================================================ building
    @classmethod
    def from_seed(cls, metrics="graph/metrics.yaml", formulas="graph/formulas.yaml",
                  causal="graph/causal.yaml") -> "BrainGraph":
        g = cls()
        for x in _read_yaml(metrics):
            g.add_node(MetricNode(**x), actor="seed")
        for f in _read_yaml(formulas):
            g.add_formula(f["output"], f["expr"], status="seed", actor="seed")
        for c in _read_yaml(causal):
            g.add_edge(Edge(type="causal", src=c["src"], dst=c["dst"], sign=c["sign"], lag_h=tuple(c["lag_h"]),
                            prior=c["prior"], confidence=c["prior"], status="seed"), actor="seed")
        return g

    def add_node(self, node: MetricNode, actor: str = "extender"):
        self.nodes[node.id] = node
        for d in node.dimensions:
            self.add_edge(Edge(type="breakdown", src=node.id, dst=f"{node.id}@{d}", dimension=d,
                               status=node.status), actor, quiet=True)
        for s in node.services:
            self.add_edge(Edge(type="context", src=SERVICE + s, dst=node.id, status=node.status), actor, quiet=True)
        for t in self.tables_of(node.id):
            self.add_edge(Edge(type="context", src=TABLE + t, dst=node.id, status=node.status), actor, quiet=True)
        self._log(actor, "add_node", node.id)

    def add_formula(self, output: str, expr: str, status: str = "proposed", actor: str = "extender",
                    confidence: float = 1.0) -> list[Edge]:
        f = Formula.parse(expr)
        unknown = [v for v in f.variables if v not in self.nodes] + ([output] if output not in self.nodes else [])
        if unknown:
            raise ValueError(f"formula for {output} references unknown metrics {unknown}")
        if self._formula_cycle(output, f.variables):
            raise ValueError(f"formula {output} = {expr} would create a formula cycle")
        self.formulas[output] = expr
        out = []
        for var, role in f.roles().items():
            e = Edge(type="formula", src=var, dst=output, expr=expr, op=ROLE_TO_OP[role.role],
                     status=status, confidence=confidence)
            self.add_edge(e, actor, quiet=True)
            out.append(e)
        self._log(actor, "add_formula", output, expr)
        return out

    def add_edge(self, e: Edge, actor: str = "extender", quiet: bool = False):
        self.edges[edge_key(e)] = e
        if not quiet:
            self._log(actor, "add_edge", edge_key(e))

    def set_status(self, e: Edge, status: str, reason: str = "", stats: dict | None = None,
                   confidence: float | None = None, actor: str = "validator"):
        e.status = status
        if confidence is not None:
            e.confidence = round(float(confidence), 3)
        self.notes[edge_key(e)] = {"reason": reason, "stats": stats or {}}
        self._log(actor, "status", edge_key(e), f"{status}: {reason}")

    def _formula_cycle(self, output: str, inputs: list[str]) -> bool:
        adj: dict[str, set[str]] = {}
        for e in self.edges.values():
            if e.type == "formula" and e.status != "rejected" and e.dst != output:
                adj.setdefault(e.src, set()).add(e.dst)
        for i in inputs:
            adj.setdefault(i, set()).add(output)
        seen, stack = set(), [output]
        while stack:  # can we get from output back to itself?
            cur = stack.pop()
            for n in adj.get(cur, ()):
                if n == output:
                    return True
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
        return False

    # ================================================================ queries (used by monitor / investigator)
    def edges_where(self, type_: str | Iterable[str] | None = None, src: str | None = None, dst: str | None = None,
                    usable: bool = True) -> list[Edge]:
        types = {type_} if isinstance(type_, str) else set(type_ or ("formula", "breakdown", "causal", "context"))
        out = []
        for e in self.edges.values():
            if e.type not in types or (src and e.src != src) or (dst and e.dst != dst):
                continue
            if usable and not self.usable(e):
                continue
            out.append(e)
        return out

    @staticmethod
    def usable(e: Edge) -> bool:
        """Spec: decomposition uses validated or seed formula edges; hypotheses use causal edges with
        confidence >= 0.2."""
        if e.type == "formula":
            return e.status in ("validated", "seed")
        if e.type == "causal":
            return e.status != "rejected" and e.confidence >= 0.2
        return e.status != "rejected"

    def inputs(self, metric: str) -> list[Edge]:
        return self.edges_where("formula", dst=metric)

    def outputs(self, metric: str) -> list[Edge]:
        return self.edges_where("formula", src=metric)

    def causes(self, metric: str) -> list[Edge]:
        return sorted(self.edges_where("causal", dst=metric), key=lambda e: -e.confidence)

    def effects(self, metric: str) -> list[Edge]:
        return self.edges_where("causal", src=metric)

    def services(self, metric: str) -> list[str]:
        return [e.src[len(SERVICE):] for e in self.edges_where("context", dst=metric) if e.src.startswith(SERVICE)]

    def tables_of(self, metric: str, _seen=frozenset()) -> list[str]:
        n = self.nodes.get(metric)
        if n is None or metric in _seen:
            return []
        if not (n.value_sql or n.numerator_sql) and n.table and metric not in self.formulas:
            return [n.table]                       # source-backed node without SQL (external catalog)
        if not (n.value_sql or n.numerator_sql):  # formula-only node
            f = self.formulas.get(metric)
            return sorted({t for v in (Formula.parse(f).variables if f else []) for t in self.tables_of(v, _seen | {metric})})
        sqls = [s for s in (n.value_sql, n.numerator_sql, n.denominator_sql) if s]
        out = {n.table} if n.table else set()
        for s in sqls:
            out |= set(re.findall(r"\b(fact_[a-z]+)\.", s))
        return sorted(out)

    def upstream(self, metric: str, types=("formula", "causal"), max_depth: int = 8) -> dict[str, int]:
        return self._walk(metric, types, backwards=True, max_depth=max_depth)

    def downstream(self, metric: str, types=("formula", "causal"), max_depth: int = 8) -> dict[str, int]:
        return self._walk(metric, types, backwards=False, max_depth=max_depth)

    def _walk(self, start, types, backwards, max_depth):
        dist, q = {start: 0}, deque([start])
        while q:
            cur = q.popleft()
            if dist[cur] >= max_depth:
                continue
            es = self.edges_where(types, dst=cur) if backwards else self.edges_where(types, src=cur)
            for e in es:
                nxt = e.src if backwards else e.dst
                if nxt in self.nodes and nxt not in dist:
                    dist[nxt] = dist[cur] + 1
                    q.append(nxt)
        dist.pop(start)
        return dist

    def roots(self) -> list[str]:
        """Top of the driver tree: metrics that feed no formula (e.g. contribution)."""
        fed = {e.src for e in self.edges.values() if e.type == "formula"}
        outs = {e.dst for e in self.edges.values() if e.type == "formula"}
        return sorted(outs - fed)

    # ================================================================ persistence / export
    def _log(self, actor, action, target, detail=""):
        from datetime import datetime, timezone
        self.log.append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "actor": actor,
                         "action": action, "target": target, "detail": detail})

    def to_dict(self) -> dict:
        return {"profile": self.profile, "nodes": [n.model_dump(mode="json") for n in self.nodes.values()],
                "formulas": self.formulas,
                "edges": [dict(e.model_dump(mode="json"), key=k, **self.notes.get(k, {})) for k, e in self.edges.items()],
                "log": self.log}

    def save(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_dict(), indent=1, default=str), encoding="utf-8")

    @classmethod
    def load(cls, path) -> "BrainGraph":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        g = cls()
        g.nodes = {n["id"]: MetricNode(**n) for n in d["nodes"]}
        g.formulas = d["formulas"]
        for x in d["edges"]:
            k = x.pop("key")
            note = {f: x.pop(f) for f in ("reason", "stats") if f in x}
            g.edges[k] = Edge(**x)
            if note:
                g.notes[k] = note
        g.log = d["log"]
        g.profile.update(d.get("profile", {}))
        return g
