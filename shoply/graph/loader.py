"""Build the starting graph from the supplied YAML definitions."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import ValidationError

from shoply.contracts import Edge, MetricNode

from .graph import DIMS, POSSIBLE_CAUSE, GraphValidationError, Issue, MetricGraph

DATA_DIR = Path(__file__).parent / "data"
_NODE_FIELDS = set(MetricNode.model_fields)


def _read(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def node_from_raw(raw: dict[str, Any]) -> tuple[MetricNode, dict[str, Any]]:
    """Supplied definition -> (MetricNode, fields the contract has no slot for)."""
    raw = dict(raw)
    fields = {k: raw.pop(k) for k in list(raw) if k in _NODE_FIELDS}
    if "sql" in raw:
        fields["value_sql"] = raw.pop("sql")
    for side in ("numerator", "denominator"):          # cross_table: keep each side's table in extra
        if side in raw:
            fields[f"{side}_sql"] = raw[side]["sql"]
    if fields.get("good_direction") not in (None, "up", "down"):
        raw["good_direction"] = fields.pop("good_direction")   # e.g. two_sided
    return MetricNode(**fields), raw


def load_graph(data_dir: Optional[Path] = None, breakdowns: bool = True) -> MetricGraph:
    """Starting graph: metrics.yaml + formulas.yaml + causal.yaml + dimensions.yaml.

    Breakdown edges come from each metric's declared dimensions; context edges service:<name> -> metric
    from each metric's `services`.
    Raises GraphValidationError listing every problem found.
    """
    d = Path(data_dir or DATA_DIR)
    graph = MetricGraph(_read(d / "dimensions.yaml"))
    issues: list[Issue] = [Issue("invalid_metric", f"dimensions.yaml: unknown dimension {k!r}", k)
                           for k in graph.dimension_values if k not in DIMS]

    def attempt(label: str, fn, *args, **kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except GraphValidationError as e:
            issues.extend(e.issues)
        except (ValidationError, KeyError, TypeError) as e:
            code = "invalid_metric" if label.startswith("metric") else "invalid_relationship"
            issues.append(Issue(code, f"{label}: {' '.join(str(e).split())}", label))

    def add_metric(raw: dict[str, Any]) -> None:
        node, extra = node_from_raw(raw)
        graph.add_metric(node, extra=extra)
        if breakdowns:
            graph.add_edges(graph.breakdown_edges(node))
        graph.add_edges(graph.context_edges(node))

    for raw in _read(d / "metrics.yaml"):
        attempt(f"metric {raw.get('id')!r}", add_metric, raw)
    for f in _read(d / "formulas.yaml"):
        attempt(f"formula {f.get('output')!r}", lambda f=f: graph.add_formula(f["output"], f["expr"]))
    for c in _read(d / "causal.yaml"):
        attempt(f"possible cause {c.get('src')}->{c.get('dst')}", lambda c=c: graph.add_edge(Edge(type=POSSIBLE_CAUSE, **c)))

    issues += graph.validate()
    if issues:
        raise GraphValidationError(list(dict.fromkeys(issues)))
    return graph


def load_held_back(data_dir: Optional[Path] = None) -> dict[str, tuple[MetricNode, dict[str, Any]]]:
    """Metrics withheld from the starting graph (added live through the extender). Never auto-registered."""
    return {raw["id"]: node_from_raw(raw) for raw in _read(Path(data_dir or DATA_DIR) / "held_back_metrics.yaml")}
