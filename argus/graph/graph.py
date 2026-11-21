"""The metric graph: the agent's evolving model of how the business works.

Nothing in here is hand-wired. A contributor registers a MetricContract; the graph
  1. rejects contracts that are malformed, version-conflicting, cyclic or use unsupported dimensions,
  2. derives contract edges (sourced_from, breakdown_by, arithmetic_component) and checks units,
     populations, time semantics and dimensions for each,
  3. infers candidate edges with interpretable rules (population subsets, shared denominators,
     outcome partitions, definition references). Inferred edges start *provisional*; data
     validation (validate.py) and later LLM proposals promote or reject them,
  4. records everything in a versioned change log.
The same pipeline runs at bootstrap and when a new metric arrives later.
"""
from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path
from typing import Iterable

import networkx as nx
import yaml

from .formula import Formula, FormulaError, infer_unit
from .model import (ChangeLogEntry, Check, Dimension, Edge, EdgeType, Metric, MetricContract, Registration,
                    Source, now_iso)
from .semantics import normalize_unit, parse_population, subset_relation, time_compatible

DIM = "dim:"
SRC = "source:"
STRUCTURAL = (EdgeType.ARITHMETIC, EdgeType.SUBSET, EdgeType.DRIVER, EdgeType.ASSOCIATED)


class GraphError(ValueError):
    pass


class MetricGraph:
    def __init__(self):
        self.metrics: dict[str, Metric] = {}
        self.dimensions: dict[str, Dimension] = {}
        self.sources: dict[str, Source] = {}
        self.edges: dict[str, Edge] = {}
        self.objectives: list[dict] = []
        self.registrations: list[Registration] = []
        self.changelog: list[ChangeLogEntry] = []
        self.revision = 0

    # ================================================================ catalog metadata
    def add_source(self, src: Source):
        self.sources[src.id] = src
        for d in src.dimensions:
            cd = src.canonical(d)
            self.dimensions.setdefault(cd, Dimension(id=cd))

    def load_sources(self, path: str | Path):
        spec = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        for d_id, d in (spec.get("dimensions") or {}).items():
            self.dimensions[d_id] = Dimension(id=d_id, **(d or {}))
        for s_id, s in (spec.get("sources") or {}).items():
            self.add_source(Source(id=s_id, **s))

    def load_objectives(self, path: str | Path):
        self.objectives = (yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}).get("objectives", [])

    # ================================================================ registration
    def register(self, contract: MetricContract | dict, actor: str = "contributor") -> Registration:
        """Validate and incorporate a metric contract. Never requires the contributor to place it."""
        reg_id = f"reg-{len(self.registrations) + 1:04d}"
        try:
            c = contract if isinstance(contract, MetricContract) else MetricContract(**contract)
        except Exception as e:  # pydantic ValidationError
            mid = contract.get("metric_id", "?") if isinstance(contract, dict) else "?"
            reg = Registration(registration_id=reg_id, metric_id=str(mid), version=int(contract.get("version", 0) or 0)
                               if isinstance(contract, dict) else 0, state="rejected",
                               reasons=[_short_validation_error(e)], actor=actor)
            self.registrations.append(reg)
            return reg
        reg = Registration(registration_id=reg_id, metric_id=c.metric_id, version=c.version, state="validating",
                           actor=actor)
        self.registrations.append(reg)

        existing = self.metrics.get(c.metric_id)
        if existing:
            if c.version < existing.contract.version:
                return self._reject(reg, f"version {c.version} is older than registered v{existing.contract.version}")
            if c.version == existing.contract.version:
                if c.model_dump() == existing.contract.model_dump():
                    reg.state, reg.warnings = "registered", ["identical contract already registered (idempotent)"]
                    reg.catalog_revision = self.revision
                    return reg
                return self._reject(reg, f"conflicting definition for existing version v{c.version}")

        reasons = self._contract_errors(c)
        if reasons:
            return self._reject(reg, *reasons)

        # accept: (re)create node and its edges
        if existing:
            self._drop_edges_of(c.metric_id, keep_inbound_from_others=True)
        m = Metric(contract=c, canonical_dimensions=self._canonical_dims(c), provenance=actor,
                   role=self._infer_role(c))
        self.metrics[c.metric_id] = m
        self.revision += 1
        before = set(self.edges)
        self._derive_contract_edges(m)
        self._infer_edges(m)
        if existing:
            self._flag_stale_dependents(c.metric_id, c.version, reg)
        reg.edges_created = sorted(set(self.edges) - before)
        reg.state = "registered"
        reg.catalog_revision = self.revision
        self._log(actor, "register" if not existing else "register_new_version", c.metric_id,
                  f"v{c.version}; {len(reg.edges_created)} edges", )
        return reg

    def _reject(self, reg: Registration, *reasons: str) -> Registration:
        reg.state = "rejected"
        reg.reasons = list(reasons)
        self._log(reg.actor, "reject_registration", reg.metric_id, "; ".join(reasons))
        return reg

    def _contract_errors(self, c: MetricContract) -> list[str]:
        errs = []
        for s in c.source_ids:
            if s not in self.sources:
                errs.append(f"unknown source {s!r}")
        if errs:
            return errs
        local_dims = {d for s in c.source_ids for d in self.sources[s].dimensions}
        for d in c.dimensions:
            if d not in local_dims:
                errs.append(f"dimension {d!r} not supported by sources {c.source_ids}")
        if c.calculation is not None:
            try:
                expr = c.expression()
                Formula.parse(expr)
                ops = c.operands()
            except (ValueError, FormulaError) as e:
                return errs + [f"malformed calculation: {e}"]
            canon = set(self._canonical_dims(c))
            for op in ops:
                om = self.metrics.get(op.metric_id)
                if om is None:
                    errs.append(f"calculation references unregistered metric {op.metric_id!r}")
                    continue
                if om.status == "deprecated":
                    errs.append(f"calculation references deprecated metric {op.metric_id!r}")
                if om.contract.version != op.version:
                    errs.append(f"operand {op.metric_id} v{op.version} does not match registered "
                                f"v{om.contract.version}")
                missing = canon - set(om.canonical_dimensions)
                if missing:
                    errs.append(f"dimension(s) {sorted(missing)} not supported by operand {op.metric_id}")
            if not errs:
                f = Formula.parse(expr)
                ck = self._unit_check(f, {o.metric_id: self.metrics[o.metric_id] for o in ops}, c)
                if ck.passed is False:
                    errs.append(f"unit check failed: {ck.detail}")
            if not errs and self._would_cycle(c.metric_id, [o.metric_id for o in ops]):
                errs.append("cyclic arithmetic definition")
        return errs

    def _would_cycle(self, target: str, operands: list[str]) -> bool:
        g = nx.DiGraph()
        for e in self.edges.values():
            if e.type == EdgeType.ARITHMETIC and e.status != "rejected" and e.target != target:
                g.add_edge(e.source, e.target)
        for o in operands:
            g.add_edge(o, target)
        return not nx.is_directed_acyclic_graph(g)

    def _canonical_dims(self, c: MetricContract) -> list[str]:
        out = []
        for d in c.dimensions:
            cd = d
            for s in c.source_ids:
                if d in self.sources[s].dimensions:
                    cd = self.sources[s].canonical(d)
                    break
            if cd not in out:
                out.append(cd)
        return out

    def _infer_role(self, c: MetricContract) -> str:
        if any(self.sources[s].role == "data_quality" for s in c.source_ids):
            return "data_quality"
        u = normalize_unit(c.unit)
        if c.time_semantics == "snapshot":
            return "snapshot"
        if c.time_semantics == "business_day_to_date":
            return "pacing"
        if not u.exps:
            return "rate"
        if str(u) == "EURc":
            return "currency"
        if "/" in str(u):
            return "unit_economics"
        return "count"

    # ================================================================ contract-derived edges
    def _derive_contract_edges(self, m: Metric):
        c = m.contract
        for s in c.source_ids:
            self._put(Edge(type=EdgeType.SOURCED, source=SRC + s, target=m.id, status="validated",
                           checks=[Check(name="lineage", passed=True, detail="declared source of the contract")]))
        add = c.aggregation_policy
        for d in m.canonical_dimensions:
            self._put(Edge(type=EdgeType.BREAKDOWN, source=DIM + d, target=m.id, dimension=d, status="validated",
                           checks=[Check(name="schema", passed=True, detail="dimension present in source schema")],
                           evidence={"aggregation": "additive partition" if add == "sum" else
                                     "ratio of summed numerator/denominator" if add == "ratio_of_sums" else
                                     f"non-additive ({add}): segments compared, not summed"}))
        if c.calculation is None:
            return
        expr = c.expression()
        f = Formula.parse(expr)
        roles = f.roles()
        ops = {o.metric_id: self.metrics[o.metric_id] for o in c.operands()}
        unit_check = self._unit_check(f, ops, c)
        top = c.calculation.get("operator")
        op_pops = sorted({om.contract.population for om in ops.values()})
        for oid, om in ops.items():
            checks = [unit_check,
                      self._time_check(om, m),
                      self._population_check(om, m, roles[oid].role, top, op_pops)]
            e = Edge(type=EdgeType.ARITHMETIC, source=oid, target=m.id, expression=expr, role=roles[oid].role,
                     polarity=roles[oid].polarity, provenance="contract", checks=checks,
                     evidence={"derived": True,
                               "note": "metric is computed from this operand; not independent evidence"})
            self._put(e.decide())

    def _unit_check(self, f: Formula, ops: dict[str, Metric], c: MetricContract) -> Check:
        try:
            units = {k: str(normalize_unit(v.contract.unit)) for k, v in ops.items()}
            got = infer_unit(f, units)
        except FormulaError as e:
            return Check(name="units", passed=False, detail=str(e))
        want = normalize_unit(c.unit)
        return Check(name="units", passed=got == want, detail=f"formula gives {got}, contract declares {want}")

    @staticmethod
    def _time_check(om: Metric, m: Metric) -> Check:
        a, b = om.contract.time_semantics, m.contract.time_semantics
        return Check(name="time_semantics", passed=time_compatible(a, b), detail=f"{a} vs {b}")

    @staticmethod
    def _population_check(om: Metric, m: Metric, role: str, top: str | None, op_pops: list[str]) -> Check:
        po, pm = om.contract.population, m.contract.population
        if top == "divide":
            if role == "divisor":
                ok = parse_population(po).key == parse_population(pm).key
                return Check(name="population", passed=ok or None,
                             detail=f"denominator population {po} {'matches' if ok else 'differs from'} rate population {pm}")
            ok = po == pm or subset_relation(po, pm)
            return Check(name="population", passed=ok or None,
                         detail=f"numerator population {po} is {'within' if ok else 'NOT within'} {pm}")
        if top in ("add", "subtract"):
            if len({parse_population(p).key for p in op_pops}) == 1:
                return Check(name="population", passed=True, detail=f"all operands share population {po}")
            return Check(name="population", passed=True,
                         detail=f"operands mix populations {op_pops}; identity holds per calendar period "
                                "on common dimensions only, not per session/order")
        return Check(name="population", passed=True, detail=f"{po} -> {pm}")

    # ================================================================ rule-based inference
    def _infer_edges(self, m: Metric):
        """Interpretable candidate rules. Inferred edges are provisional until data supports them."""
        self._rule_population_subsets(m)
        self._rule_shared_denominator(m)
        self._rule_outcome_partitions()
        self._rule_definition_references(m)

    def _rule_population_subsets(self, m: Metric):
        for x in self.active_metrics():
            if x.id == m.id:
                continue
            for child, parent in ((m, x), (x, m)):
                if self._is_count(child) and self._is_count(parent) and \
                        subset_relation(child.contract.population, parent.contract.population) and \
                        normalize_unit(child.contract.unit) == normalize_unit(parent.contract.unit):
                    e = Edge(type=EdgeType.SUBSET, source=child.id, target=parent.id, provenance="rule:population_subset",
                             confidence=0.8,
                             checks=[Check(name="population", passed=True,
                                           detail=f"{child.contract.population} qualifies {parent.contract.population}"),
                                     Check(name="units", passed=True, detail=child.contract.unit),
                                     Check(name="invariant", passed=None, detail="awaiting data: subset <= superset")])
                    self._put(e.decide(), keep_existing=True)

    def _rule_shared_denominator(self, m: Metric):
        """A query_ref rate whose population equals a registered count's population: that count is its denominator."""
        pairs = []
        for x in self.active_metrics():
            if x.id == m.id:
                continue
            for rate, count in ((m, x), (x, m)):
                if rate.role == "rate" and rate.contract.query_ref and self._is_count(count) and \
                        rate.contract.population == count.contract.population and \
                        time_compatible(rate.contract.time_semantics, count.contract.time_semantics):
                    pairs.append((rate, count))
        for rate, count in pairs:
            expr = f"<numerator> / {count.id}"
            e = Edge(type=EdgeType.ARITHMETIC, source=count.id, target=rate.id, expression=expr, role="divisor",
                     polarity=-1, provenance="rule:shared_denominator", confidence=0.7,
                     reasons=["numerator is not a registered metric; denominator inferred from identical population"],
                     checks=[Check(name="population", passed=True, detail=rate.contract.population),
                             Check(name="reconciliation", passed=None,
                                   detail="awaiting data: query denominator equals count")])
            self._put(e.decide(), keep_existing=True)

    def _rule_outcome_partitions(self):
        """Parent count with >=2 qualified subset counts: propose parent = sum(children) (data decides)."""
        groups: dict[tuple, list[Metric]] = {}
        parents: dict[tuple, Metric] = {}
        for x in self.active_metrics():
            if not self._is_count(x):
                continue
            p = parse_population(x.contract.population)
            key = (p.entity, p.basis, str(normalize_unit(x.contract.unit)), tuple(sorted(x.canonical_dimensions)))
            if p.qualifier:
                groups.setdefault(key, []).append(x)
            else:
                parents[key] = x
        for key, kids in groups.items():
            parent = parents.get(key)
            if parent is None or len(kids) < 2:
                continue
            ids = sorted(k.id for k in kids)
            expr = " + ".join(ids)
            if any(e.type == EdgeType.ARITHMETIC and e.target == parent.id and e.expression == expr
                   for e in self.edges.values()):
                continue
            # a larger set of subsets supersedes an untested smaller candidate
            for eid in [k for k, e in self.edges.items() if e.provenance == "rule:outcome_partition"
                        and e.target == parent.id and e.status == "provisional"]:
                del self.edges[eid]
            for k in ids:
                e = Edge(type=EdgeType.ARITHMETIC, source=k, target=parent.id, expression=expr, role="addend",
                         polarity=1, provenance="rule:outcome_partition", confidence=0.5,
                         reasons=["candidate partition: subsets could overlap or be incomplete"],
                         checks=[Check(name="units", passed=True, detail="identical units"),
                                 Check(name="population", passed=True, detail="qualified subsets of the same population"),
                                 Check(name="reconciliation", passed=None,
                                       detail="awaiting data: sum of subsets equals parent in every bucket/segment")])
                self._put(e.decide(), keep_existing=True)

    def _rule_definition_references(self, m: Metric):
        """If metric A's definition names metric B (and no edge links them), B is a candidate driver of A."""
        def phrases(x: Metric) -> list[str]:
            name = x.contract.name.lower()
            out = {name, x.id.replace("_", " ")}
            out |= {" ".join(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in p.split()) for p in list(out)}
            return [p for p in out if len(p) >= 5]

        def linked(a: str, b: str) -> bool:
            return any({e.source, e.target} == {a, b} for e in self.edges.values() if e.type in STRUCTURAL)

        for a in self.active_metrics():
            # mask phrases of metrics already linked to `a` (longest first), so "checkout sessions"
            # does not also count as a reference to "sessions"
            text = a.contract.definition.lower()
            known = [p for x in self.active_metrics() if x.id == a.id or linked(a.id, x.id) for p in phrases(x)]
            for p in sorted(known, key=len, reverse=True):
                text = re.sub(rf"\b{re.escape(p)}\b", " ", text)
            for b in self.active_metrics():
                if a.id == b.id or m.id not in (a.id, b.id) or linked(a.id, b.id):
                    continue
                hit = next((p for p in sorted(phrases(b), key=len, reverse=True)
                            if re.search(rf"\b{re.escape(p)}\b", text)), None)
                if hit:
                    e = Edge(type=EdgeType.DRIVER, source=b.id, target=a.id, provenance="rule:definition_reference",
                             confidence=0.4, mechanism=f"definition of {a.id} refers to '{hit}'",
                             reasons=["textual reference only; direction and strength untested"],
                             checks=[Check(name="evidence", passed=None, detail="awaiting observational test")])
                    self._put(e.decide(), keep_existing=True)

    @staticmethod
    def _is_count(x: Metric) -> bool:
        return x.role in ("count", "currency") and x.contract.aggregation_policy == "sum"

    def _flag_stale_dependents(self, metric_id: str, version: int, reg: Registration):
        for x in self.active_metrics():
            for op in x.contract.operands():
                if op.metric_id == metric_id and op.version != version:
                    reg.warnings.append(f"{x.id} still references {metric_id} v{op.version}; "
                                        "its arithmetic edges are provisional until it is re-registered")
                    for e in self.edges.values():
                        if e.type == EdgeType.ARITHMETIC and e.target == x.id and e.source == metric_id:
                            e.checks.append(Check(name="version", passed=None, detail=f"operand upgraded to v{version}"))
                            e.decide()

    # ================================================================ agent-side edge maintenance
    def _put(self, e: Edge, keep_existing: bool = False):
        old = self.edges.get(e.id)
        if old is not None and keep_existing:
            return
        if old is not None:
            e.revision = old.revision + 1
            e.created_at = old.created_at
        e.revised_at = now_iso()
        self.edges[e.id] = e

    def upsert_edge(self, e: Edge, actor: str = "agent"):
        """Add or revise an inferred edge (e.g. from data validation or an LLM proposal)."""
        for end in (e.source, e.target):
            if not self.has_node(end):
                raise GraphError(f"unknown node {end!r}")
        if e.type == EdgeType.ARITHMETIC and e.status != "rejected" and \
                self._would_cycle(e.target, [e.source] + [x.source for x in self.edges.values()
                                                         if x.type == EdgeType.ARITHMETIC and x.target == e.target]):
            e.status, e.reasons = "rejected", e.reasons + ["would create an arithmetic cycle"]
        self._put(e)
        self.revision += 1
        self._log(actor, "upsert_edge", e.id, f"{e.status}: {'; '.join(c.detail for c in e.checks)[:200]}")

    def set_edge_status(self, edge_id: str, status: str, reason: str, actor: str = "agent", **evidence):
        e = self.edges[edge_id]
        e.status = status
        if reason:
            e.reasons.append(reason)
        e.evidence.update(evidence)
        e.revision += 1
        e.revised_at = now_iso()
        self.revision += 1
        self._log(actor, "edge_status", edge_id, f"{status}: {reason}")

    def _drop_edges_of(self, metric_id: str, keep_inbound_from_others: bool = False):
        for eid in [k for k, e in self.edges.items() if e.target == metric_id or
                    (not keep_inbound_from_others and e.source == metric_id)]:
            del self.edges[eid]

    def deprecate(self, metric_id: str, actor: str = "contributor", reason: str = ""):
        users = sorted({x.id for x in self.active_metrics()
                        if metric_id in [o.metric_id for o in x.contract.operands()]})
        if users:
            raise GraphError(f"cannot deprecate {metric_id}: operand of {users}")
        self.metric(metric_id).status = "deprecated"
        self.revision += 1
        self._log(actor, "deprecate", metric_id, reason)

    # ================================================================ lookups
    def metric(self, metric_id: str) -> Metric:
        try:
            return self.metrics[metric_id]
        except KeyError:
            raise GraphError(f"unknown metric {metric_id!r}") from None

    def has_node(self, node: str) -> bool:
        if node.startswith(DIM):
            return node[len(DIM):] in self.dimensions
        if node.startswith(SRC):
            return node[len(SRC):] in self.sources
        return node in self.metrics

    def active_metrics(self) -> list[Metric]:
        return [m for m in self.metrics.values() if m.status != "deprecated"]

    def edges_of(self, node: str, types: Iterable[EdgeType] | None = None, direction: str = "in",
                 include_rejected: bool = False) -> list[Edge]:
        types = set(types or EdgeType)
        return [e for e in self.edges.values() if e.type in types
                and (include_rejected or e.status != "rejected")
                and (e.target == node if direction == "in" else e.source == node)]

    def components(self, metric_id: str) -> dict[str, list[Edge]]:
        out: dict[str, list[Edge]] = {}
        for e in self.edges_of(metric_id, [EdgeType.ARITHMETIC]):
            out.setdefault(e.expression, []).append(e)
        return out

    def subsets(self, metric_id: str) -> list[str]:
        return [e.source for e in self.edges_of(metric_id, [EdgeType.SUBSET])]

    def drivers(self, metric_id: str) -> list[Edge]:
        return self.edges_of(metric_id, [EdgeType.DRIVER, EdgeType.ASSOCIATED])

    def breakdowns(self, metric_id: str) -> list[str]:
        return [e.dimension for e in self.edges_of(metric_id, [EdgeType.BREAKDOWN])]

    def source_ids(self, metric_id: str) -> list[str]:
        return [e.source[len(SRC):] for e in self.edges_of(metric_id, [EdgeType.SOURCED])]

    def _walk(self, start: str, types, direction: str, max_depth: int) -> dict[str, int]:
        dist, q = {start: 0}, deque([start])
        while q:
            cur = q.popleft()
            if dist[cur] >= max_depth:
                continue
            for e in self.edges_of(cur, types, direction):
                nxt = e.source if direction == "in" else e.target
                if nxt in self.metrics and nxt not in dist:
                    dist[nxt] = dist[cur] + 1
                    q.append(nxt)
        dist.pop(start)
        return dist

    def upstream(self, metric_id: str, types=STRUCTURAL, max_depth: int = 10) -> dict[str, int]:
        return self._walk(metric_id, set(types), "in", max_depth)

    def downstream(self, metric_id: str, types=STRUCTURAL, max_depth: int = 10) -> dict[str, int]:
        return self._walk(metric_id, set(types), "out", max_depth)

    def neighbours(self, metric_id: str) -> set[str]:
        return {e.source if e.target == metric_id else e.target for e in self.edges.values()
                if metric_id in (e.source, e.target) and e.type in STRUCTURAL}

    def objective_ids(self) -> list[str]:
        return [o["metric_id"] for o in self.objectives]

    def unresolved_objectives(self) -> list[str]:
        return [o for o in self.objective_ids() if o not in self.metrics]

    def nx(self) -> nx.MultiDiGraph:
        g = nx.MultiDiGraph()
        for m in self.metrics.values():
            g.add_node(m.id, kind="metric", role=m.role, status=m.status)
        for d in self.dimensions:
            g.add_node(DIM + d, kind="dimension")
        for s in self.sources:
            g.add_node(SRC + s, kind="source")
        for e in self.edges.values():
            g.add_edge(e.source, e.target, key=e.id, type=e.type.value, status=e.status)
        return g

    # ================================================================ bootstrap & persistence
    @classmethod
    def bootstrap(cls, sources: str | Path, registry: str | Path, objectives: str | Path | None = None,
                  actor: str = "registry") -> tuple["MetricGraph", list[Registration]]:
        """Register every contract of a registry file, in dependency order (operands first)."""
        g = cls()
        g.load_sources(sources)
        if objectives:
            g.load_objectives(objectives)
        contracts = load_contracts(registry)
        pending, regs = list(contracts), []
        while pending:
            progressed, nxt = False, []
            for c in pending:
                missing = [o.metric_id for o in c.operands() if o.metric_id not in g.metrics]
                if missing and any(o in {p.metric_id for p in pending} for o in missing):
                    nxt.append(c)
                    continue
                regs.append(g.register(c, actor))
                progressed = True
            if not progressed:
                regs += [g.register(c, actor) for c in nxt]
                break
            pending = nxt
        return g, regs

    def _log(self, actor: str, action: str, target: str, detail: str = ""):
        self.changelog.append(ChangeLogEntry(actor=actor, action=action, target=target, detail=detail,
                                             revision=self.revision))

    def to_dict(self) -> dict:
        return {
            "schema_version": 2, "saved_at": now_iso(), "revision": self.revision,
            "objectives": self.objectives,
            "dimensions": [d.model_dump(mode="json") for d in self.dimensions.values()],
            "sources": [s.model_dump(mode="json") for s in self.sources.values()],
            "metrics": [m.model_dump(mode="json") for m in self.metrics.values()],
            "edges": [e.model_dump(mode="json") for e in self.edges.values()],
            "registrations": [r.model_dump(mode="json") for r in self.registrations],
            "changelog": [c.model_dump(mode="json") for c in self.changelog],
        }

    def save(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "MetricGraph":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        g = cls()
        g.revision = d["revision"]
        g.objectives = d["objectives"]
        g.dimensions = {x["id"]: Dimension(**x) for x in d["dimensions"]}
        g.sources = {x["id"]: Source(**x) for x in d["sources"]}
        g.metrics = {x["contract"]["metric_id"]: Metric(**x) for x in d["metrics"]}
        for x in d["edges"]:
            e = Edge(**x)
            g.edges[e.id] = e
        g.registrations = [Registration(**x) for x in d["registrations"]]
        g.changelog = [ChangeLogEntry(**x) for x in d["changelog"]]
        return g


def load_contracts(path: str | Path, raw: bool = False) -> list:
    """Load contracts from a registry YAML ({defaults, metrics}) or a single-contract YAML/JSON file.
    raw=True returns unvalidated dicts so that `register` can reject them with reasons."""
    p = Path(path)
    spec = yaml.safe_load(p.read_text(encoding="utf-8"))
    if isinstance(spec, dict) and "metrics" in spec:
        defaults = spec.get("defaults") or {}
        items = [{**defaults, **m} for m in spec["metrics"]]
    else:
        items = [spec]
    return items if raw else [MetricContract(**x) for x in items]


def _short_validation_error(e: Exception) -> str:
    errs = getattr(e, "errors", None)
    if callable(errs):
        parts = []
        for x in errs():
            loc = ".".join(str(i) for i in x.get("loc", ())) or "contract"
            msg = "field not allowed in a contract" if x.get("type") == "extra_forbidden" else x.get("msg", "")
            parts.append(f"{loc}: {msg}")
        return "invalid contract: " + "; ".join(parts)
    return f"invalid contract: {e}"
