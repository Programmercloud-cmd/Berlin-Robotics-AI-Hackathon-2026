# Eye of the System — implementation planning

Planning branch: `plan/jacob`. This branch prepares a handoff for a subsequent Codex implementation session; it does not implement the application.

## Objective

Continuously observe a simulated marketplace, independently detect meaningful changes, investigate competing explanations using an evolving metric graph, and communicate evidence, impact, uncertainty, persistence, and recovery.

Contributors supply metric definitions and executable queries/calculations. They do not supply graph placement, causal edges, fault labels, investigation instructions, or metric-specific alert thresholds.

## Read in order

1. [Metric catalog](docs/metrics.md): the agreed 36-metric scope and proposed calculation contracts.
2. [Company simulation](docs/simulation.md): a proposed reproducible marketplace and independent observation boundary.
3. [Observation interface](docs/interface.md): proposed MCP tools, metric registration, result contracts, and access boundaries.
4. [System harness](docs/harness.md): proposed bootstrap, graph extension, monitoring, investigation, and incident lifecycle.
5. [Implementation handoff](docs/implementation.md): milestones, acceptance checks, and decisions still open.

## Decision status

**Agreed in the planning conversation:** marketplace simulation; the 36 metrics; contributors specify meaning and access but not graph placement; autonomous fault discovery; validated graph extension; separation of simulator truth from agent observations.

**Proposed defaults for review:** all concrete simulation parameters, schemas, transport, runtime layout, monitoring algorithms, budgets, timings, and demo sequence in these documents. They make the plan implementable but are not claims of user approval or measured performance. Implement coherent defaults when necessary and record changes.

The earlier draft metric IDs such as `traffic`, `conversion_rate`, and `revenue` are superseded by the explicit definitions in the catalog. Do not implement ambiguous aliases.

## Scope boundaries

Build one small coherent company, not arbitrary company reconstruction. Simulated documents and messages supply context; real Slack, intranet, and advertising integrations are unnecessary for the first version. Start with payments plus one innocent change, then expand to the other scenario families.

All 36 metrics are supported by the simulator. Demo holdbacks affect registration, not whether underlying events exist. A withheld derived ratio may add a useful graph representation without adding information beyond its already-available components.

## Source brief

[Almedia track in the hackathon brief](https://quick-chestnut-0cd.notion.site/2nd-edition-Berlin-Robotics-AI-Hackathon-01c30736e47a83fe85388156949d2240).

The brief weights metric structure/extension and autonomous monitoring/investigation equally (40% each); the working demo is 20%. Demonstrate fault detection, an innocent change, and adding a metric.
