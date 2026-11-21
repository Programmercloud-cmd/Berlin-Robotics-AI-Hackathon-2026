"""Investigate anomalous windows with the read-only observation tools."""

import json
import logging
from collections import deque
from datetime import datetime, timedelta
from statistics import fmean

import networkx as nx
from psycopg.types.json import Jsonb

from eye.db import connect, state
from eye.model import ask
from eye.observation import observation_tool

HOUR = timedelta(hours=1)
log = logging.getLogger(__name__)
PRIORITY = {
    "measures": 0, "calculated_from": 1, "precedes": 2, "depends_on": 3,
    "part_of": 4, "associated_with": 5, "may_influence": 6, "sourced_from": 7,
}
REPORT_PROMPT = """Investigate this marketplace anomaly using only the provided observations.
Distinguish the observed failure mechanism from an unverified underlying cause.
Use scoped observation evidence and reference the graph or metric evidence by node ID.
Use the earliest observed funnel break when available. Customer geography does
not establish a hosting location. Name a hosting location only when an observed
event explicitly identifies it.
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
    historic = [row for row in historic if row["value"] is not None]
    hourly = [row for row in historic if datetime.fromisoformat(row["window_start"]).hour == start.hour]
    periods = hourly or historic
    if periods and all(row["details"].get("denominator") for row in periods):
        return (sum(row["details"]["numerator"] for row in periods) /
                sum(row["details"]["denominator"] for row in periods))
    return fmean(row["value"] for row in periods) if periods else None


def signal(tool, metric_id, start, scope, baseline):
    definition = tool("metric_definition", metric_id=metric_id)
    if not definition:
        return None
    scope = {key: value for key, value in scope.items() if key in definition["dimensions"]}
    readings = tool("metric_readings", metric_id=metric_id, start=start, end=start + HOUR, scope=scope)
    if not readings or readings[-1]["value"] is None:
        return None
    observed = readings[-1]["value"]
    expected = reference(tool, metric_id, start, scope, baseline)
    return {
        "metric_id": metric_id, "name": definition["name"], "scope": scope,
        "observed": observed, "expected": expected, "sample_size": readings[-1]["sample_size"],
        "definition": definition["definition"],
    }


def first_funnel_break(db, tool, start, scope, baseline, incident):
    stages = ("sessions", "product_view_sessions", "add_to_cart_sessions", "checkout_sessions")
    previous = None
    for identifier in stages:
        current = signal(tool, identifier, start, scope, baseline)
        if (previous and current and previous["scope"] == current["scope"]
                and previous["observed"] >= 30 and previous["expected"]
                and current["expected"] is not None):
            observed = current["observed"] / previous["observed"]
            expected = current["expected"] / previous["expected"]
            if expected - observed >= 0.10:
                text = (f"{current['name']} was the first measured funnel drop in this scope. "
                        f"{current['observed']:.0f} of {previous['observed']:.0f} reached that step "
                        f"({observed:.1%} versus {expected:.1%} expected). "
                        f"Evidence: {metric_node(previous['metric_id'])}, {metric_node(identifier)}.")
                add_evidence(db, incident, start, "first observable break",
                             {"upstream": previous, "downstream": current,
                              "observed_share": observed, "expected_share": expected}, metric_node(identifier))
                return text, identifier, current["scope"]
        previous = current
    return None, None, None


def collect_evidence(db, tool, anomaly, incident, baseline):
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

    first_break, stage, break_scope = first_funnel_break(db, tool, start, scope, baseline, incident)
    page_errors = []
    if stage == "product_view_sessions":
        activity = "activity:product_page_load_failed"
        if tool("graph_neighbors", node_id=activity).get("node"):
            page_errors = tool("supporting_events", window_start=start, scope=break_scope,
                               activities=["product_page_load_failed"], limit=3)
            if page_errors:
                samples.append({"node": activity, "events": page_errors})
                add_evidence(db, incident, start, "observed events", page_errors, activity)

    definition = tool("metric_definition", metric_id=anomaly["metric_id"])
    label = definition["name"]
    scope_label = ", ".join(f"{key} {value}" for key, value in sorted(scope.items())) or "all traffic"
    estimate = abs(anomaly["expected"] - anomaly["observed"])
    is_rate = (definition["calculation"]["op"] in {"ratio", "share"}
               or definition["access"].get("unit") == "ratio")
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
    if page_errors:
        example = page_errors[0]
        status = example["attrs"].get("http_status")
        likely = [
            f"Product-page requests failed before a successful view. "
            f"Observed event {example['id']} recorded HTTP {status} ({activity})."
        ]
    report = {
        "summary": summary, "likely_causes": likely, "impact": impact,
        "uncertainty": "The observations establish the deviation and its scope. The underlying operational cause remains unverified.",
        "affected_scope": scope, "path": paths, "reasoning": "statistical",
    }
    if first_break:
        report["first_observable_break"] = first_break
        report["uncertainty"] = "The evidence shows where the funnel broke. The underlying cause and hosting location remain unverified."
    context = {
        "anomaly": anomaly, "metric": label, "affected_scope": scope,
        "baseline_impact": impact, "metric_evidence": signals, "event_evidence": samples,
        "segment_comparison": comparisons, "context": window_context, "graph_path": paths,
        "first_observable_break": first_break,
    }
    return report, context


def resolve_report(model, context, fallback):
    answer, provenance = ask(model, "investigation", REPORT_PROMPT, context)
    frankfurt_observed = any(
        "frankfurt" in str(row.get("attrs", {}).get(field, "")).casefold()
        for sample in context.get("event_evidence", ()) for row in sample.get("events", ())
        for field in ("hosting_location", "hosting_region", "datacenter", "edge_location")
    )

    def supported(text):
        return (isinstance(text, str) and bool(text.strip())
                and (frankfurt_observed or "frankfurt" not in text.casefold()))

    usable = {key: answer[key] for key in ("summary", "impact", "uncertainty")
              if supported(answer.get(key))}
    causes = answer.get("likely_causes")
    if isinstance(causes, list) and all(isinstance(item, str) for item in causes) and causes:
        supported_causes = [cause for cause in causes if supported(cause)]
        if supported_causes:
            usable["likely_causes"] = supported_causes
    if usable:
        return {**fallback, **usable, "reasoning": "model", **provenance}
    return fallback


def finish_next_report(dsn, model, stop):
    with connect(dsn) as db:
        # A session lock permits one report worker per database, without holding a
        # transaction or an incident row lock during the provider request.
        if not db.execute("select pg_try_advisory_lock(hashtext('eye.pending_reports')) as claimed"
                          ).fetchone()["claimed"]:
            return False
        job = db.execute("select * from pending_reports order by incident_id limit 1").fetchone()
        db.commit()
        if not job or stop.is_set():
            return False
        try:
            report = resolve_report(model, job["context"], job["fallback"])
        except Exception as exc:
            log.warning("Investigation provider failed (%s). Using the statistical report.", type(exc).__name__)
            report = job["fallback"]
        if stop.is_set():
            return False
        db.execute("update incidents set report = report || %s where id = %s",
                   (jsonb(report), job["incident_id"]))
        db.execute("delete from pending_reports where incident_id = %s", (job["incident_id"],))
        return True


def watch_reports(dsn, stop, model, poll=0.5):
    while not stop.is_set():
        try:
            if finish_next_report(dsn, model, stop):
                continue
        except Exception:
            log.exception("Pending report could not be saved and will be retried")
            stop.wait(2)
        stop.wait(poll)


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
    good_data = not health or (health[-1]["value"] is not None and health[-1]["value"] >= 0.97)
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
        metric = tool("metric_definition", metric_id=incident["metric_id"])
        ratio = metric["calculation"]["op"] in {"ratio", "share"} or metric["access"].get("unit") == "ratio"
        adequate = (current and current[-1]["value"] is not None and
                    (not ratio or current[-1]["sample_size"] >= 30))
        if not adequate or not good_data:
            db.execute("update incidents set healthy_windows = 0 where id = %s", (incident["id"],))
            continue
        streak = incident["healthy_windows"] + 1
        recovered = start if streak >= 2 else None
        recovery = {}
        if recovered:
            recovery["recovery"] = "The affected segment returned to its baseline range for two observed windows."
        add_evidence(db, incident["id"], start, "recovery observation", current[-1],
                     metric_node(incident["metric_id"]))
        db.execute(
            """update incidents set updated_at = %s, healthy_windows = %s,
               recovered_at = %s, report = report || %s where id = %s""",
            (start, streak, recovered, jsonb(recovery), incident["id"]),
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
        report, context = collect_evidence(db, tool, anchor, incident, baseline)
        for row in nearby:
            pending.remove(row)
            if row != anchor:
                add_evidence(db, incident, start, "related anomaly", row, metric_node(row["metric_id"]))
        if model:
            db.execute("insert into pending_reports values (%s, %s, %s)",
                       (incident, jsonb(context), jsonb(report)))
            report = {key: report[key] for key in
                      ("summary", "affected_scope", "path", "first_observable_break") if key in report}
        db.execute("update incidents set report = %s where id = %s", (jsonb(report), incident))
