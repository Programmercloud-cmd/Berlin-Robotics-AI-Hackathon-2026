# Autonomous system harness

Status: proposed algorithm and runtime defaults, to be implemented and calibrated. No performance claims have been measured.

## Responsibilities and state

The runtime owns a persistent metric graph, baseline summaries, monitoring queue, hypothesis/evidence records, incidents, and an append-only decision log. An LLM assists semantic inference and query planning; deterministic code performs queries, statistics, arithmetic, schema checks, scheduling, and state transitions.

It starts once and responds to observation ticks and catalog revisions. A human does not ask "why did orders drop?" or select the affected segment. The UI can inspect decisions but does not initiate the detection loop.

## Bootstrap and new metrics

1. Read company charter, source descriptions, and all registered metric contracts.
2. Create metric nodes with immutable IDs and versioned definitions. Source/dimension nodes carry provenance from the catalog.
3. Extract arithmetic dependencies from parsed calculations and query lineage. Generate semantic dependency candidates from definitions and shared entities/dimensions.
4. Validate units, compatible populations, time semantics, denominator/subset requirements, and sample query results. Formula equality on sample data alone is insufficient.
5. Store edge type, evidence, validation checks, confidence/status, and creation/revision timestamps.
6. Backfill normal historical observations, infer baselines, and schedule initial monitoring.
7. On catalog revision, repeat these steps only for changed/new contracts and affected neighbours. Never require a contributor to place a node.

Start with no manually wired business edges. Configured objectives may nominate orders and contribution as important outcomes; contribution can remain an unresolved objective until registered. Objective priorities do not supply relationships.

A metric with insufficient history enters warming and uses conservative change checks; it must not silently become unmonitored. Rejected or ambiguous relationships remain visible with a reason.

## Graph representation

| Edge type | Meaning | Validation |
| --- | --- | --- |
| arithmetic_component | Operand in a supplied/parsed formula | Parse tree, units, population/time alignment, reconciliation |
| breakdown_by | Metric supports a dimension or segment | Source schema, query support, disjointness/additivity where applicable |
| sourced_from | Metric derives from a source/view | Trusted query metadata and lineage |
| possible_driver | Business mechanism that may influence another metric | Definitions/lineage/mechanism plus observational tests; remains provisional without stronger evidence |
| associated_with | Measured co-movement | Sample size, windows, effect and uncertainty; never label as causal |

A browse tree is a projection of the graph, not the storage model. A metric can have several relevant relationships. Enforce acyclic arithmetic dependencies, not an acyclic graph overall.

Definitions and historical evidence cannot be silently changed by the agent. Inferred edges, monitoring priorities, and baselines are revisable. Definition changes create versions and invalidate incompatible baselines/edges.

## Each autonomous tick

1. Acquire a snapshot and check catalog revision/source quality.
2. Allocate monitoring coverage and execute due baseline checks.
3. Rank unexplained changes by effect size, support, business importance, and novelty.
4. Open or update investigations without a user prompt.
5. Choose and execute bounded evidence queries against the same snapshot.
6. Persist conclusions, update incidents, and emit structured reports.
7. Record checkpoints and scheduling decisions; resume on the next tick.

Snapshot consistency is mandatory within a tick. New ticks may update an ongoing investigation with newer evidence, explicitly recording the new snapshot.

### Monitoring attention

Initial policy combines guaranteed coverage with adaptive focus:
- Check registered metric totals at least once every 15 simulated minutes.
- Check session_conversion_rate and checkout_completion_rate by country x platform every 15 minutes. Check orders by country/platform plus payment_success_rate by country/platform/provider using supported one/two-dimensional queries and scoped followups.
- Rotate other supported one-dimensional slices so each dimension is visited at least every 30 minutes; examine two-dimensional intersections when a suspicious slice warrants it.
- Reserve capacity for new metrics, low-volume uncertainty, and unexplored dimensions. Do not spend the entire budget on already-open incidents.
- Record last-check time and coverage gaps. If budgets cannot sustain these promises, mark degraded coverage and reduce the monitored scope explicitly.

Suggested per-tick cap: 120 domain calls total, allocated 60 monitoring, 40 investigation, 20 extension/backfill; unused allocation can be borrowed while preserving coverage. Five LLM planning calls maximum per tick, with explicit input/output token limits. Query results are cached by snapshot and arguments.

### Detecting meaningful changes

Use metric-specific statistical families selected from the contract, not hand-coded fault signatures:
- Counts/currency: seasonal robust baselines using matching local-hour/day-type buckets from the 14-day history; use median and MAD with a nonzero noise floor.
- Rates: compare numerator/denominator against the baseline with interval estimates; require adequate support. Preserve ratio-of-sums semantics.
- Snapshots and cumulative budgets: compare against expected trajectories/scheduled curves, not generic bucket-sum detectors.
- Sparse series: coarsen buckets or report insufficient support. Use related outcomes to prioritise examination.

Proposed starting candidate rule: robust deviation >4 scale units, relative change >20%, and persistence across two distinct completed buckets. Rate candidates additionally require denominator >=50 in the compared window and an uncertainty interval inconsistent with baseline. These are general starting heuristics, not a universal detection guarantee; tune using normal and fault replays.

Check completeness and maturity before interpreting deviations. Compare against expected campaign changes and seasonal patterns; an announcement supplies a hypothesis, not blanket suppression. Unexpected conversion, cost, or segment behaviour during a planned campaign still warrants investigation.

Repeated segment searches inflate false positives. Use persistence, minimum support, effect size, grouped incident handling, and empirical normal-run calibration in v1. Do not claim formal multiple-testing guarantees from these heuristics.

## Investigation policy

Represent each investigation as hypotheses with supporting/refuting evidence and a query queue. Initial generic questions:
- Is this measurement incomplete or immature?
- Is demand changing, or is a downstream stage behaving differently?
- Which segments explain the deviation?
- Do linked metrics support the same mechanism?
- Do published operational changes match timing/scope, and what alternatives remain?

Choose the next query by an explainable priority: expected discrimination between live hypotheses x affected business magnitude / estimated query cost. Implement an ordinal scoring rubric initially, not a claim of calibrated information gain.

For an order decline, the graph may lead toward sessions and conversion, then checkout and payments, or search/feed/inventory. This is an example of possible traversal, not a hardcoded payment investigation script. A supplied formula supports arithmetic reasoning; a possible_driver edge licenses testing, not a causal conclusion.

For each chosen query, log the question, candidate explanations it distinguishes, expected interpretation, arguments, and result evidence IDs. Use structured decision summaries, not hidden chain-of-thought transcripts.

### Contributions and stopping

- For disjoint additive segments, attribute total change using each segment's observed minus expected amount. Report unexplained residual and avoid double-counting overlapping country/platform slices.
- For conversion-related lost orders, use affected mature checkout cohort size x (baseline completion rate - observed completion rate), with uncertainty and assumptions. Label this an estimate; do not mix confirmation-time orders with session-cohort denominators as an exact identity.
- For contribution, reconcile changes in each arithmetic component on common country/channel/calendar windows.
- Stop when the material anomaly is scoped, the strongest supported explanation and alternatives are documented, source quality is checked, and additional queries are unlikely to change the conclusion; or when the budget/time cap is reached.
- Budget exhaustion produces an explicitly incomplete investigation, not a fabricated diagnosis.

Operational attribution to a release remains "consistent with"/"likely" when supported only by timing and segment alignment. A fault disappearing after rollback strengthens the case but is not an experimental proof when other conditions also changed.

## Incident lifecycle

States: investigating -> open -> recovering -> resolved; alternatives dismissed (explained innocent change) or inconclusive (insufficient evidence). Maintain one incident for related symptoms and update it instead of emitting repeated alerts.

Identity uses affected business area, segment, onset proximity, and shared evidence; it must not use the hidden scenario ID. Record linked symptoms rather than treating orders, conversion, and payment success as three independent incidents.

Suggested policy: material updates at most once per 15 simulated minutes unless severity changes; recovering after two normal mature buckets; resolved after three normal mature buckets with adequate completeness. Reopen on recurrence and preserve history.

Structured report fields:
- incident_id, status, first_seen, last_updated, snapshot_id
- affected_metrics, affected_scope, observed/expected values, effect/support/uncertainty
- estimated_business_impact and assumptions
- supported_findings, provisional_explanations, alternatives_tested
- source_quality, evidence_ids, next_check_at
- concise readable explanation

No automated remediation in v1.

## Visibility and restart

UI: graph with typed/status-labelled edges; current coverage and baselines; chronological decisions/queries; incident evidence; recovery timeline. Show provisional and validated edges distinctly. A separate operator panel injects faults and displays evaluation truth.

Persist cursor/snapshot, catalog revision, active jobs, baseline versions, incidents, and idempotent report IDs. Restarting must resume without duplicate alerts. Store model/version/config and decision outputs for replay; exact LLM reproducibility is not assumed.

## Acceptance boundaries

The agent has no controller tools, private filesystem, fault seed, or evaluation truth. A fake release and unrelated message must not by themselves produce a definitive diagnosis. Detection must precede demo metric registration in the primary replay. Metrics may be monitored with uncertain graph edges; uncertainty must not block all coverage.
