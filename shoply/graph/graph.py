"""MetricGraph: metrics as nodes, typed relationships as edges (driver -> affected metric)."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Iterable, Optional, get_args

import networkx as nx

from shoply.contracts import Dim, Edge, MetricNode, Segment

# Relationship type -> value of contracts.Edge.type
FORMULA, BREAKDOWN, POSSIBLE_CAUSE, CONTEXT = "formula", "breakdown", "causal", "context"
RELATIONSHIP_TYPES = {"FORMULA": FORMULA, "BREAKDOWN": BREAKDOWN,
                      "POSSIBLE_CAUSE": POSSIBLE_CAUSE, "CONTEXT": CONTEXT}
DIMS: tuple[str, ...] = get_args(Dim)
_OPS = {ast.Mult: "product", ast.Div: "ratio", ast.Add: "sum", ast.Sub: "difference"}
_SYMBOL = {ast.Mult: "*", ast.Div: "/", ast.Add: "+", ast.Sub: "-"}
_NEUTRAL = {ast.Mult: "*", ast.Div: "*", ast.Add: "+", ast.Sub: "+"}
_TYPES = set(RELATIONSHIP_TYPES.values())
_TRAVERSE = _TYPES - {CONTEXT}             # default for multi-hop walks: services are reached only on request
SERVICE_PREFIX = "service:"


@dataclass(frozen=True)
class Issue:
    code: str          # missing_metric | invalid_relationship | duplicate_id | incompatible_units | invalid_metric
    message: str
    subject: str = ""  # metric id or "src->dst"

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class GraphValidationError(ValueError):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("; ".join(str(i) for i in issues))


def rel_type(name: str) -> str:
    """Accepts 'POSSIBLE_CAUSE' or 'causal'; returns the contracts.Edge.type value."""
    value = RELATIONSHIP_TYPES.get(name, name)
    if value not in _TYPES:
        raise ValueError(f"unknown relationship type {name!r}; expected one of {sorted(RELATIONSHIP_TYPES)}")
    return value


def _parse(expr: str) -> ast.expr:
    try:
        return ast.parse(expr, mode="eval").body
    except SyntaxError as e:
        raise ValueError(f"cannot parse formula {expr!r}: {e.msg}") from None


def parse_formula(expr: str) -> dict[str, str]:
    """Metric names in expr (left to right) -> operator of the binary operation that applies it.

    The right operand gets the operator itself (+ - * /); the left operand gets + or *.
    `a + b - c` -> {a: +, b: +, c: -};  `a / b` -> {a: *, b: /}. Raises ValueError.
    """
    body = _parse(expr)
    inputs: dict[str, str] = {}

    def walk(node: ast.AST, operator: str) -> None:
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            walk(node.left, _NEUTRAL[type(node.op)])
            walk(node.right, _SYMBOL[type(node.op)])
        elif isinstance(node, ast.Name):
            inputs.setdefault(node.id, operator)
        elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            pass
        else:
            raise ValueError(f"unsupported syntax in formula {expr!r}")

    walk(body, "+")
    if not inputs:
        raise ValueError(f"formula {expr!r} references no metric")
    return inputs


def formula_op(expr: str) -> str:
    """product | ratio | sum | difference, from the top-level operation of expr (a lone name is a sum)."""
    body = _parse(expr)
    return _OPS[type(body.op)] if isinstance(body, ast.BinOp) and type(body.op) in _OPS else "sum"


def _eur_power(expr: str, units: dict[str, str]) -> int:
    """Power of EUR in expr. Counts, shares and scores are plain numbers. Raises ValueError on eur +/- non-eur."""
    def walk(node: ast.AST) -> int:
        if isinstance(node, ast.Name):
            return 1 if units[node.id] == "eur" else 0
        if isinstance(node, ast.Constant):
            return 0
        left, right = walk(node.left), walk(node.right)
        if isinstance(node.op, ast.Mult):
            return left + right
        if isinstance(node.op, ast.Div):
            return left - right
        if left != right:
            raise ValueError("adds or subtracts EUR and non-EUR terms")
        return left

    return walk(ast.parse(expr, mode="eval").body)


def _edge_key(e: Edge) -> str:
    return f"{e.type}:{e.dimension}" if e.type == BREAKDOWN else e.type


def service_node(name: str) -> str:
    return name if name.startswith(SERVICE_PREFIX) else f"{SERVICE_PREFIX}{name}"


class MetricGraph:
    """Node = MetricNode (+ supplied fields the contract has no slot for). Edge = contracts.Edge."""

    def __init__(self, dimension_values: Optional[dict[str, list[str]]] = None):
        self._g = nx.MultiDiGraph()
        self.dimension_values: dict[str, list[str]] = dict(dimension_values or {})

    # ---------- registration ----------
    def add_metric(self, node: MetricNode, edges: Iterable[Edge] = (), extra: Optional[dict[str, Any]] = None) -> None:
        """Validate, then register the metric and its edges. All or nothing."""
        trial = self.copy()
        issues = trial._check_metric(node)
        if not issues:
            trial._g.add_node(node.id, metric=node, extra=dict(extra or {}))
            for e in edges:
                found = trial._check_edge(e)
                issues += found
                if not found:
                    trial._insert(e)
            issues = issues or trial._incomplete_formulas()
        if issues:
            raise GraphValidationError(issues)
        self._g = trial._g

    def add_edge(self, edge: Edge) -> None:
        self.add_edges([edge])

    def add_edges(self, edges: Iterable[Edge]) -> None:
        trial, issues = self.copy(), []
        for e in edges:
            if e.type == CONTEXT and e.src.startswith(SERVICE_PREFIX):
                trial.add_service(e.src)
            found = trial._check_edge(e)
            issues += found
            if not found:
                trial._insert(e)
        issues = issues or trial._incomplete_formulas()
        if issues:
            raise GraphValidationError(issues)
        self._g = trial._g

    def add_service(self, name: str) -> str:
        """Register a service node (id `service:<name>`); context edges link it to the metrics it can move."""
        sid = service_node(name)
        if sid not in self._g:
            self._g.add_node(sid, service=sid[len(SERVICE_PREFIX):])
        return sid

    def _insert(self, e: Edge) -> None:
        attrs = {"operator": parse_formula(e.expr)[e.src]} if e.type == FORMULA else {}
        self._g.add_edge(e.src, e.dst, key=_edge_key(e), edge=e, **attrs)

    def context_edges(self, node: MetricNode) -> list[Edge]:
        """One context edge service:<name> -> metric per service listed on the metric."""
        return [Edge(src=service_node(s), dst=node.id, type=CONTEXT) for s in node.services]

    def add_formula(self, output: str, expr: str, **edge_fields: Any) -> list[Edge]:
        """Every metric named in expr becomes a formula edge input -> output."""
        edges = self.formula_edges(output, expr, **edge_fields)
        self.add_edges(edges)
        return edges

    @staticmethod
    def formula_edges(output: str, expr: str, **edge_fields: Any) -> list[Edge]:
        try:
            inputs, op = parse_formula(expr), formula_op(expr)
        except ValueError as e:
            raise GraphValidationError([Issue("invalid_relationship", str(e), output)]) from None
        return [Edge(src=src, dst=output, type=FORMULA, expr=expr, op=op, **edge_fields) for src in inputs]

    @staticmethod
    def breakdown_edges(node: MetricNode, dimensions: Optional[Iterable[str]] = None) -> list[Edge]:
        """One breakdown edge per dimension: the metric's slices drive its total."""
        return [Edge(src=node.id, dst=node.id, type=BREAKDOWN, dimension=d)
                for d in (node.dimensions if dimensions is None else dimensions)]

    def copy(self) -> "MetricGraph":
        other = MetricGraph(self.dimension_values)
        other._g = self._g.copy()
        return other

    # ---------- validation ----------
    def _check_metric(self, node: MetricNode) -> list[Issue]:
        issues = []
        if node.id in self._g:
            issues.append(Issue("duplicate_id", f"metric {node.id!r} is already registered", node.id))
        if not node.id.isidentifier():
            issues.append(Issue("invalid_metric", f"metric id {node.id!r} is not a valid identifier", node.id))
        if len(set(node.dimensions)) != len(node.dimensions):
            issues.append(Issue("invalid_metric", f"{node.id}: repeated dimension", node.id))
        if (node.kind == "count") != (node.unit == "count") and node.kind != "sum":
            issues.append(Issue("incompatible_units", f"{node.id}: kind {node.kind!r} cannot have unit {node.unit!r}", node.id))
        if node.kind == "sum" and node.unit not in ("eur", "count"):
            issues.append(Issue("incompatible_units", f"{node.id}: kind 'sum' cannot have unit {node.unit!r}", node.id))
        if node.unit == "score" and node.kind != "gauge":
            issues.append(Issue("incompatible_units", f"{node.id}: unit 'score' needs kind 'gauge'", node.id))
        return issues

    def _check_edge(self, e: Edge) -> list[Issue]:
        subject = f"{e.src}->{e.dst}"
        ok_src = e.src in self if e.type != CONTEXT else e.src in self._g
        missing = [Issue("missing_metric", f"{e.type} edge {subject}: metric {m!r} is not registered", m)
                   for m, ok in dict.fromkeys(((e.src, ok_src), (e.dst, e.dst in self))) if not ok]
        if missing:
            return missing
        bad = lambda msg: Issue("invalid_relationship", f"{e.type} edge {subject}: {msg}", subject)  # noqa: E731
        issues = []
        if self._g.has_edge(e.src, e.dst, _edge_key(e)):
            issues.append(bad("already exists"))
        if e.type != BREAKDOWN and e.src == e.dst:
            issues.append(bad("a metric cannot drive itself"))
        if not 0 <= e.lag_h[0] <= e.lag_h[1]:
            issues.append(bad(f"lag_h {e.lag_h} must be 0 <= min <= max"))
        if not (0 <= e.prior <= 1 and 0 <= e.confidence <= 1):
            issues.append(bad("prior and confidence must be within 0..1"))
        if e.type != FORMULA and (e.expr or e.op):
            issues.append(bad("expr and op belong to formula edges only"))
        if e.type != BREAKDOWN and e.dimension:
            issues.append(bad("dimension belongs to breakdown edges only"))
        if e.type != POSSIBLE_CAUSE and e.sign:
            issues.append(bad("sign belongs to possible-cause edges only"))

        if e.type == FORMULA:
            issues += self._check_formula_edge(e, bad)
        elif e.type == BREAKDOWN:
            if e.src != e.dst:
                issues.append(bad("a breakdown links a metric's slices to the same metric"))
            if not e.dimension:
                issues.append(bad("needs a dimension"))
            elif e.dimension not in self.metric(e.dst).dimensions:
                issues.append(bad(f"{e.dst} is not available by {e.dimension!r}"))
        elif e.type == POSSIBLE_CAUSE and not e.sign:
            issues.append(bad("needs a sign"))
        return issues

    def _check_formula_edge(self, e: Edge, bad) -> list[Issue]:
        if not e.expr or not e.op:
            return [bad("needs expr and op")]
        try:
            inputs = parse_formula(e.expr)
        except ValueError as err:
            return [bad(str(err))]
        issues = []
        if e.src not in inputs:
            issues.append(bad(f"{e.src!r} does not appear in {e.expr!r}"))
        elif formula_op(e.expr) != e.op:
            issues.append(bad(f"op {e.op!r} does not match {e.expr!r} (expected {formula_op(e.expr)!r})"))
        current = self.formula(e.dst)
        if current and current["expr"] != e.expr:
            issues.append(bad(f"{e.dst} already has the formula {current['expr']!r}"))
        declared = self.extra(e.dst).get("expr")
        if declared and declared != e.expr:
            issues.append(bad(f"{e.dst} is defined as {declared!r}"))
        if e.src != e.dst and e.src in self.downstream(e.dst, types=[FORMULA]):
            issues.append(bad("creates a formula cycle"))
        unknown = [m for m in inputs if m not in self]
        issues += [Issue("missing_metric", f"formula of {e.dst}: metric {m!r} is not registered", m) for m in unknown]
        if not unknown:
            units = {m: self.metric(m).unit for m in inputs}
            want = 1 if self.metric(e.dst).unit == "eur" else 0
            try:
                got = _eur_power(e.expr, units)
                if got != want:
                    issues.append(Issue("incompatible_units", f"formula {e.dst} = {e.expr}: result is "
                                        f"EUR^{got} but {e.dst} has unit {self.metric(e.dst).unit!r}", e.dst))
            except ValueError as err:
                issues.append(Issue("incompatible_units", f"formula {e.dst} = {e.expr}: {err}", e.dst))
        return issues

    def _incomplete_formulas(self) -> list[Issue]:
        """A formula needs one edge from every metric named in its expr."""
        issues = []
        for mid in self.metric_ids():
            f = self.formula(mid)
            absent = sorted(set(parse_formula(f["expr"])) - set(f["inputs"])) if f else []
            if absent:
                issues.append(Issue("invalid_relationship", f"formula of {mid}: no formula edge from {absent}", mid))
        return issues

    def validate(self) -> list[Issue]:
        """Re-check the whole graph. Empty list = consistent."""
        issues: list[Issue] = []
        empty = MetricGraph(self.dimension_values)
        for mid in self.metric_ids():
            issues += empty._check_metric(self.metric(mid))
        for e in self.edges():
            rest = self.copy()
            rest._g.remove_edge(e.src, e.dst, _edge_key(e))
            issues += rest._check_edge(e)
        issues += self._incomplete_formulas()
        for mid in self.metric_ids():
            f = self.formula(mid)
            declared = self.extra(mid).get("expr")
            if declared and not f:
                issues.append(Issue("invalid_relationship", f"{mid} is defined as {declared!r} but has no formula edges", mid))
        return list(dict.fromkeys(issues))

    # ---------- retrieval ----------
    @classmethod
    def load(cls, data_dir: Any = None, breakdowns: bool = True) -> "MetricGraph":
        """Starting graph from the YAML definitions (see loader.load_graph)."""
        from .loader import load_graph
        return load_graph(data_dir, breakdowns=breakdowns)

    def __contains__(self, metric_id: str) -> bool:
        return metric_id in self._g and "metric" in self._g.nodes[metric_id]

    def __len__(self) -> int:
        """Number of metrics (service nodes are not counted)."""
        return len(self.metric_ids())

    def metric_ids(self) -> list[str]:
        return [n for n, d in self._g.nodes(data=True) if "metric" in d]

    def services(self) -> list[str]:
        """Service node ids (`service:<name>`)."""
        return [n for n, d in self._g.nodes(data=True) if "service" in d]

    def _node(self, node_id: str) -> None:
        if node_id not in self._g:
            raise KeyError(f"unknown metric {node_id!r}")

    @property
    def nx(self) -> nx.MultiDiGraph:
        return self._g

    def metric(self, metric_id: str) -> MetricNode:
        if metric_id not in self:
            raise KeyError(f"unknown metric {metric_id!r}")
        return self._g.nodes[metric_id]["metric"]

    def extra(self, metric_id: str) -> dict[str, Any]:
        """Supplied fields without a MetricNode slot: expr, note, cross-table sources, two-sided direction."""
        self.metric(metric_id)
        return self._g.nodes[metric_id]["extra"]

    def good_direction(self, metric_id: str) -> str:
        return self.extra(metric_id).get("good_direction", self.metric(metric_id).good_direction)

    def metrics(self, *, unit: Optional[str] = None, kind: Optional[str] = None, table: Optional[str] = None,
                dimension: Optional[str] = None, service: Optional[str] = None, status: Optional[str] = None,
                text: Optional[str] = None) -> list[MetricNode]:
        def keep(m: MetricNode) -> bool:
            return ((unit is None or m.unit == unit) and (kind is None or m.kind == kind)
                    and (table is None or table in self.tables(m.id))
                    and (dimension is None or dimension in m.dimensions)
                    and (service is None or service in m.services) and (status is None or m.status == status)
                    and (text is None or text.lower() in f"{m.id} {m.name} {m.description}".lower()))
        return [m for m in (self.metric(i) for i in self.metric_ids()) if keep(m)]

    def tables(self, metric_id: str) -> list[str]:
        """Physical tables the metric reads (both sides of a cross-table ratio)."""
        x = self.extra(metric_id)
        sides = [x[k]["table"] for k in ("numerator", "denominator") if k in x]
        return sides or [self.metric(metric_id).table]

    def edges(self, type: Optional[str] = None, *, src: Optional[str] = None, dst: Optional[str] = None,
              status: Optional[str] = None) -> list[Edge]:
        t = rel_type(type) if type else None
        return [e for _, _, e in self._g.edges(data="edge")
                if (t is None or e.type == t) and (src is None or e.src == src)
                and (dst is None or e.dst == dst) and (status is None or e.status == status)]

    def drivers(self, metric_id: str, types: Optional[Iterable[str]] = None) -> list[Edge]:
        """Edges pointing at the metric (excluding its own breakdowns)."""
        return self._incident(metric_id, types, incoming=True)

    def affected(self, metric_id: str, types: Optional[Iterable[str]] = None) -> list[Edge]:
        """Edges leaving the metric (excluding its own breakdowns)."""
        return self._incident(metric_id, types, incoming=False)

    driven = affected

    @staticmethod
    def _wanted(types: Optional[Iterable[str]], default: set[str] = _TYPES) -> set[str]:
        return ({rel_type(t) for t in types} if types is not None else None) or default

    def _incident(self, metric_id: str, types: Optional[Iterable[str]], incoming: bool) -> list[Edge]:
        self._node(metric_id)
        wanted = self._wanted(types)
        found = self._g.in_edges(metric_id, data="edge") if incoming else self._g.out_edges(metric_id, data="edge")
        return [e for _, _, e in found if e.type in wanted and e.src != e.dst]

    def upstream(self, metric_id: str, types: Optional[Iterable[str]] = None, depth: Optional[int] = None) -> dict[str, int]:
        """Every metric that can drive this one -> number of hops."""
        return self._reach(metric_id, types, depth, incoming=True)

    def downstream(self, metric_id: str, types: Optional[Iterable[str]] = None, depth: Optional[int] = None) -> dict[str, int]:
        """Every metric this one can affect -> number of hops."""
        return self._reach(metric_id, types, depth, incoming=False)

    def _reach(self, metric_id: str, types: Optional[Iterable[str]], depth: Optional[int], incoming: bool) -> dict[str, int]:
        types = self._wanted(types, _TRAVERSE)
        seen, frontier, hops = {metric_id: 0}, [metric_id], 0
        while frontier and (depth is None or hops < depth):
            hops += 1
            step = []
            for m in frontier:
                for e in self._incident(m, types, incoming):
                    nxt = e.src if incoming else e.dst
                    if nxt not in seen:
                        seen[nxt] = hops
                        step.append(nxt)
            frontier = step
        del seen[metric_id]
        return seen

    def paths(self, src: str, dst: str, types: Optional[Iterable[str]] = None, cutoff: Optional[int] = None) -> list[list[Edge]]:
        """All driver -> affected chains from src to dst, shortest first."""
        self._node(src), self._node(dst)
        wanted = self._wanted(types, _TRAVERSE)
        view = nx.MultiDiGraph((u, v, k, d) for u, v, k, d in self._g.edges(keys=True, data=True)
                               if d["edge"].type in wanted and u != v)
        if src not in view or dst not in view:
            return []
        found = [[d["edge"] for d in (view.edges[e] for e in p)] for p in nx.all_simple_edge_paths(view, src, dst, cutoff)]
        return sorted(found, key=len)

    def formula(self, metric_id: str) -> Optional[dict[str, Any]]:
        """{'expr', 'op': top-level operation, 'inputs': {metric: operator (+ - * /)}} or None."""
        edges = self.drivers(metric_id, [FORMULA])
        if not edges:
            return None
        operators = parse_formula(edges[0].expr)
        return {"expr": edges[0].expr, "op": edges[0].op,
                "inputs": {m: operators[m] for m in operators if m in {e.src for e in edges}}}

    def possible_causes(self, metric_id: str) -> list[Edge]:
        """Hypotheses to test, highest prior first. Never arithmetic."""
        return sorted(self.drivers(metric_id, [POSSIBLE_CAUSE]), key=lambda e: -e.prior)

    def context(self, metric_id: str) -> list[Edge]:
        return self.drivers(metric_id, [CONTEXT])

    def breakdowns(self, metric_id: str) -> list[str]:
        """Dimensions the metric can be broken down by."""
        self.metric(metric_id)
        return [e.dimension for e in self.edges(BREAKDOWN, dst=metric_id)]

    def shared_dimensions(self, *metric_ids: str) -> list[str]:
        """Dimensions available on every given metric, in contract order."""
        common = set(DIMS).intersection(*(self.metric(m).dimensions for m in metric_ids))
        return [d for d in DIMS if d in common]

    def shared_dims(self, metric_ids: Iterable[str]) -> list[str]:
        return self.shared_dimensions(*metric_ids)

    def hops(self, a: str, b: str, types: Optional[Iterable[str]] = None, directed: bool = False) -> Optional[int]:
        """Fewest edges between two nodes (None = not connected). directed=True follows driver -> metric only.

        Breakdowns are self-loops and never shorten a path; context edges count when an end is a service.
        """
        self._node(a), self._node(b)
        default = _TYPES if a.startswith(SERVICE_PREFIX) or b.startswith(SERVICE_PREFIX) else _TRAVERSE
        wanted = self._wanted(types, default)
        view = nx.DiGraph() if directed else nx.Graph()
        view.add_nodes_from(self._g)
        view.add_edges_from((u, v) for u, v, e in self._g.edges(data="edge") if e.type in wanted and u != v)
        try:
            return nx.shortest_path_length(view, a, b)
        except nx.NetworkXNoPath:
            return None

    def metric_tree(self, root: str = "contribution", depth: Optional[int] = None) -> dict[str, Any]:
        """Formula decomposition of root as nested dicts, inputs in expr order.

        {"id", "name", "unit", "operator" (how the parent applies it), "op", "expr", "children": [...]}
        """
        def build(mid: str, operator: Optional[str], level: int, seen: frozenset) -> dict[str, Any]:
            m, f = self.metric(mid), self.formula(mid)
            node = {"id": mid, "name": m.name, "unit": m.unit, "operator": operator,
                    "op": f["op"] if f else None, "expr": f["expr"] if f else None, "children": []}
            if f and mid not in seen and (depth is None or level < depth):
                node["children"] = [build(c, o, level + 1, seen | {mid}) for c, o in f["inputs"].items()]
            return node

        return build(root, None, 0, frozenset())

    def segment_issues(self, metric_id: str, segment: Segment) -> list[str]:
        """Why a segment cannot be used with the metric. Empty list = usable."""
        dims, out = self.metric(metric_id).dimensions, []
        for d, v in segment.items():
            if d not in dims:
                out.append(f"{metric_id} is not available by {d!r}")
            elif d in self.dimension_values and v not in self.dimension_values[d]:
                out.append(f"{v!r} is not a value of {d!r}")
        return out

    def subgraph(self, metric_id: str, depth: int = 1, types: Optional[Iterable[str]] = None) -> "MetricGraph":
        """The metric with everything within `depth` hops in either direction."""
        wanted = self._wanted(types, _TRAVERSE)
        keep = {metric_id, *self.upstream(metric_id, wanted, depth), *self.downstream(metric_id, wanted, depth)}
        other = MetricGraph(self.dimension_values)
        other._g.add_nodes_from((n, self._g.nodes[n]) for n in self._g if n in keep)
        other._g.add_edges_from((u, v, k, d) for u, v, k, d in self._g.edges(keys=True, data=True)
                                if u in keep and v in keep and d["edge"].type in wanted)
        return other

    def describe(self, metric_id: str) -> dict[str, Any]:
        """Everything known about one metric, as plain data."""
        m = self.metric(metric_id)
        return {
            **m.model_dump(), **self.extra(metric_id),
            "tables": self.tables(metric_id),
            "formula": self.formula(metric_id),
            "breakdowns": self.breakdowns(metric_id),
            "used_in": sorted({e.dst for e in self.affected(metric_id, [FORMULA])}),
            "possible_causes": [e.model_dump(include={"src", "sign", "lag_h", "prior", "confidence", "status"})
                                for e in self.possible_causes(metric_id)],
            "may_cause": [e.model_dump(include={"dst", "sign", "lag_h", "prior", "confidence", "status"})
                          for e in self.affected(metric_id, [POSSIBLE_CAUSE])],
            "context": [e.src for e in self.context(metric_id)],
        }
