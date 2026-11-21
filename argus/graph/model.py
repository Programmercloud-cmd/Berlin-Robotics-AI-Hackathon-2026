"""Data model of the metric graph, aligned with plan/jacob (docs/interface.md, docs/harness.md).

Contributors supply MetricContracts (meaning + access). Everything else — edges, roles,
statuses, confidence — is inferred and owned by the agent runtime.

Edge types (all point upstream -> downstream; "what explains X" = X's predecessors)

  arithmetic_component  operand -> derived metric   parsed or discovered formula; exact decomposition
  breakdown_by          dim:<d> -> metric           metric supports a dimension; localisation
  sourced_from          source:<s> -> metric        lineage; data-quality reasoning
  subset_of             subset -> superset          population nesting (checkout sessions within
                                                    sessions); funnel order and invariant checks.
                                                    [extension of the plan's five types]
  possible_driver       driver -> metric            plausible business mechanism; licenses *testing*,
                                                    never a conclusion; stays provisional without evidence
  associated_with       metric -> metric            measured co-movement; never labelled causal
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class EdgeType(str, Enum):
    ARITHMETIC = "arithmetic_component"
    BREAKDOWN = "breakdown_by"
    SOURCED = "sourced_from"
    SUBSET = "subset_of"
    DRIVER = "possible_driver"
    ASSOCIATED = "associated_with"


EDGE_REASONING = {
    EdgeType.ARITHMETIC: "decomposition: exact attribution of a change to formula operands",
    EdgeType.BREAKDOWN: "localisation: which segments carry the change (mix vs rate for ratios)",
    EdgeType.SOURCED: "data quality: completeness, maturity and freshness of the source",
    EdgeType.SUBSET: "funnel structure: stage order, step rates, subset <= superset invariants",
    EdgeType.DRIVER: "hypothesis: a mechanism worth testing; never a conclusion by itself",
    EdgeType.ASSOCIATED: "evidence: measured co-movement with effect size and uncertainty; not causal",
}

EdgeStatus = Literal["validated", "provisional", "rejected"]


# ====================================================================== contracts (contributor input)
CALC_OPS = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}


class OperandRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_id: str
    version: int = 1


class MetricContract(BaseModel):
    """Exactly the contributor contract of docs/interface.md. Unknown fields (parent, edges,
    threshold, scenario...) are rejected by extra='forbid'."""
    model_config = ConfigDict(extra="forbid")

    metric_id: str
    version: int = Field(ge=1)
    name: str
    definition: str
    unit: str
    population: str
    time_semantics: Literal["event_time", "session_cohort", "snapshot", "business_day_to_date"]
    dimensions: list[str]
    supported_granularities: list[str]
    source_ids: list[str]
    query_ref: Optional[str] = None
    calculation: Optional[dict] = None
    maturity_delay_seconds: int = Field(ge=0)
    null_policy: str
    aggregation_policy: str
    effective_from: str
    owner: str

    @model_validator(mode="after")
    def _one_access_path(self):
        if (self.query_ref is None) == (self.calculation is None):
            raise ValueError("exactly one of query_ref or calculation is required")
        if not self.metric_id.isidentifier():
            raise ValueError(f"illegal metric_id {self.metric_id!r}")
        return self

    # ---- calculation tree helpers ({operator, left, right} | {metric_id, version} | {constant})
    def expression(self) -> Optional[str]:
        if self.calculation is None:
            return None

        def render(node: dict) -> str:
            if "metric_id" in node:
                return node["metric_id"]
            if "constant" in node:
                return repr(float(node["constant"]))
            op = node.get("operator")
            if op not in CALC_OPS or "left" not in node or "right" not in node:
                raise ValueError(f"malformed calculation node {node!r}")
            return f"({render(node['left'])} {CALC_OPS[op]} {render(node['right'])})"
        return render(self.calculation)

    def operands(self) -> list[OperandRef]:
        out: list[OperandRef] = []

        def walk(node):
            if not isinstance(node, dict):
                raise ValueError(f"malformed calculation node {node!r}")
            if "metric_id" in node:
                extra = set(node) - {"metric_id", "version"}
                if extra:
                    raise ValueError(f"operand node {node.get('metric_id')!r} has unexpected keys {sorted(extra)}")
                out.append(OperandRef(**node))
            elif "constant" not in node:
                walk(node.get("left"))
                walk(node.get("right"))
        if self.calculation is not None:
            walk(self.calculation)
        return out


# ====================================================================== graph objects (agent-owned)
class Check(BaseModel):
    name: str                    # units | population | time_semantics | dimensions | reconciliation | ...
    passed: Optional[bool]       # None = not yet testable (e.g. awaiting data)
    detail: str = ""


class Metric(BaseModel):
    contract: MetricContract
    status: Literal["warming", "active", "deprecated"] = "warming"
    role: str = ""               # inferred: count | rate | currency | snapshot | derived | data_quality ...
    canonical_dimensions: list[str] = Field(default_factory=list)
    registered_at: str = Field(default_factory=now_iso)
    provenance: str = "registry"

    @property
    def id(self) -> str:
        return self.contract.metric_id


class Dimension(BaseModel):
    id: str
    description: str = ""
    members: list[str] = Field(default_factory=list)


class Source(BaseModel):
    id: str
    description: str = ""
    entity: str = ""
    keys: list[str] = Field(default_factory=list)
    time_field: str = ""
    dimensions: list[str] = Field(default_factory=list)
    dimension_mapping: dict[str, str] = Field(default_factory=dict)
    derived_from: list[str] = Field(default_factory=list)
    role: str = "business"

    def canonical(self, dim: str) -> str:
        return self.dimension_mapping.get(dim, dim)


class Edge(BaseModel):
    type: EdgeType
    source: str
    target: str
    status: EdgeStatus = "provisional"
    provenance: str = "contract"            # contract | rule:<name> | llm | data
    confidence: float = 1.0
    checks: list[Check] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)   # why provisional / rejected
    # arithmetic
    expression: Optional[str] = None
    role: Optional[str] = None
    polarity: Optional[int] = None
    # breakdown
    dimension: Optional[str] = None
    # driver / association
    mechanism: str = ""
    lag_minutes: list[int] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    revision: int = 1
    created_at: str = Field(default_factory=now_iso)
    revised_at: str = Field(default_factory=now_iso)

    @property
    def id(self) -> str:
        base = f"{self.type.value}:{self.source}->{self.target}"
        return f"{base}#{self.expression}" if self.type == EdgeType.ARITHMETIC else base

    def decide(self):
        """Status from checks: any failed -> rejected; any untested -> provisional; else validated."""
        if any(c.passed is False for c in self.checks):
            self.status = "rejected"
        elif any(c.passed is None for c in self.checks) or self.reasons:
            self.status = "provisional"
        else:
            self.status = "validated"
        return self


class Registration(BaseModel):
    registration_id: str
    metric_id: str
    version: int
    state: Literal["submitted", "validating", "registered", "rejected"]
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    edges_created: list[str] = Field(default_factory=list)
    catalog_revision: int = 0
    submitted_at: str = Field(default_factory=now_iso)
    actor: str = "contributor"


class ChangeLogEntry(BaseModel):
    ts: str = Field(default_factory=now_iso)
    actor: str
    action: str
    target: str
    detail: str = ""
    revision: int = 0
