"""Broker Devin's bounded graph investigation through local observation reads."""

from datetime import datetime
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eye.db import connect
from eye.intervals import CADENCE, GRACE, HOUR, aligned_history
from eye.monitor import rate_counts
from eye.observation import observation_tool

PROMPT = """You are Eye's incident investigator. Use only the bounded telemetry below. Never access
repositories, the internet, or outside tools. The graph is discovered structure, not proof of causation.
Use the structured output tool and mark each turn's output final. Echo the current turn. Either submit a report and an empty queries list,
or submit up to three distinct reads and a null report. Use turn 1 to request evidence when a read can
test a plausible explanation. You may request one optional follow-up batch on turn 2. Turn 3 must be a
report. After requesting reads, wait for Eye to send their results. Use each read's reason to say what
it can test. Omit time bounds: Eye fixes the anomaly window and observation cutoff. Omit scope to use
the alert scope, or send {} for marketplace and a JSON object for an explicitly matched cohort.
Tools and their arguments:
- graph_neighbors: target is an exact node ID. Incoming and outgoing typed edges describe relationships.
- metric_definition, metric_readings, metric_breakdown: target is a metric ID without the metric: prefix.
  Readings include current, recent, and time-aligned history. Breakdown can enumerate broader
  comparisons when given scope {}.
- decompose_metric: target is a metric ID with a declared ratio. Quantifies how its observed
  numerator and denominator contribute to the deviation against aligned history. Also tests the
  observed outcome count at the historical rate and shows recent checks with similar eligible volume.
- activity_mix: activities are event names from the metric calculation or discovered graph. Compares
  event counts against the same scoped historical hours at equal ingestion maturity.
- supporting_events: activities are event names. Returns a few observed payloads with linked objects.
Cite supplied E-prefixed evidence IDs in the report. If reads returned evidence, cite that evidence.
Include a plausible alternative, what weakens it, and the evidence IDs for that comparison. When the
evidence cannot test an alternative, say so. Distinguish event counts from unique entities, delayed
visibility from workflow failure, and symptoms from causes. Report the scope the evidence supports.
For a ratio, compare outcomes per eligible entity. A rise in the absolute outcome count does not rule
out genuine degradation per eligible entity. Holding the rate constant, higher volume would increase
both counts proportionally. If a recent check with similar eligible volume had a healthy rate, compare
the rates and use that observation to test the intake-only explanation. Quantify excess failures or
missing outcomes at the observed level of eligibility. These arithmetic results do not identify the
underlying cause. Respect the stated direction of graph edges and limit sample claims to sampled records.
Keep raw event IDs out of the prose. Never infer hosting or engineering causes without direct evidence.
State why you stopped and which uncertainty remains. Avoid invented facts or quantities."""


class Structured(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Query(Structured):
    tool: Literal["graph_neighbors", "metric_definition", "metric_readings", "metric_breakdown",
                  "activity_mix", "supporting_events", "decompose_metric"]
    reason: str = Field(min_length=1, max_length=320)
    target: str | None = Field(default=None, max_length=160)
    scope: dict[str, str] | None = None
    activities: list[str] = Field(default_factory=list, max_length=8)


class Alternative(Structured):
    alternative: str = Field(min_length=1)
    weakened_by: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)


class Diagnosis(Structured):
    summary: str = Field(min_length=1)
    likely_causes: list[str] = Field(min_length=1, max_length=4)
    impact: str = Field(min_length=1)
    uncertainty: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    alternative: Alternative
    stop_reason: str = Field(min_length=1)


class Turn(Structured):
    turn: int
    queries: list[Query] = Field(max_length=3)
    report: Diagnosis | None


def neighborhood(graph, node_id):
    nodes = {item["id"]: item for item in graph["nodes"]}
    edges = [item for item in graph["edges"] if node_id in (item["source"], item["target"])]
    neighbors = sorted({end for edge in edges for end in (edge["source"], edge["target"])} - {node_id})
    return {"node": nodes.get(node_id), "edges": edges, "neighbors": [nodes[node] for node in neighbors]}


def capture(db, tool, anomaly, baseline, record):
    """Freeze graph metadata and essential telemetry in the transaction that opens the incident."""
    start = anomaly["window_start"]
    end = anomaly.get("window_end") or start + HOUR
    cutoff = anomaly.get("observed_at") or end + GRACE
    metric = anomaly["metric_id"]
    graph = {
        "nodes": db.execute("select id, kind, label, properties from graph_nodes order by id").fetchall(),
        "edges": db.execute("""select source, target, kind, provenance, confidence, properties
                             from graph_edges order by source, target, kind, provenance""").fetchall(),
    }
    # Retain metadata that existed at the cutoff even when a later metric registration changes its definition.
    definitions = {item["id"]: item for item in db.execute("select * from metrics").fetchall()}
    root = f"metric:{metric}"
    direct = neighborhood(graph, root)
    dates = aligned_history(start, baseline)
    scope = anomaly["scope"]
    readings = tool("metric_readings", metric_id=metric, start=dates[-1], end=dates[0] + CADENCE,
                    scope=scope, aligned_to=start, as_of=cutoff, limit=4)
    delivery = tool("company_context", window_start=start, as_of=cutoff)
    expected = delivery["window"]["expected_events"]
    observed = sum(row["events"] for row in delivery["activities"])
    quality = {"manifest_events": expected, "observed_events": observed,
               "completeness": observed / expected if expected else None}
    def seed(tool_name, title, signal, result):
        payload = evidence_record(title, tool_name, {"metric_id": metric, "scope": scope}, signal, start, end,
                                  cutoff, metric, result, "Captured at incident opening.")
        return {"id": record("investigation context", payload), **payload}

    initial = [
        seed("alert", "Alert, scoped baseline and data quality", "data_quality",
             {"alert": anomaly, "metric_definition": definitions[metric], "historical_readings": readings,
              "coverage": quality}),
        seed("graph_neighbors", "Direct discovered neighbours", "graph_structure", direct),
    ]
    return {"evidence": initial, "graph": graph, "definitions": definitions, "baseline": baseline,
            "start": start, "end": end, "cutoff": cutoff, "scope": scope, "metric": metric}


def evidence_record(title, tool, params, signal, start, end, cutoff, entity, result, interpretation):
    # The E-prefixed ID is derived from Eye's evidence row ID when persisted.
    return {"title": title, "step": "events" if tool in {"activity_mix", "supporting_events"} else "frontier",
            "query": tool, "tool": tool, "params": params, "window": [start, end], "observed_through": cutoff,
            "entity": entity, "signal": signal, "result": result, "interpretation": interpretation}


def decompose_metric(formula, latest, historical, recent=()):
    """Split a declared ratio deviation by averaging both possible input-change orders."""
    if formula.get("op") != "ratio" or not formula.get("denominator_activity"):
        return {"available": False, "reason": "The metric does not declare both activities of a ratio."}
    samples = [row for row in historical if row["value"] is not None and row["sample_size"]
               and row["details"].get("quality") not in {"incomplete", "missing_window"}]
    if not latest or latest[0]["value"] is None or not latest[0]["sample_size"] or not samples:
        return {"available": False, "reason": "Observed and reference counts are required."}
    numerator, denominator = rate_counts(latest[0])
    expected_n, expected_d = (fmean(values) for values in zip(*(rate_counts(row) for row in samples)))
    if min(denominator, expected_d) <= 0:
        return {"available": False, "reason": "A zero eligible count cannot support a ratio comparison."}
    delta = numerator / denominator - expected_n / expected_d
    numerator_part = (numerator - expected_n) * (1 / denominator + 1 / expected_d) / 2
    denominator_part = (numerator + expected_n) * (1 / denominator - 1 / expected_d) / 2
    expected_at_current_volume = expected_n / expected_d * denominator
    comparable = []
    for row in recent:
        if row["value"] is None or not row["sample_size"]:
            continue
        outcomes, eligible = rate_counts(row)
        if abs(eligible - denominator) <= denominator * .10:
            comparable.append({"window_start": row["window_start"], "outcomes": outcomes,
                               "eligible": eligible, "rate": row["value"]})
    return {"available": True, "historical_windows": len(samples), "observed_rate": numerator / denominator,
            "expected_rate": expected_n / expected_d, "rate_change": delta,
            "expected_outcomes_at_observed_eligibility": expected_at_current_volume,
            "outcome_gap_at_observed_eligibility": numerator - expected_at_current_volume,
            "recent_checks_within_10_percent_of_eligible_volume": comparable[-6:],
            "numerator": {"activity": formula["activity"], "observed": numerator, "expected": expected_n,
                          "rate_contribution": numerator_part},
            "denominator": {"activity": formula["denominator_activity"], "observed": denominator, "expected": expected_d,
                            "rate_contribution": denominator_part},
            "residual": delta - numerator_part - denominator_part}


def read_query(dsn, snapshot, read):
    start, cutoff, baseline = (datetime.fromisoformat(snapshot[key]) for key in ("start", "cutoff", "baseline"))
    scope = snapshot["scope"] if read.scope is None else read.scope
    if read.tool == "graph_neighbors":
        if not read.target:
            raise ValueError("Supply a graph node ID as target.")
        result = neighborhood(snapshot["graph"], read.target)
        if not result["node"]:
            raise ValueError("This node was absent from the graph captured at the observation cutoff.")
        return result
    definitions = snapshot["definitions"]
    if (read.tool.startswith("metric_") or read.tool == "decompose_metric") and read.target not in definitions:
        raise ValueError("Supply a metric ID from the discovered graph.")
    if read.tool == "metric_definition":
        return definitions[read.target]
    with connect(dsn) as db:
        db.execute("set transaction read only")
        tool = observation_tool(db)
        if read.tool in {"metric_readings", "decompose_metric"}:
            dates = aligned_history(start, baseline)
            args = {"metric_id": read.target, "scope": scope, "as_of": cutoff}
            latest = tool("metric_readings", start=start, end=start + CADENCE, limit=1, **args)
            historical = tool("metric_readings", start=dates[-1], end=dates[0] + CADENCE,
                              aligned_to=start, limit=4, **args)
            recent = tool("metric_readings", start=start - HOUR, end=start, limit=12, **args)
            if read.tool == "decompose_metric":
                return decompose_metric(definitions[read.target]["calculation"], latest, historical, recent)
            return {"latest": latest, "aligned_reference": historical, "recent_checks": recent}
        if read.tool == "metric_breakdown":
            return tool(read.tool, metric_id=read.target, window_start=start, as_of=cutoff, scope=scope, limit=14)
        known = {item["id"].removeprefix("activity:") for item in snapshot["graph"]["nodes"]
                 if item["kind"] == "activity"}
        if not read.activities or set(read.activities) - known:
            raise ValueError("Supply activity names from the discovered graph.")
        args = {"window_start": start, "scope": scope, "activities": read.activities, "as_of": cutoff}
        if read.tool == "supporting_events":
            return tool(read.tool, limit=4, **args)
        return tool(read.tool, **args)


def run(dsn, model, snapshot, record):
    initial = snapshot["evidence"]
    evidence = list(initial)
    successful_reads = set()
    signals = {"activity_mix": "process_mix", "supporting_events": "event_match",
               "graph_neighbors": "graph_structure", "metric_breakdown": "drilldown",
               "decompose_metric": "decomposition"}
    used_nodes = {f"metric:{snapshot['metric']}"}

    def respond(raw, turn):
        answer = Turn.model_validate(raw)
        if answer.report or not answer.queries or turn == 3:
            return None
        records = []
        for read in answer.queries:
            params = read.model_dump(exclude_none=True, exclude_defaults=True, exclude={"tool", "reason"})
            params.setdefault("scope", snapshot["scope"])
            successful = False
            try:
                result = read_query(dsn, snapshot, read)
                successful = bool(result)
            except ValueError as exc:
                result = {"error": str(exc)}
            payload = evidence_record(read.tool.replace("_", " ").title(), read.tool, params,
                                      signals.get(read.tool, "metric_observation"), snapshot["start"],
                                      snapshot["end"], snapshot["cutoff"], read.target or ", ".join(read.activities),
                                      result, read.reason)
            entry = {"id": record("investigation read", payload), **payload}
            evidence.append(entry)
            records.append(entry)
            if successful:
                successful_reads.add(entry["id"])
                if read.target and (read.tool.startswith("metric_") or read.tool == "decompose_metric"):
                    used_nodes.add(f"metric:{read.target}")
                elif read.target and read.tool == "graph_neighbors":
                    used_nodes.add(read.target)
                else:
                    used_nodes.update(f"activity:{name}" for name in read.activities)
        return records

    try:
        answer, provenance = model.investigate(PROMPT, Turn.model_json_schema(), {
            "evidence": initial, "observation_cutoff": snapshot["cutoff"], "read_batches": 2,
        }, respond)
        if not answer:
            return None, evidence
        answer = Turn.model_validate(answer)
    except ValidationError:
        return None, evidence
    if answer.queries or not answer.report:
        return None, evidence
    diagnosis = answer.report.model_dump()
    citations = set(diagnosis["evidence_ids"]) | set(diagnosis["alternative"]["evidence_ids"])
    if not citations <= {item["id"] for item in evidence} or (successful_reads and not citations & successful_reads):
        return None, evidence
    diagnosis.update(provenance, reasoning="model", investigation_method="devin_graph",
                     path=sorted(used_nodes), conclusion=diagnosis["summary"])
    return diagnosis, evidence
