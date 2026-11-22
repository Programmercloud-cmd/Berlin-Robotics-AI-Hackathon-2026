"""Shared data contracts between the data layer, the agent and the UI."""
from __future__ import annotations
from datetime import datetime
from typing import Literal, Optional
import pandas as pd
from pydantic import BaseModel

# Dimension names come from the data source (e.g. country, platform, channel, payment_provider).
Dim = str
KNOWN_DIMS = ("country", "platform", "channel", "category", "advertiser")
Segment = dict[str, str]  # {} = total
Status = Literal["seed", "proposed", "validated", "needs_review", "rejected"]


def seg_key(seg: Segment) -> str:  # {"platform": "android", "country": "de"} -> "country=de|platform=android"
    return "|".join(f"{k}={seg[k]}" for k in sorted(seg))


# ---------- Data -> Brain (warehouse/semantic.py, sim/) ----------
class Event(BaseModel):
    id: str
    ts: datetime
    type: Literal["release", "config", "campaign", "holiday", "data_incident"]
    service: Optional[str] = None
    scope: Segment = {}
    description: str = ""
    planned: bool = False
    expected_effect: dict[str, float] = {}  # e.g. {"sessions": 1.4}


def metric(id: str, start: datetime, end: datetime,
           filters: Segment | None = None, group_by: list[Dim] | None = None) -> pd.DataFrame:
    """MetricFrame columns: ts, *group_by, value, numerator, denominator, complete."""
    raise NotImplementedError


def completeness(table: str, ts: datetime) -> float:  # rows / expected_rows, 0..1
    raise NotImplementedError


def events(start: datetime, end: datetime, services: list[str] | None = None) -> list[Event]:
    raise NotImplementedError


# ---------- Graph ----------
class MetricNode(BaseModel):
    id: str
    name: str
    description: str
    kind: Literal["count", "sum", "ratio", "gauge"]
    unit: str  # count | eur | share | score | ms | seconds | ...
    table: str
    value_sql: Optional[str] = None  # count/sum, e.g. "SUM(orders)"
    numerator_sql: Optional[str] = None  # ratio/gauge
    denominator_sql: Optional[str] = None
    filters: Segment = {}
    dimensions: list[Dim]
    good_direction: Literal["up", "down"] = "up"
    services: list[str] = []
    owner: str = ""
    status: Status = "seed"


class Edge(BaseModel):
    src: str  # the driver
    dst: str  # the metric it drives
    type: Literal["formula", "breakdown", "causal", "context"]
    expr: Optional[str] = None  # formula: dst's full expression
    op: Optional[Literal["product", "ratio", "sum", "difference"]] = None
    dimension: Optional[Dim] = None  # breakdown
    sign: Optional[Literal["+", "-", "±"]] = None  # causal
    lag_h: tuple[int, int] = (0, 0)
    prior: float = 0.3
    confidence: float = 1.0
    status: Status = "seed"
    evidence: list[str] = []  # evidence ids that moved the confidence


# ---------- Brain -> Surface (saved in the SQLite store) ----------
class WatchDecision(BaseModel):
    ts: datetime
    metric: str
    segment: str  # seg_key
    score: float
    reasons: list[str]  # ["impact", "recent release", "explore"]


class Anomaly(BaseModel):
    metric: str
    segment: str
    ts: datetime
    actual: float
    expected: float
    z: float
    detector: Literal["spike", "cusum", "pacing"]
    impact_eur_per_h: float


class Evidence(BaseModel):
    """One structured evidence record: written by deterministic code after every tool call."""
    id: str  # "E7"
    incident_id: str
    step: Literal["data_check", "decompose", "localise", "events", "predict", "frontier", "traverse"]
    query: str  # readable metric() call
    result: dict  # numbers only
    interpretation: str
    # v3 evidence anchoring: what was asked, of which entity, over which window, and what the statistics say
    tool: str = ""                 # e.g. decompose, drilldown, compare_control, causal_check, prune
    params: dict = {}              # the tool call's parameters
    window: list[str] = []         # [start, end] of the data slice
    entity: str = ""               # metric[segment]
    signal: str = ""               # decomposition | drilldown | control_test | event_match | causal_test |
    #                                prune | process_mix | graph_structure | data_quality | riskloc | meta
    stats: dict = {}               # z, p, share, lift, rel ... (numbers only)
    verdict: str = ""              # anomalous | normal | confined | not_confined | ...


class EvidenceLink(BaseModel):
    """A hypothesis' prediction tied to the evidence records that tested it."""
    prediction: str
    result: Literal["confirmed", "refuted", "untestable"]
    evidence_ids: list[str]
    log_lr: float                  # contribution to the posterior (log likelihood ratio)


class Hypothesis(BaseModel):
    id: str
    statement: str
    source: Literal["causal_edge", "event", "llm"]
    predictions: list[str]
    prior: float
    posterior: float
    status: Literal["open", "supported", "rejected"] = "open"
    evidence: list[EvidenceLink] = []
    score: float = 0.0             # deterministic hybrid score used for ranking (posterior x evidence diversity)


class Incident(BaseModel):
    id: str
    fingerprint: str  # first_cause|segment|hypothesis_id
    state: Literal["detected", "investigating", "alerted", "monitoring", "resolved", "reopened"]
    label: Optional[Literal["supported_cause", "hypothesis", "explained", "data_issue", "unexplained"]] = None
    opened_at: datetime
    updated_at: datetime
    first_cause: Optional[str] = None
    segment: str = ""
    anomalies: list[Anomaly] = []
    hypotheses: list[Hypothesis] = []
    evidence_ids: list[str] = []
    impact_eur_total: float = 0.0
    report_md: str = ""


class TickResult(BaseModel):
    ts: datetime
    watched: list[WatchDecision]
    anomalies: list[Anomaly]
    incidents_changed: list[str]


class MetricSpec(BaseModel):  # what a contributor submits
    id: str
    name: str
    description: str
    unit: str
    sql: Optional[str] = None
    expr: Optional[str] = None


class ExtensionReport(BaseModel):
    spec: MetricSpec
    inferred_kind: str
    dimensions: list[Dim]
    edges_added: list[Edge]
    alternatives: list[str]
    warnings: list[str]  # duplicate, failing slices, name mismatch
    status: Literal["validated", "needs_review", "rejected"]


# ---------- Surface -> loop (the store's commands table) ----------
class Command(BaseModel):
    id: str
    kind: Literal["inject_scenario", "add_metric", "feedback", "pause", "resume"]
    payload: dict  # scenario id | MetricSpec | {"incident_id", "kind"}
    created_at: datetime


# ---------- Brain entry points (called by loop.py) ----------
# tick(ts: datetime) -> TickResult
# extend(spec: MetricSpec) -> ExtensionReport
# feedback(incident_id: str, kind: Literal["false_alarm", "planned"]) -> None
