"""Graph export for visualisation: JSON (nodes + links), Graphviz DOT, Mermaid."""
from __future__ import annotations

import json
import re
from typing import Any

from .graph import BREAKDOWN, CONTEXT, FORMULA, POSSIBLE_CAUSE, MetricGraph

_LABEL = {FORMULA: "FORMULA", BREAKDOWN: "BREAKDOWN", POSSIBLE_CAUSE: "POSSIBLE_CAUSE", CONTEXT: "CONTEXT"}
_DOT_STYLE = {FORMULA: "solid", POSSIBLE_CAUSE: "dashed", CONTEXT: "dotted", BREAKDOWN: "dotted"}
_MERMAID_ARROW = {FORMULA: "-->", POSSIBLE_CAUSE: "-.->", CONTEXT: "-.-", BREAKDOWN: "-.->"}


def _edge_label(e) -> str:
    detail = {FORMULA: e.op, POSSIBLE_CAUSE: e.sign, BREAKDOWN: e.dimension}.get(e.type)
    return f"{_LABEL[e.type]} {detail}" if detail else _LABEL[e.type]


def to_dict(graph: MetricGraph) -> dict[str, Any]:
    """Node-link data: every node and edge attribute, JSON-serialisable."""
    return {
        "directed": True, "multigraph": True, "arrow": "driver -> affected metric",
        "dimensions": graph.dimension_values,
        "nodes": [{**graph.metric(m).model_dump(), **graph.extra(m), "tables": graph.tables(m)} for m in graph.metric_ids()]
        + [{"id": s, "kind": "service"} for s in graph.services()],
        "links": [{**e.model_dump(mode="json"), "source": e.src, "target": e.dst, "relationship": _LABEL[e.type]}
                  for e in graph.edges()],
    }


def to_json(graph: MetricGraph, indent: int = 2) -> str:
    return json.dumps(to_dict(graph), indent=indent, ensure_ascii=False)


def _ids(graph: MetricGraph) -> dict[str, str]:
    """DOT/Mermaid identifier per node: metrics keep their id, services get a sanitised id made unique."""
    ids = {m: m for m in graph.metric_ids()}
    used = set(ids)
    for s in graph.services():
        base = re.sub(r"\W", "_", s.replace(":", "__"))
        ids[s] = next(c for c in (base if i == 0 else f"{base}_{i}" for i in range(len(used) + 1)) if c not in used)
        used.add(ids[s])
    return ids


def _shown(e, breakdowns: bool, context: bool) -> bool:
    return (e.type != BREAKDOWN or breakdowns) and (e.type != CONTEXT or context)


def to_dot(graph: MetricGraph, breakdowns: bool = False, context: bool = False) -> str:
    ids = _ids(graph)
    lines = ["digraph shoply_metrics {", "  rankdir=LR;", "  node [shape=box, style=rounded];"]
    for m in (graph.metric(i) for i in graph.metric_ids()):
        name = m.name.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'  {m.id} [label="{name}\\n{m.id} [{m.unit}]"];')
    if context:
        lines += [f'  {ids[s]} [label="{s}", shape=ellipse];' for s in graph.services()]
    for e in graph.edges():
        if _shown(e, breakdowns, context):
            lines.append(f'  {ids[e.src]} -> {ids[e.dst]} [label="{_edge_label(e)}", style={_DOT_STYLE[e.type]}];')
    return "\n".join(lines + ["}"])


def to_mermaid(graph: MetricGraph, breakdowns: bool = False, context: bool = False) -> str:
    ids, lines = _ids(graph), ["flowchart LR"]
    for m in (graph.metric(i) for i in graph.metric_ids()):
        name = m.name.replace('"', "#quot;")
        lines.append(f'  {m.id}["{name} ({m.unit})"]')
    if context:
        lines += [f'  {ids[s]}(["{s}"])' for s in graph.services()]
    for e in graph.edges():
        if _shown(e, breakdowns, context):
            lines.append(f'  {ids[e.src]} {_MERMAID_ARROW[e.type]}|"{_edge_label(e)}"| {ids[e.dst]}')
    return "\n".join(lines)


FORMATS = {"json": to_json, "dot": to_dot, "mermaid": to_mermaid}
