from psycopg.types.json import Jsonb

from eye.graph import attach_measures, calculation_activities, put_edge, put_node
from eye.model import ask


CURATOR_PROMPT = """You curate metric definitions for a marketplace monitoring graph.
Use only the provided metric, observed graph and existing metric definitions.
Choose a small number of useful target nodes. Return a JSON object like
{"relationships":[{"target":"node identifier","kind":"measures","reason":"brief evidence"}]}.
Use measures for an activity or process the metric quantifies. Use depends_on
for a plausible operational dependency, or may_influence for a plausible downstream
effect. Mark uncertainty in your reasoning. Do not assert causal or statistical proof,
invent node identifiers, or propose arithmetic or source edges. The application
already attaches the metric to the raw activities named in its calculation.
Use observed process nodes, transitions or related metrics for additional context."""

ALLOWED_TARGETS = {
    "measures": {"activity", "process"},
    "depends_on": {"activity", "process", "metric"},
    "may_influence": {"activity", "process", "metric"},
}


def calculation_components(calculation):
    if calculation["op"] not in {"ratio", "share"}:
        return {}
    return {
        "numerator": (calculation.get("activity"), calculation.get("where") or {}),
        "denominator": (
            calculation.get("denominator_activity") or calculation.get("activity"),
            calculation.get("denominator_where") or {},
        ),
    }


def curate(db, metric, model=None):
    definition = {
        "id": metric["id"],
        "name": metric["name"],
        "definition": metric["definition"],
        "calculation": metric["calculation"],
        "dimensions": metric.get("dimensions", []),
        "access": metric.get("access", {}),
        "direction": metric.get("direction", "both"),
    }
    calculation = definition["calculation"]
    metric_id = definition["id"]
    node_id = f"metric:{metric_id}"
    nodes = {
        node["id"]: node for node in db.execute(
            "select id, kind, label, properties from graph_nodes where kind <> 'source' order by id"
        ).fetchall() if node["id"] != node_id
    }
    existing = db.execute(
        "select id, name, definition, calculation, dimensions, access from metrics where id <> %s order by id",
        (metric_id,),
    ).fetchall()
    context = {
        "new_metric": definition,
        "nodes": [{"id": node["id"], "kind": node["kind"], "label": node["label"]}
                  for node in nodes.values()],
        "existing_metrics": existing,
        "workflow": db.execute(
            """select source, target, kind from graph_edges
            where provenance = 'pm4py.ocdfg' order by source, target, kind"""
        ).fetchall(),
    }
    proposal, provenance = ask(model, "curation", CURATOR_PROMPT, context)

    db.execute(
        """insert into metrics (id, name, definition, calculation, dimensions, access, direction)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (id) do update set name = excluded.name, definition = excluded.definition,
        calculation = excluded.calculation, dimensions = excluded.dimensions,
        access = excluded.access, direction = excluded.direction""",
        (metric_id, definition["name"], definition["definition"], Jsonb(calculation),
         definition["dimensions"], Jsonb(definition["access"]), definition["direction"]),
    )
    put_node(db, node_id, "metric", definition["name"], {
        "metric_id": metric_id, "definition": definition["definition"],
        "dimensions": definition["dimensions"],
    })
    db.execute(
        "delete from graph_edges where source = %s and provenance in ('curator.llm', 'calculation')",
        (node_id,),
    )
    attach_measures(db, metric_id, calculation, nodes)
    sources = calculation_activities(calculation)
    if not sources:
        sources = {"events": None}
    for activity in sources:
        source = f"source:{activity}"
        label = activity.replace("_", " ").capitalize()
        props = {"table": "events"}
        if activity != "events":
            label += " events"
            props["activity"] = activity
        put_node(db, source, "source", label, props)
        put_edge(db, node_id, source, "sourced_from", "calculation", definition["access"])
    if calculation["op"] == "completeness":
        put_node(db, "source:windows", "source", "Closed-window manifests", {"table": "windows"})
        put_edge(db, node_id, "source:windows", "sourced_from", "calculation",
                 {"column": "expected_events"})

    components = calculation_components(calculation)
    for other in existing:
        candidate = other["calculation"]
        if (candidate.get("op") != "count" or candidate.get("distinct") or
                not set(definition["dimensions"]).issubset(other["dimensions"])):
            continue
        selector = candidate.get("activity"), candidate.get("where") or {}
        roles = [role for role, component in components.items() if component == selector]
        if roles:
            put_edge(db, node_id, f"metric:{other['id']}", "calculated_from", "calculation",
                     {"roles": roles})

    relationships = (proposal or {}).get("relationships")
    used_model = False
    for relation in relationships[:8] if isinstance(relationships, list) else ():
        if not isinstance(relation, dict):
            continue
        target, kind, reason = relation.get("target"), relation.get("kind"), relation.get("reason")
        if not all(isinstance(field, str) for field in (target, kind, reason)):
            continue
        if target not in nodes or nodes[target]["kind"] not in ALLOWED_TARGETS.get(kind, ()):
            continue
        if kind == "measures" and target in {f"activity:{activity}" for activity in sources}:
            continue
        put_edge(db, node_id, target, kind, "curator.llm", {
            "hypothesis": True, "reason": reason, **provenance,
        })
        used_model = True

    return {
        "metric": definition,
        "relationships": db.execute(
            """select source, target, kind, provenance, properties, confidence
            from graph_edges where source = %s order by kind, target""",
            (node_id,),
        ).fetchall(),
        "reasoning": "llm" if used_model else "calculation",
        **(provenance if used_model else {}),
    }
