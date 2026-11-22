"""Investigate anomalous windows with the read-only observation tools."""

import json
import logging
from collections import deque
from datetime import datetime
from statistics import fmean

from psycopg.types.json import Jsonb

from eye.db import connect, state
from eye.intervals import CADENCE, HOUR, aligned_history
from eye.investigation_branches import build_branches
from eye.model import ask
from eye.observation import observation_tool
from eye.shared_incidents import COMPLETENESS, cohort_plan, source_plans
from eye.transitions import ALERT_STEPS, BY_ID, POSITIONS, WORKFLOW, Readings, compatible

log = logging.getLogger(__name__)
PRIORITY = {
    "measures": 0, "calculated_from": 1, "precedes": 2, "depends_on": 3,
    "part_of": 4, "associated_with": 5, "may_influence": 6, "sourced_from": 7,
}
REPORT_PROMPT = """Investigate this marketplace anomaly using only the provided observations.
Distinguish the observed failure mechanism from an unverified underlying cause.
Use scoped observation evidence and reference the graph or metric evidence by node ID.
Use the identified transition and its eligible population when available. A smaller
downstream population can coexist with a separate degraded completion rate.
Keep event IDs and raw payloads in supporting evidence. Customer geography does
not establish a hosting location. Name a hosting location only when an observed
event explicitly identifies it.
Estimate business impact from comparable observations and state the uncertainty.
Return a JSON object with summary (string), likely_causes (list of short strings),
impact (string), and uncertainty (string). Do not invent facts or quantities."""


def jsonb(value):
    return Jsonb(json.loads(json.dumps(value, default=str)))


def metric_node(metric_id):
    return f"metric:{metric_id}"


def save_report_snapshot(db, incident, phase, report, telemetry, cutoff):
    db.execute(
        """insert into report_snapshots (incident_id, phase, report, telemetry, observed_through)
           values (%s, %s, %s, %s, %s) on conflict (incident_id, phase) do update
           set report = excluded.report, telemetry = excluded.telemetry,
               observed_through = excluded.observed_through, written_at = excluded.written_at""",
        (incident, phase, jsonb(report), jsonb(telemetry), cutoff),
    )


def add_evidence(db, incident_id, start, kind, payload, node_id=None):
    return db.execute(
        """insert into evidence (incident_id, window_start, kind, node_id, payload)
           values (%s, %s, %s, %s, %s) returning id""",
        (incident_id, start, kind, node_id, jsonb(payload)),
    ).fetchone()["id"]


def reference(tool, metric_id, start, scope, baseline):
    dates = aligned_history(start, baseline)
    historic = tool("metric_readings", metric_id=metric_id, start=dates[-1],
                    end=dates[0] + CADENCE, scope=scope, aligned_to=start)
    periods = [row for row in historic if row["value"] is not None]
    if periods and all(row["details"].get("denominator") for row in periods):
        return (sum(row["details"]["numerator"] for row in periods) /
                sum(row["details"]["denominator"] for row in periods))
    return fmean(row["value"] for row in periods) if periods else None


def signal(tool, metric_id, start, scope, baseline):
    definition = tool("metric_definition", metric_id=metric_id)
    if not definition:
        return None
    scope = {key: value for key, value in scope.items() if key in definition["dimensions"]}
    readings = tool("metric_readings", metric_id=metric_id, start=start, end=start + CADENCE, scope=scope)
    if not readings or readings[-1]["value"] is None:
        return None
    observed = readings[-1]["value"]
    expected = reference(tool, metric_id, start, scope, baseline)
    return {
        "metric_id": metric_id, "name": definition["name"], "scope": scope,
        "observed": observed, "expected": expected, "sample_size": readings[-1]["sample_size"],
        "definition": definition["definition"],
    }


def transition_evidence(db, tool, incident, start, completion, samples, as_of):
    step = BY_ID[completion["id"]]
    add_evidence(db, incident, start, "first observable break", completion, metric_node(step.metric))
    if not step.failure:
        return None
    breakdown = completion["scope"]
    failures = tool("supporting_events", window_start=start, scope=breakdown,
                    activities=[step.failure], limit=3, as_of=as_of)
    if not failures:
        return None
    example = failures[0]
    identity = example["attrs"].get(step.identity)
    requests = tool("supporting_events", window_start=start, scope={**breakdown, step.identity: identity},
                    activities=[step.entry], limit=1, as_of=as_of) if identity else []
    for activity, rows in ((step.entry, requests), (step.failure, failures)):
        if rows:
            node = f"activity:{activity}"
            samples.append({"node": node, "events": rows})
            # The event can already be visible to Eye while graph discovery catches up.
            known = tool("graph_neighbors", node_id=node).get("node")
            add_evidence(db, incident, start, "observed events", rows, node if known else None)
    response = example["attrs"].get("http_status") or example["attrs"].get("response")
    outcome = f" recorded {str(response).replace('_', ' ')}" if response is not None else " recorded failures"
    return f"Observed {step.failure.replace('_', ' ')} events{outcome}."


def collect_evidence(db, tool, anomaly, incident, baseline, completion=None):
    start, scope = anomaly["window_start"], anomaly["scope"]
    as_of = anomaly.get("observed_at")
    root = metric_node(anomaly["metric_id"])
    if "id" in anomaly:
        add_evidence(db, incident, start, "anomaly", anomaly, root)
    breakdown = tool("metric_breakdown", metric_id=anomaly["metric_id"], window_start=start)
    comparisons = [row for row in breakdown if row["scope"].keys() == scope.keys()]
    comparisons = sorted(comparisons, key=lambda row: -row["sample_size"])[:12]
    add_evidence(db, incident, start, "segment comparison", comparisons, root)
    window_context = tool("company_context", window_start=start, as_of=as_of)
    add_evidence(db, incident, start, "window context", window_context)

    queue = deque([(root, 0, None)])
    seen, paths, signals, samples = {root}, [], [], []
    while queue and len(paths) < 8:
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
                        activities=[node["properties"]["activity"]], limit=3, as_of=as_of)
            if rows:
                samples.append({"node": node_id, "events": rows})
                add_evidence(db, incident, start, "observed events", rows, node_id)

        # Keep the initial report local. The branch worker checks upstream telemetry.
        if depth == 1:
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
    observed, expected = anomaly["observed"], anomaly["expected"]
    is_rate = (definition["calculation"]["op"] in {"ratio", "share"}
               or definition["access"].get("unit") == "ratio")
    first_break = transition_failure = None
    if completion:
        label = completion["label"]
        observed, expected = completion["observed"], completion["expected"]
        is_rate = True
        first_break = (f"{completion['completed']} of {completion['eligible']} eligible attempts completed "
                       f"({observed:.1%} versus {expected:.1%} expected).")
        transition_failure = transition_evidence(db, tool, incident, start, completion, samples, as_of)
    estimate = abs(expected - observed)
    if is_rate:
        size = completion["eligible"] if completion else anomaly["sample_size"]
        summary = f"{label} reached {observed:.1%} versus {expected:.1%} expected in {scope_label}."
        direction = "fewer" if observed < expected else "more"
        outcome = "completions" if completion else "outcomes"
        impact = f"About {round(estimate * size)} {direction} {outcome} versus baseline across {size} observations."
    else:
        summary = f"{label} reached {observed:.1f} versus {expected:.1f} expected in {scope_label}."
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
    if transition_failure:
        likely = [transition_failure]
    report = {
        "summary": summary, "likely_causes": likely, "impact": impact,
        "uncertainty": "The observations establish the deviation and its scope. The underlying operational cause remains unverified.",
        "affected_scope": scope, "path": paths, "reasoning": "statistical",
    }
    if first_break:
        report["first_observable_break"] = first_break
        report["uncertainty"] = ("The evidence shows where the funnel broke. The underlying service cause remains unverified.")
        if completion["id"] == "product_view":
            report["uncertainty"] += " The observed events do not identify a hosting location."
    context = {
        "anomaly": anomaly, "metric": label, "affected_scope": scope,
        "baseline_impact": impact, "metric_evidence": signals, "event_evidence": samples,
        "segment_comparison": comparisons, "context": window_context, "graph_path": paths,
        "first_observable_break": first_break, "transition": completion,
    }
    return report, context


def resolve_report(model, context, fallback):
    answer, provenance = ask(model, "investigation", REPORT_PROMPT, context)
    frankfurt_observed = any(
        "frankfurt" in str(row.get("attrs", {}).get(field, "")).casefold()
        for sample in context.get("event_evidence", ()) for row in sample.get("events", ())
        for field in ("hosting_location", "hosting_region", "datacenter", "edge_location")
    )

    event_ids = {row["id"] for sample in context.get("event_evidence", ()) for row in sample.get("events", ())
                 if row.get("id")}

    def supported(text):
        return (isinstance(text, str) and bool(text.strip()) and not any(identifier in text for identifier in event_ids)
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
        context = job["context"]
        cutoff = (context.get("context", {}).get("window") or {}).get("observation_snapshot")
        save_report_snapshot(db, job["incident_id"], "initial", report, context, cutoff)
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


def finish_next_branch(dsn, stop):
    with connect(dsn) as db:
        if stop.is_set() or not db.execute(
            "select pg_try_advisory_xact_lock(hashtext('eye.evidence_branches')) as claimed"
        ).fetchone()["claimed"]:
            return False
        baseline = state(db, "baseline_through")
        latest = state(db, "last_window")
        if not baseline or not latest:
            return False
        observed = state(db, "last_observed_at")
        cutoff = datetime.fromisoformat(observed) if observed else db.execute(
            "select coalesce(observation_snapshot, end_at) as cutoff from windows where start_at = %s",
            (datetime.fromisoformat(latest),),
        ).fetchone()["cutoff"]
        incident = db.execute(
            """select * from incidents
               where report->>'deep_investigation' = 'pending'
                  or (report->>'branch_waits_for_receipts' = 'true'
                      and (report->>'branch_observed_through')::timestamptz < %s)
                  or (report->>'branch_revisit' = 'true'
                      and (report->>'branch_observed_through')::timestamptz + interval '15 minutes' <= %s)
               order by (report->>'deep_investigation' = 'pending') desc, id limit 1""",
            (cutoff, cutoff),
        ).fetchone()
        if not incident:
            return False
        findings, watch_receipts, observed_through = build_branches(db, incident, cutoff, datetime.fromisoformat(baseline))
        if stop.is_set():
            return False
        previous = incident["report"].get("evidence_branches", [])
        branches = [branch for branch, _ in findings]
        if branches != [{key: value for key, value in branch.items() if key != "evidence_id"} for branch in previous]:
            for branch, details in findings:
                branch["evidence_id"] = add_evidence(
                    db, incident["id"], incident["opened_at"], "branch evidence",
                    {"title": branch["title"], "observed_through": observed_through, **details},
                )
        else:
            branches = previous
        fields = {"evidence_branches": branches, "deep_investigation": "complete",
                  "branch_waits_for_receipts": watch_receipts, "branch_observed_through": observed_through}
        fields["branch_revisit"] = any(details.get("revisit") for _, details in findings)
        db.execute("update incidents set report = report || %s where id = %s", (jsonb(fields), incident["id"]))
        save_report_snapshot(db, incident["id"], "deeper", fields, [details for _, details in findings], observed_through)
        return True


def watch_branches(dsn, stop, poll=0.5):
    while not stop.is_set():
        try:
            if finish_next_branch(dsn, stop):
                continue
        except Exception:
            log.exception("Evidence branches could not be saved and will be retried")
            stop.wait(2)
        stop.wait(poll)


def update_recovery(db, incident, start, reading, healthy, node, observed_at=None):
    cadence = CADENCE if observed_at else HOUR
    required = HOUR // CADENCE if observed_at else 2
    previous = incident["healthy_windows"] if incident["updated_at"] + cadence == start else 0
    streak = previous + 1 if healthy else 0
    recovered = start if streak >= required else None
    recovery = {}
    if recovered:
        recovery["recovery"] = f"The affected rate or metric returned to its baseline range for {required} consecutive evaluations."
        if observed_at:
            recovery["recovery_observed_at"] = observed_at
    if reading:
        add_evidence(db, incident["id"], start, "recovery observation", reading, node)
    db.execute(
        """update incidents set updated_at = %s, healthy_windows = %s,
           recovered_at = %s, report = report || %s where id = %s""",
        (start, streak, recovered, jsonb(recovery), incident["id"]),
    )


def workflow_groups(readings, open_incidents, alerts):
    groups = []
    for incident in open_incidents:
        initial = incident["report"].get("break", {})
        if initial.get("id") in BY_ID:
            current = readings.get(BY_ID[initial["id"]], incident["scope"])
            groups.append({"incident": incident, "completion": current, "alerts": []})
    for index, step in enumerate(WORKFLOW):
        relevant = [row for row in alerts if ALERT_STEPS.get(row["metric_id"], -1) >= index]
        if not relevant:
            continue
        chosen = [group["completion"] for group in groups
                  if group["completion"]["id"] == step.id]
        for completion in readings.degraded(step):
            scope = completion["scope"]
            # An alert on an intersecting aggregate does not corroborate a break in an unrelated segment.
            if (not any(compatible(row["scope"], scope) and (not scope or scope.keys() & row["scope"].keys())
                        for row in relevant)
                    or any(compatible(row["scope"], completion["scope"]) for row in chosen)):
                continue
            groups.append({"incident": None, "completion": completion, "alerts": []})
            chosen.append(completion)
    return groups


def assign_workflow_alerts(readings, groups, alerts):
    remaining = []
    for row in alerts:
        stage = ALERT_STEPS.get(row["metric_id"], -1)
        candidates = [group for group in groups if (group["completion"]["status"] == "degraded" or group.get("incident"))
                      and POSITIONS[group["completion"]["id"]] <= stage
                      and compatible(group["completion"]["scope"], row["scope"])]
        if not candidates:
            remaining.append(row)
            continue
        # Attribute the signal to its matching cohort before comparing candidate steps.
        best = max(candidates, key=lambda group: (
            group["completion"]["status"] == "degraded",
            sum(row["scope"].get(key) == value for key, value in group["completion"]["scope"].items()),
            POSITIONS[group["completion"]["id"]],
            group["completion"]["strength"],
        ))
        anchor = best["completion"]
        payload = dict(row)
        if stage > POSITIONS[anchor["id"]]:
            local = {**anchor["scope"], **row["scope"]}
            checks = [readings.get(step, local) for step in WORKFLOW[POSITIONS[anchor["id"]] + 1:stage + 1]]
            uncertain = [check for check in checks if check["status"] != "steady"]
            payload["relationship"] = "uncertain" if uncertain else "downstream volume"
            payload["completion_checks"] = checks
        else:
            payload["relationship"] = "same transition"
        best["alerts"].append(payload)
    return remaining


def open_incident(db, tool, start, baseline, anchor, alerts, model, completion=None, explanation=None):
    incident_id = db.execute(
        """insert into incidents (metric_id, scope, opened_at, updated_at)
           values (%s, %s, %s, %s) returning id""",
        (anchor["metric_id"], jsonb(anchor["scope"]), start, start),
    ).fetchone()["id"]
    report, context = collect_evidence(db, tool, anchor, incident_id, baseline, completion)
    if explanation:
        report["shared_explanation"] = context["shared_explanation"] = explanation
        report["likely_causes"] = [explanation["summary"]]
    if model:
        db.execute("insert into pending_reports values (%s, %s, %s)",
                   (incident_id, jsonb(context), jsonb(report)))
        report = {key: report[key] for key in
                  ("summary", "affected_scope", "path", "first_observable_break", "shared_explanation") if key in report}
    if completion:
        report["break"] = completion
    else:
        metric = tool("metric_definition", metric_id=anchor["metric_id"])
        report["measurement"] = {"observed": anchor["observed"], "expected": anchor["expected"],
                                 "unit": metric["access"].get("unit", "count")}
    report["deep_investigation"] = "pending"
    if anchor.get("observed_at"):
        report["observation_window"] = {key: anchor[key] for key in ("window_start", "window_end", "observed_at")}
    db.execute("update incidents set report = %s where id = %s", (jsonb(report), incident_id))
    if not model:
        cutoff = (context["context"].get("window") or {}).get("observation_snapshot")
        save_report_snapshot(db, incident_id, "initial", report, context, cutoff)
    attach_alerts(db, incident_id, start, [row for row in alerts if row["id"] != anchor.get("id")],
                  "related anomaly")
    return incident_id


def attach_alerts(db, incident_id, start, alerts, kind):
    uncertain = {}
    for row in alerts:
        add_evidence(db, incident_id, start, kind, row, metric_node(row["metric_id"]))
        if row.get("relationship") == "uncertain":
            for check in row.get("completion_checks", ()):
                if check["status"] != "steady":
                    uncertain[check["id"]] = check["label"]
    if uncertain:
        db.execute("update incidents set report = report || %s where id = %s",
                   (jsonb({"uncertain_relationships": list(uncertain.values())}), incident_id))


def track_other(db, tool, start, baseline, incidents, alerts, model, good_data, observed_at):
    for incident in incidents:
        shared = incident["report"].get("shared_explanation", {})
        same = [row for row in alerts if not shared and row["metric_id"] == incident["metric_id"]
                and compatible(row["scope"], incident["scope"])]
        if same:
            attach_alerts(db, incident["id"], start, same, "ongoing anomaly")
            for row in same:
                alerts.remove(row)
            update_recovery(db, incident, start, None, False, None, observed_at)
            continue
        current = tool("metric_readings", metric_id=incident["metric_id"], start=start,
                       end=start + CADENCE, scope=incident["scope"])
        metric = tool("metric_definition", metric_id=incident["metric_id"])
        ratio = metric["calculation"]["op"] in {"ratio", "share"} or metric["access"].get("unit") == "ratio"
        reading = current[-1] if current else None
        healthy = (good_data and reading and reading["value"] is not None
                   and (not ratio or reading["sample_size"] >= 30))
        if shared.get("kind") == "source":
            types = [tool("metric_readings", metric_id=COMPLETENESS, start=start, end=start + CADENCE,
                          scope={**incident["scope"], "event_type": event}) for event in shared["event_types"]]
            healthy = reading and reading["value"] is not None and reading["value"] >= .97 and all(
                rows and rows[-1]["value"] is not None and rows[-1]["value"] >= .97 for rows in types)
        update_recovery(db, incident, start, reading, healthy, metric_node(incident["metric_id"]), observed_at)

    while alerts:
        # Keep independent signals when there is no measured workflow break to explain them.
        anchor = max(alerts, key=lambda row: row["score"] * (1 + .15 * len(row["scope"])))
        cohort = [row for row in alerts if row["metric_id"] == anchor["metric_id"]
                  and compatible(row["scope"], anchor["scope"])]
        for row in cohort:
            alerts.remove(row)
        open_incident(db, tool, start, baseline, anchor, cohort, model)


def apply_shared(db, tool, start, baseline, plan, alerts, incidents, model, observed_at):
    kind, scope, metric = (plan[key] for key in ("kind", "scope", "metric_id"))
    matching = [row for row in plan["alerts"] if row in alerts]
    if not matching:
        return None
    relation = {"source": "shared receipt loss", "stock": "shared inventory dependency", "search": "matched search cohort"}[kind]
    if plan["confidence"] == "provisional":
        relation = "provisional " + relation
    attached = [dict(row, relationship=relation) for row in matching]
    explanation = {key: value for key, value in plan.items() if key not in {"incident", "alerts"}}
    incident = plan["incident"]
    if not incident:
        incident = next((item for item in incidents if item["metric_id"] == metric and item["scope"] == scope
                         and not item["report"].get("break")), None)
    if not incident and kind == "source":
        # A narrow receipt breach may have opened first while sibling samples were sparse.
        initial = [item for item in incidents if item["metric_id"] == metric
                   and item["scope"].get("source_id") == scope["source_id"]
                   and item["scope"].get("event_type") in plan["event_types"] and start - item["updated_at"] < HOUR]
        if len(initial) == 1:
            incident = initial[0]
            db.execute("update incidents set scope=%s where id=%s", (jsonb(scope), incident["id"]))
    if incident:
        db.execute("update incidents set report=report || %s where id=%s",
                   (jsonb({"shared_explanation": explanation, "affected_scope": scope}), incident["id"]))
        attach_alerts(db, incident["id"], start, attached, "ongoing anomaly")
        update_recovery(db, incident, start, None, False, None, observed_at)
        identifier = incident["id"]
    else:
        anchor = next((row for row in attached if row["metric_id"] == metric and row["scope"] == scope), None)
        if not anchor:
            reading = signal(tool, metric, start, scope, baseline)
            if reading is None or reading["expected"] is None:
                return None
            anchor = {**reading, "window_start": start, "window_end": start + HOUR, "observed_at": observed_at}
        identifier = open_incident(db, tool, start, baseline, anchor, attached, model, explanation=explanation)
    for row in matching:
        alerts.remove(row)
    return identifier


def track(db, start, anomalies, model=None, *, history=None, facts=None, observed_at=None):
    open_incidents = db.execute("select * from incidents where recovered_at is null order by id").fetchall()
    if not anomalies and not open_incidents:
        return
    baseline = datetime.fromisoformat(state(db, "baseline_through"))
    tool = observation_tool(db)
    health = tool("metric_readings", metric_id="data_completeness_ratio", start=start, end=start + CADENCE)
    good_data = not health or (health[-1]["value"] is not None and health[-1]["value"] >= 0.97)
    remaining = list(anomalies)
    accounted = set()
    current_facts = (facts or {}).get(start)
    for plan in source_plans(anomalies, open_incidents, current_facts):
        identifier = apply_shared(db, tool, start, baseline, plan, remaining, open_incidents, model, observed_at)
        if identifier:
            accounted.add(identifier)
    readings, groups = None, []
    if any(row["metric_id"] in ALERT_STEPS for row in anomalies) or any(
            incident["report"].get("break", {}).get("id") in BY_ID for incident in open_incidents):
        if history is not None:
            discarded = {row["window_start"] for row in db.execute(
                """select window_start from observations where metric_id = 'data_completeness_ratio'
                   and scope = '{}' and window_start = any(%s)
                   and (details->>'quality' = 'missing_window' or value < 0.97)""", (history,),
            ).fetchall()}
            history = [date for date in history if date not in discarded]
        readings = Readings(db, start, baseline, history, facts)
        groups = workflow_groups(readings, open_incidents, remaining if good_data else [])
    if good_data and current_facts and observed_at:
        for kind in ("stock", "search"):
            plan = cohort_plan(db, kind, start, observed_at, remaining, open_incidents, groups, current_facts)
            if plan:
                if readings:
                    plan["completion_checks"] = [readings.get(step, plan["scope"]) for step in WORKFLOW
                                                 if POSITIONS[step.id] >= POSITIONS["checkout"]]
                identifier = apply_shared(db, tool, start, baseline, plan, remaining, open_incidents, model, observed_at)
                if identifier:
                    accounted.add(identifier)
    if readings:
        groups = workflow_groups(readings, open_incidents, remaining if good_data else [])
        remaining = assign_workflow_alerts(readings, groups, remaining)
        for group in groups:
            completion, attached = group["completion"], group["alerts"]
            step = BY_ID[completion["id"]]
            incident = group["incident"]
            if not incident and not attached:
                continue
            if incident is None:
                # A prior breach can appear before there is enough cohort evidence to locate its step.
                incident = next((item for item in open_incidents if item["id"] not in accounted
                                 and item["metric_id"] == step.metric and item["scope"] == completion["scope"]), None)
                if incident:
                    db.execute("update incidents set report = report || %s where id = %s",
                               (jsonb({"break": completion}), incident["id"]))
                    add_evidence(db, incident["id"], start, "first observable break", completion, metric_node(step.metric))
            if incident:
                accounted.add(incident["id"])
                attach_alerts(db, incident["id"], start, attached, "ongoing anomaly")
                update_recovery(db, incident, start, completion,
                                good_data and completion["status"] == "steady" and not attached,
                                metric_node(step.metric), observed_at)
            else:
                anchor = next((row for row in attached if row["metric_id"] == step.metric
                               and row["scope"] == completion["scope"]), None)
                if not anchor:
                    anchor = {"metric_id": step.metric, "window_start": start, "scope": completion["scope"],
                              "observed": completion["observed"], "expected": completion["expected"],
                              "sample_size": completion["eligible"], "window_end": start + HOUR,
                              "observed_at": observed_at}
                open_incident(db, tool, start, baseline, anchor, attached, model, completion)

    others = [incident for incident in open_incidents if incident["id"] not in accounted]
    track_other(db, tool, start, baseline, others, remaining, model, good_data, observed_at)
