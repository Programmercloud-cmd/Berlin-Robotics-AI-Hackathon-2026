"""Investigate anomalous windows with the read-only observation tools."""

import json
from collections import deque
from datetime import datetime, timedelta
from statistics import fmean

import networkx as nx
from psycopg.types.json import Jsonb

from eye.db import state
from eye.model import ask
from eye.observation import observation_tool

HOUR = timedelta(hours=1)
PRIORITY = {
    "measures": 0, "calculated_from": 1, "precedes": 2, "depends_on": 3,
    "part_of": 4, "associated_with": 5, "may_influence": 6, "sourced_from": 7,
}
REPORT_PROMPT = """Investigate this marketplace anomaly using only the provided observations.
Distinguish the observed failure mechanism from an unverified underlying cause.
Use scoped observation evidence and reference the graph or metric evidence by node ID.
Estimate business impact from comparable observations and state the uncertainty.
Return a JSON object with summary (string), likely_causes (list of short strings),
impact (string), and uncertainty (string). Do not invent facts or quantities."""


def jsonb(value):
    return Jsonb(json.loads(json.dumps(value, default=str)))


def metric_node(metric_id):
    return f"metric:{metric_id}"


def add_evidence(db, incident_id, start, kind, payload, node_id=None):
    db.execute(
        """insert into evidence (incident_id, window_start, kind, node_id, payload)
           values (%s, %s, %s, %s, %s)""",
        (incident_id, start, kind, node_id, jsonb(payload)),
    )


def overlaps(left, right):
    return all(right.get(key, value) == value for key, value in left.items())


def related(graph, first, second):
    if not overlaps(first["scope"], second["scope"]):
        return False
    source, target = metric_node(first["metric_id"]), metric_node(second["metric_id"])
    return source == target or target in nx.single_source_shortest_path_length(graph, source, cutoff=4)


def reference(tool, metric_id, start, scope, baseline):
    historic = tool("metric_readings", metric_id=metric_id, start=baseline - timedelta(days=5),
                    end=baseline + HOUR, scope=scope)
    hourly = [row for row in historic if datetime.fromisoformat(row["window_start"]).hour == start.hour]
    periods = hourly or historic
    return fmean(row["value"] for row in periods) if periods else None


def signal(tool, metric_id, start, scope, baseline):
    definition = tool("metric_definition", metric_id=metric_id)
    if not definition:
        return None
    scope = {key: value for key, value in scope.items() if key in definition["dimensions"]}
    readings = tool("metric_readings", metric_id=metric_id, start=start, end=start + HOUR, scope=scope)
    if not readings:
        return None
    observed = readings[-1]["value"]
    expected = reference(tool, metric_id, start, scope, baseline)
    return {
        "metric_id": metric_id, "name": definition["name"], "scope": scope,
        "observed": observed, "expected": expected, "sample_size": readings[-1]["sample_size"],
        "definition": definition["definition"],
    }


def investigate(db, tool, anomaly, incident, model, baseline):
    start, scope = anomaly["window_start"], anomaly["scope"]
    root = metric_node(anomaly["metric_id"])
    add_evidence(db, incident, start, "anomaly", anomaly, root)
    breakdown = tool("metric_breakdown", metric_id=anomaly["metric_id"], window_start=start)
    comparisons = [row for row in breakdown if row["scope"].keys() == scope.keys()]
    comparisons = sorted(comparisons, key=lambda row: -row["sample_size"])[:12]
    add_evidence(db, incident, start, "segment comparison", comparisons, root)
    window_context = tool("company_context", window_start=start)
    add_evidence(db, incident, start, "window context", window_context)

    queue = deque([(root, 0, None)])
    seen, paths, signals, samples = {root}, [], [], []
    while queue and len(paths) < 30:
        node_id, depth, edge = queue.popleft()
        neighborhood = tool("graph_neighbors", node_id=node_id)
        node = neighborhood.get("node")
        if not node:
            continue
        paths.append(node_id)
        if edge:
            add_evidence(db, incident, start, "graph step", edge, node_id)
        if node["kind"] == "metric" and depth:
            reading = signal(tool, node["properties"]["metric_id"], start, scope, baseline)
            if reading:
                signals.append(reading)
                add_evidence(db, incident, start, "metric comparison", reading, node_id)
        if node["kind"] == "activity" and len(samples) < 8:
            rows = tool("supporting_events", window_start=start, scope=scope,
                        activities=[node["properties"]["activity"]], limit=3)
            if rows:
                samples.append({"node": node_id, "events": rows})
                add_evidence(db, incident, start, "observed events", rows, node_id)

        # Expand beyond immediate consequences while the failure mechanism is unclear.
        clear_signal = any(row["expected"] is not None and row["observed"] >= 4 and
                           row["observed"] >= 3 * max(1, row["expected"]) for row in signals)
        if depth == 3 or (depth >= 2 and clear_signal):
            continue
        for link in sorted(neighborhood.get("edges", []), key=lambda row: (
            PRIORITY.get(row["kind"], 9), row["source"], row["target"]
        )):
            neighbor = link["target"] if link["source"] == node_id else link["source"]
            if neighbor not in seen and link["kind"] != "sourced_from":
                seen.add(neighbor)
                queue.append((neighbor, depth + 1, link))

    definition = tool("metric_definition", metric_id=anomaly["metric_id"])
    label = definition["name"]
    scope_label = ", ".join(f"{key} {value}" for key, value in sorted(scope.items())) or "all traffic"
    estimate = abs(anomaly["expected"] - anomaly["observed"])
    is_rate = definition["calculation"]["op"] in {"ratio", "share"}
    if is_rate:
        summary = (f"{label} reached {anomaly['observed']:.1%} versus "
                   f"{anomaly['expected']:.1%} expected in {scope_label}.")
        impact = (f"The {estimate:.1%} rate gap represents about "
                  f"{round(estimate * anomaly['sample_size'])} outcomes versus baseline "
                  f"across {anomaly['sample_size']} observations in this window.")
    else:
        summary = (f"{label} reached {anomaly['observed']:.1f} versus "
                   f"{anomaly['expected']:.1f} expected in {scope_label}.")
        impact = f"Observed volume differed from the baseline by about {estimate:.1f}."
    candidates = sorted(
        (row for row in signals if row["expected"] is not None and
         row["observed"] >= 4 and row["observed"] >= 3 * max(1, row["expected"])),
        key=lambda row: -(row["observed"] / max(1, row["expected"])),
    )
    likely = [
        f"{row['name']} increased to {row['observed']:.1f} compared with a baseline "
        f"of {row['expected']:.1f} ({metric_node(row['metric_id'])})."
        for row in candidates[:2]
    ] or ["The graph shows related activities, but this window does not isolate the failure mechanism."]
    report = {
        "summary": summary, "likely_causes": likely, "impact": impact,
        "uncertainty": "The observations establish the deviation and its scope. The underlying operational cause remains unverified.",
        "affected_scope": scope, "path": paths, "reasoning": "statistical",
    }
    if model:
        answer, provenance = ask(model, "investigation", REPORT_PROMPT, {
            "anomaly": anomaly, "metric": label, "affected_scope": scope,
            "baseline_impact": impact, "metric_evidence": signals, "event_evidence": samples,
            "segment_comparison": comparisons, "context": window_context, "graph_path": paths,
        })
        usable = {key: answer[key] for key in ("summary", "impact", "uncertainty")
                  if isinstance(answer.get(key), str) and answer[key].strip()}
        causes = answer.get("likely_causes")
        if isinstance(causes, list) and all(isinstance(item, str) for item in causes) and causes:
            usable["likely_causes"] = causes
        if usable:
            report.update(usable, reasoning="model", **provenance)
    return report


def track(db, start, anomalies, model=None):
    open_incidents = db.execute("select * from incidents where recovered_at is null").fetchall()
    if not anomalies and not open_incidents:
        return
    tool = observation_tool(db)
    graph = nx.Graph()
    graph.add_nodes_from(row["id"] for row in db.execute("select id from graph_nodes").fetchall())
    graph.add_edges_from((row["source"], row["target"]) for row in db.execute(
        """select source, target from graph_edges
           where kind in ('measures', 'part_of', 'precedes', 'calculated_from', 'depends_on')"""
    ).fetchall())
    pending = list(anomalies)
    health = tool("metric_readings", metric_id="data_completeness_ratio", start=start, end=start + HOUR)
    good_data = not health or health[-1]["value"] >= 0.97
    for incident in open_incidents:
        anchor = next((row for row in pending if row["metric_id"] == incident["metric_id"]
                       and row["scope"] == incident["scope"]), None)
        if anchor:
            nearby = [row for row in pending if related(graph, anchor, row)]
            for row in nearby:
                add_evidence(db, incident["id"], start, "ongoing anomaly", row, metric_node(row["metric_id"]))
                pending.remove(row)
            db.execute("update incidents set updated_at = %s, healthy_windows = 0 where id = %s",
                       (start, incident["id"]))
            continue

        current = tool("metric_readings", metric_id=incident["metric_id"], start=start,
                       end=start + HOUR, scope=incident["scope"])
        kind = tool("metric_definition", metric_id=incident["metric_id"])["calculation"]["op"]
        adequate = current and (kind not in {"ratio", "share"} or current[-1]["sample_size"] >= 30)
        if not adequate or not good_data:
            db.execute("update incidents set healthy_windows = 0 where id = %s", (incident["id"],))
            continue
        streak = incident["healthy_windows"] + 1
        recovered = start if streak >= 2 else None
        report = incident["report"]
        if recovered:
            report["recovery"] = "The affected segment returned to its baseline range for two observed windows."
        add_evidence(db, incident["id"], start, "recovery observation", current[-1],
                     metric_node(incident["metric_id"]))
        db.execute(
            """update incidents set updated_at = %s, healthy_windows = %s,
               recovered_at = %s, report = %s where id = %s""",
            (start, streak, recovered, jsonb(report), incident["id"]),
        )

    baseline = datetime.fromisoformat(state(db, "baseline_through"))
    while pending:
        anchor = max(pending, key=lambda row: row["score"] * (1 + 0.15 * len(row["scope"])))
        nearby = [row for row in pending if related(graph, anchor, row)]
        incident = db.execute(
            """insert into incidents (metric_id, scope, opened_at, updated_at)
               values (%s, %s, %s, %s) returning id""",
            (anchor["metric_id"], jsonb(anchor["scope"]), start, start),
        ).fetchone()["id"]
        report = investigate(db, tool, anchor, incident, model, baseline)
        for row in nearby:
            pending.remove(row)
            if row != anchor:
                add_evidence(db, incident, start, "related anomaly", row, metric_node(row["metric_id"]))
        db.execute("update incidents set report = %s where id = %s", (jsonb(report), incident))
