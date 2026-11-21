# Observation interface and metric registration

Status: proposed application contract. MCP is a transport adapter around these domain operations; it does not supply business semantics or run the monitoring schedule.

## Service boundaries

1. **Observation service:** exposes company evidence as of an immutable snapshot. Agent credential is read-only.
2. **Contributor API:** accepts versioned metric contracts and executes validation/backfill. Contributor can register metrics but cannot change simulation truth.
3. **Controller API:** advances/reset virtual time and injects scenarios. Operator/evaluator only; absent from the agent's tool list and network permissions.
4. **Agent state service:** graph changes, monitoring jobs, decisions, and incident updates. Owned by the monitoring runtime.

Implement one domain adapter first, then expose identical semantics over MCP. Proposed demo transport: Streamable HTTP on an isolated local run network, with a run-scoped observation credential and origin validation. Choose and pin compatible SDK/protocol versions at implementation; do not implement JSON-RPC by hand.

MCP tools have explicit input schemas and output schemas. Return the application result in structuredContent and a serialized JSON text block for client compatibility. Read-only annotations describe intent, while actual permissions enforce it.

Protocol references:
- [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

## Common types

- Time: RFC3339 UTC timestamp. Windows are [start, end).
- Snapshot: opaque server-issued snapshot_id binding run, observation time, catalog revision, and visible record revisions. Never accept a client-invented future snapshot.
- Filters: map of advertised dimension names to arrays of exact values (AND between dimensions, OR within values). No arbitrary expressions.
- group_by: unique advertised dimensions. Empty array means total.
- Granularity: 5m, 15m, 1h, or day for flows; inventory supports point-in-time snapshots. Day boundaries for campaign pacing use Europe/Berlin.
- Pagination: opaque cursor bound to snapshot and request; maximum page size 500.
- Every response: schema_version, snapshot_id, observed_at, request_id, and operation-specific data.

Every time-dependent read after get_observation_state requires snapshot_id. Reject windows ending after observed_at. Retain prior snapshots for the replay duration.

## Agent-visible tools

Signatures below define required fields unless followed by ?. Implement JSON Schema with additionalProperties=false and enums derived from the catalog.

| Tool | Arguments | Result |
| --- | --- | --- |
| get_observation_state | {} | Current snapshot_id, observed_at, catalog_revision, source watermarks; no seed/scenario information |
| list_metrics | snapshot_id, cursor?, page_size? | Registered metric IDs, versions, descriptions, status, next_cursor |
| get_metric | snapshot_id, metric_id, version? | Complete contract, executable query/formula description, source schema/lineage, validation status; no inferred graph placement |
| list_dimension_values | snapshot_id, metric_id, dimension, start, end, filters?, cursor?, page_size? | Visible values and supporting population counts |
| query_metric | snapshot_id, metric_id, start, end, granularity, group_by, filters? | Series rows, population/quality, query cost |
| list_sources | snapshot_id | Source IDs, schemas, descriptions, permitted event types and fields |
| query_events | snapshot_id, source_id, start, end, fields, filters?, cursor?, page_size? | Bounded visible evidence rows with stable event IDs |
| search_context | snapshot_id, start, end, kinds?, text?, scope_filters?, cursor?, page_size? | Published release/campaign/message documents with IDs, scope, published_at |
| get_evidence | snapshot_id, evidence_id | Exact prior query result or source record within that run and snapshot |

query_events filters use source-advertised fields, not arbitrary SQL. Source schemas define allowed fields/operators. No file path, URL fetch, shell, or unrestricted SQL tool is needed.

## Query result shape

Example application payload; MCP wraps this object as structuredContent:

~~~json
{
  "schema_version": "1",
  "snapshot_id": "snap-0042",
  "observed_at": "2026-11-21T10:30:00Z",
  "request_id": "req-17",
  "metric_id": "payment_success_rate",
  "definition_version": 1,
  "unit": "ratio",
  "population": "finalised_payment_attempts_by_finalisation_time",
  "rows": [
    {
      "start": "2026-11-21T10:00:00Z",
      "end": "2026-11-21T10:15:00Z",
      "dimensions": {"country": "DE", "platform": "Android"},
      "value": 0.52,
      "numerator": 52,
      "denominator": 100,
      "maturity": "mature",
      "completeness": "complete",
      "completeness_ratio": 1.0,
      "null_reason": null,
      "evidence_id": "ev-17-1"
    }
  ],
  "warnings": [],
  "cost": {"returned_rows": 1, "scanned_records": 110}
}
~~~

Currency values are integer cents; ratios are numbers in [0,1] except documented exceptions. Counts need no numerator/denominator. Maturity is mature/pending/not_applicable; completeness is complete/partial/unknown. A zero denominator has value=null and null_reason=zero_denominator. No observed events with missing manifests is not proof of zero activity.

Store evidence results immutably under their IDs so a report can be reproduced. Never make an old evidence ID resolve to a newer query result.

## Metric contract supplied by contributor

Required fields:
- metric_id, version (positive integer), name, definition
- unit, population, time_semantics (event_time or session_cohort or snapshot or business_day_to_date)
- dimensions, supported_granularities, source_ids
- exactly one of query_ref or calculation
- maturity_delay_seconds, null_policy, aggregation_policy
- effective_from, owner

query_ref is a pre-registered, parameterized read query owned by the observation service. It binds only snapshot, window, grouping, and filters. The service validates declared columns and access. Future contributor SQL support must compile against allowlisted observation views, never controller/private tables.

calculation is a declarative arithmetic expression over registered metric IDs and versions, with explicit compatible population/alignment rules. Support +, -, *, / only in v1. Parse an expression tree; never eval supplied code.

Example addition:

~~~json
{
  "metric_id": "payment_timeout_rate",
  "version": 1,
  "name": "Payment timeout rate",
  "definition": "Fraction of finalised payment attempts ending in timeout.",
  "unit": "ratio",
  "population": "finalised_payment_attempts_by_finalisation_time",
  "time_semantics": "event_time",
  "dimensions": ["country", "platform", "payment_provider", "checkout_release_version"],
  "supported_granularities": ["5m", "15m", "1h"],
  "source_ids": ["payments"],
  "calculation": {
    "operator": "divide",
    "left": {"metric_id": "payment_timeouts", "version": 1},
    "right": {"metric_id": "payment_attempts", "version": 1}
  },
  "maturity_delay_seconds": 0,
  "null_policy": "null_on_zero_denominator",
  "aggregation_policy": "ratio_of_sums",
  "effective_from": "2026-11-07T00:00:00Z",
  "owner": "payments"
}
~~~

No graph parent, edge list, suspected incident, alert threshold, or scenario ID is accepted. Arithmetic references legitimately reveal dependencies but do not certify compatible units/populations or establish causality.

## Contributor lifecycle

Proposed private endpoint: POST /admin/metrics with metric contract and idempotency key; GET /admin/registrations/{id} returns state. Admin is outside MCP observation tools.

States: submitted -> validating -> registered or rejected; registered metrics then move through warming to active in the monitoring runtime. Validation rejects malformed calculations, unsupported dimensions, inaccessible queries, conflicting versions, and cyclic arithmetic definitions. Semantic ambiguity may register a metric with provisional graph relationships; it must not manufacture certainty.

Registration produces a new catalog revision visible on the next snapshot. The runtime polls revisions each tick; it does not depend on MCP notifications. Historical backfill uses only records visible in the current snapshot and the contract's effective_from. Choose effective_from early enough to supply baseline history for demo additions.

## Bounds and errors

Defaults: maximum 24h event window per query_events call, 14 days for aggregate queries, two group_by dimensions per aggregate query, 500 rows per page, five seconds per query, and per-tick cost budgets from the harness. Aggregates that exceed 500 rows require a narrower query; never silently truncate sums. Raw evidence may be paginated.

Errors carry code, message, retryable, request_id. Codes include INVALID_ARGUMENT, UNKNOWN_METRIC, INVALID_DIMENSION, SNAPSHOT_NOT_FOUND, FUTURE_WINDOW, UNSUPPORTED_AGGREGATION, BUDGET_EXCEEDED, QUERY_TIMEOUT. Distinguish protocol-invalid requests from domain failures; use the SDK's appropriate tool-error mechanism.

Contract checks must demonstrate rejected future reads, unavailable dimensions, unsupported time aggregation, mismatched versions, cross-run evidence IDs, and inaccessible controller state.
