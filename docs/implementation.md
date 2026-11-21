# Implementation handoff

Status: proposed delivery plan. No executable system exists in this branch yet.

## Implementer orientation

Read README and the four linked design documents before coding. The metric catalog is the current 36-metric scope; earlier generic traffic/revenue metrics are obsolete. Preserve the separation between metric registration by a contributor and fault discovery by the agent.

These documents propose a complete default architecture. User-agreed requirements and proposed details are distinguished in README. Record changes to defaults rather than silently diverging. Do not block routine implementation on every parameter; ask only about decisions that materially change the objective or scope.

## Suggested layout

- contracts/: metric, query result, graph edge, incident, and decision schemas
- simulator/: world generation, event delivery, context generation, private scenario controller
- observation/: read-only query service and MCP adapter
- monitoring/: registry integration, graph, baselines, scheduler, investigator, incidents
- evaluation/: private ground truth, replay runner, acceptance checks
- ui/: graph/decision/incident views, with a separate operator-only control view
- tests/: contract, invariant, integration, and blind replay checks

Proposed implementation default: Python for simulator, observation service, and monitoring core; SQLite for persistent event/agent stores; MCP SDK adapter; a small web UI using the implementer's chosen stack. This is a convenience choice, not an existing dependency or user requirement. Keep domain contracts independent of transport and model provider.

Keep simulator/controller and agent state in different databases and access contexts. Observation queries can use indexed relational projections; a graph database is not required for a 36-metric graph. Store nodes/edges with versions and provenance in ordinary tables initially.

## Milestones

### 1. Contracts and coherent world

Implement all source schemas, virtual clock, seeded generator, delivery pipeline, and 36 metric queries. Preserve the catalog's units, populations, supported dimensions, null policies, and special aggregation rules.

Checks: deterministic same-seed event replay; no negative stock; payment/order linkage; mature funnel subsets; exact financial reconciliation; ratio-of-sums; zero versus missing data; retries and cross-bucket outcomes.

Exit: one command creates baseline and demo observations, with no agent yet required.

### 2. Observation adapter and registration

Implement bounded domain queries and MCP surface, contributor metric registration, snapshot consistency, provenance/evidence retention, and error schemas. Support the 33+3 registration split.

Checks: no future records or unpublished context; cross-run evidence rejection; no controller access; schema rejection for unsupported dimensions; multi-day budget rejection; same arguments/snapshot yield same results.

Exit: a separate MCP client can list registered metrics, query evidence, and observe a new catalog revision after registration.

### 3. Graph bootstrap and extension

Implement parsed arithmetic edges, semantic candidate edges, validation, provenance/status, historical backfill, and graph versioning.

Checks: new metric registers without a parent; incompatible units/populations are not accepted as validated edges; arithmetic cycles rejected; ambiguous semantic relationships remain provisional; no manually seeded answer graph.

Exit: the three additions create useful, inspectable graph changes. Derived metrics do not masquerade as independent evidence.

### 4. Autonomous vertical slice

Implement scheduler, baselines, quality gates, query planning, incident lifecycle, and persistence for the payment regression plus innocent campaign.

Checks: start agent once, inject hidden fault outside its tools, observe independent detection and investigation before metric addition. Check correct segment/provider, evidence-backed uncertainty, recovery, and no duplicate incident on restart.

Exit: complete end-to-end unattended replay with traceable decisions.

### 5. Breadth, evaluation, and presentation

Add search, recommendation availability, pacing, profitability, and delivery-delay scenarios. Add normal controls and variations in segment/provider/seed. Build graph and incident views from real runtime records.

Checks: a read-only UI reflects actual decisions; no scripted alert times or canned diagnosis text; innocent/delivery changes handled with appropriate qualification; operator truth remains outside agent input.

Exit: reproducible demo plus evaluation summary.

## Evaluation harness

Run detector/investigator blind. The evaluator alone loads onset, mechanism, scope, restoration time, and counterfactual simulator expectations. Evaluate after the run; never feed the score or expected diagnosis back into the active agent.

Proposed minimum evaluation matrix:
- Three unseen seeds each for normal control, innocent campaign, payment fault, and ingestion delay.
- One initial seed per additional scenario family; expand when runtime allows.
- Vary fault onset, affected scope/provider, and unrelated releases.
- Add each heldback metric in a separate run as well as the primary narrative.
- Restart the runtime during an open incident.
- Include malformed/ambiguous metric registrations and a sparse-population case.

Metrics to report: autonomous detection recall, false incidents in normal/innocent runs, simulated detection latency, affected-segment accuracy, evidence coverage of report claims, unsupported causal assertions, duplicate updates, recovery latency, coverage gaps, query/model costs, and graph-validation outcomes.

Initial acceptance targets, to calibrate rather than claim as achieved:
- Payment regression detected within 30 simulated minutes of observable impact and before T+135 registration in primary replay.
- Affected DE/Android segment and provider identified with cited numerical evidence; release attribution qualified.
- Innocent campaign does not create an unsupported business-failure incident.
- Delivery delay is identified as a measurement issue or explicitly unresolved, not confidently called an actual sales outage.
- Recovery resolved within 45 simulated minutes after source-complete, mature observations return to normal.
- New metrics appear automatically in graph and monitoring, or visibly enter warming when history is insufficient.
- At least one scenario per family investigated through observed data rather than a scenario-to-report lookup.

Do not tune and evaluate on identical seeds only. Capture failures and unresolved uncertainty rather than suppressing them to make the demo pass.

## Planned operator commands

These are command contracts to implement, not commands currently available:
- make baseline: generate the 14-day historical observation store.
- make demo: start isolated services, agent, UI, and primary replay.
- make evaluate: run blind replay matrix and produce machine-readable plus readable results.
- make check: run schemas, invariants, adapter contracts, and integration checks.

Configuration includes seed, replay time range, source delays, scenario composition, model/provider credentials, and budgets. Only operator/evaluator configuration contains seed/scenarios; the runtime receives a separately generated observation-only configuration.

## Decisions still open

- Model/provider choice and available hackathon credits.
- UI stack and how much visual polish the weekend permits.
- Whether all additional fault families must be demo-ready, versus a payments-first vertical slice.
- Exact simulation/baseline parameters after calibration.
- Operational relevance edge scoring: start with interpretable rules plus LLM candidates, then refine if evidence warrants it.

The contracts and autonomy boundary should survive these choices.
