# Eye of the System

Autonomous marketplace monitoring for Almedia’s Fully Agentic Monitoring track at the Berlin Robotics & AI Hackathon 2026.

## Launch the test lab

Python 3.12 is the verified runtime.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-local.txt
python -m eye operator
```

Open the [operator panel on port 8001](http://127.0.0.1:8001) and click **Start test lab**. The panel opens before PostgreSQL starts and shows baseline and discovery progress. Open the [test lab on port 8002](http://127.0.0.1:8002) to create a scenario. The [Eye dashboard on port 8000](http://127.0.0.1:8000) becomes available after discovery.

Run `python -m eye lab` to open the lab directly when ports 8000 and 8002 are free. Each lab session uses a temporary local PostgreSQL database and a separate simulator process. Ctrl+C stops the session.

On systems where Python's `ensurepip` is unavailable, create the environment with `python3 -m venv --without-pip .venv` and install with `python3 -m pip --python .venv/bin/python install -r requirements-local.txt`.

## Metric catalog

Eye’s **Metric catalog** lists all 36 definitions, their data sources, units, dimensions, latest reading availability, and alert settings. The 33 baseline metrics register at startup. `payment_timeout_rate`, `unavailable_feed_share`, and `marketplace_contribution` remain available for separate runtime registration. The payment regression registers `payment_timeout_rate` after detection. Use `POST /api/catalog/{metric_id}/register` to register a catalog metric or `POST /api/metrics` to register a custom calculation.

The original `product_views` and `cart_additions` map to distinct session-cohort counts. Payment attempts count finalized outcomes, with `payment_provider` as the catalog dimension. Search, feed, orders, inventory, advertising, and ingestion use their own populations and source metadata. The source of truth is [the 36-metric catalog on `plan/jacob`](https://github.com/Programmercloud-cmd/Berlin-Robotics-AI-Hackathon-2026/blob/plan/jacob/docs/metrics.md).

`GET /api/catalog/{metric_id}/readings` defaults to five-minute buckets. Pass `start`, `end`, `bucket_minutes`, an optional JSON `scope`, and an optional `as_of` timestamp to recompute from facts. Readings include units, dimensions, population, maturity, quality, and numerator and denominator where applicable. Eye evaluates a trailing 60-minute window every five virtual minutes after the 15-minute ingestion grace period. Baselines use comparable UTC hour and minute windows for the same scope, with rates calculated from pooled numerators and denominators. Budget utilization uses campaign budgets and spend from one Europe/Berlin business day per request.

## Run a scenario

1. Set `DEVIN_API_KEY` and `DEVIN_ORG_ID` in the repo-root `.env` for chat. Try “Frankfurt product pages fail to load” or “people struggle to add products to their cart.” Devin can map both premises to the same **Fewer successful completions** effect at different workflow transitions. Other premises can model gateway timeouts, delayed payment success receipts, or a faster search rollout with a holdout.
2. Review the transition, affected traffic, severity, and duration. Unspecified scope is marketplace-wide. For completion loss, severity is the fraction of otherwise successful attempts that the simulator prevents from completing. Click **Apply to upcoming windows** when the preview is ready.
3. Read **Run timeline** for the applied plan, scenario start and end, Eye’s first alert and scope, and recorded recovery. Follow the incident link for the evidence and diagnosis. Alert details show both window bounds and the virtual evaluation time after the ingestion grace period. Real latency measures from the start of publishing the scenario to the persisted alert. Each scenario publishes four healthy hours afterward.

Use **Generate traffic** to publish normal hours while Devin is preparing a plan. Normal traffic follows a repeating 28-day volume pattern with seeded variation. Every generated window advances the virtual clock and uses fresh event IDs. Select **Review replay** to preview a previous plan, or use **Download JSON** and **Review saved scenario** to preview it in a later lab session. Applying the preview uses the next virtual windows.

For the rolling detection replay, use a fresh seed-42 lab and import the following plan, then apply the preview.

```json
{"effect":"payment_timeout","transition":"payment","scope":{"country":"DE","platform":"mobile","provider":"Stripe"},"severity":0.85,"duration_hours":2}
```

The simulator map outlines a draft’s affected branch with dashes and uses a solid outline during the active scenario windows. Use **Show on map** in the run timeline to inspect an earlier run. The map counts outcomes within the selected scenario’s scope and affected windows, and the receipt lane updates as later windows publish.

The workflow diagram, scenario premises, and controls stay in the test lab. Eye reads observed requests, outcomes, and closed windows from PostgreSQL and compares successive funnel steps with their baselines. Set `EYE_MODEL=devin` when launching the operator or lab if Eye should also use Devin for incident reports. The lab chat uses Devin with valid credentials regardless of Eye’s chosen report provider.

On Eye’s dashboard, **Graph** shows the observed workflow and **Table** opens the metric catalog. The eye menu includes **Journey**, which arranges the shopping path from left to right with search, recommendations, and receipt delivery in separate lanes. Selecting an incident highlights its observed evidence connections in plum and shows the scope above the graph. Use **Clear selection** to restore the full contrast. All three local views use the Bright and Dark palettes from `AI_Chronicle_UI`, and the header toggle follows you between views on the same host.

Each incident card shows the first measured workflow break, scope, completion rate versus baseline at detection, and recovery status. Eye measures completion among shoppers or attempts that reached the transition. Downstream volume alerts attach to the upstream incident when the downstream completion rate holds. Overlapping breaches of the same step and compatible scope stay attached to the open incident. A separate drop in conditional completion opens its own incident. Eye declares recovery after twelve consecutive healthy evaluations. The first report appears while a separate worker gathers deeper evidence. Each evidence branch shows what supports the explanation, what weakens it, and what remains unresolved. Later observations can update the branches after the incident recovers. **View evidence** shows the observations and event IDs.

## Regression checks

Run `python -m eye check --with-search` for the payment and search regressions in local PostgreSQL. Run `python -m unittest discover -s tests -p test_incident_grouping.py` to reproduce the cart-only and concurrent cart/payment cases.

## Benchmark

Commit the code and start the lab from a clean checkout with `DEVIN_API_KEY` and `DEVIN_ORG_ID` set. Open [Benchmark](http://127.0.0.1:8002/benchmark) and click **Run benchmark**. The same SHA can run again. Eye uses the model settings from the lab launch. To benchmark statistical reports with Devin as the reviewer, leave `EYE_MODEL` and `MODEL_NAME` unset. The reviewer uses its own Devin session.

Suite v1 runs ten seed-42 cases. Each case starts from an isolated copy of the same historical baseline, applies one hour of traffic or a fault, and follows it with three hours of normal traffic. Detection time is measured from the case start to Eye’s first alert. Recovery time is measured from the case end until all its incidents recover. The scoreboard displays any incidents in the two benign cases as false incidents. Case details show the actual alert, the incidents, and separate Devin verdicts for the initial report and deeper findings, with the captured telemetry for each phase.

The runner executes an archive of the recorded SHA. Scoreboard records and local logs are kept in `.runtime/benchmarks/` across lab restarts. Open the SHA link to read the local Git commit, or the run link to read case results. The Benchmark view and review rubrics are served by the Test Lab.

## Live reasoning

Run `EYE_MODEL=codex python -m eye lab` to use an authenticated Codex CLI on your PATH. To use an OpenAI-compatible JSON chat completions provider, copy [the environment example](.env.example) to `.env` and set `MODEL_NAME`, `MODEL_API_KEY` and, if applicable, `MODEL_BASE_URL`. With no reachable provider, graph curation uses the registered calculations and the investigator produces a statistical report. The report records which reasoning path ran.

### Devin API v3

Set `DEVIN_API_KEY` and `DEVIN_ORG_ID` in the repo-root `.env`. Use one organization service user with permission to create Devin sessions. Eye creates a separate on-demand session for each investigation or curation request and submits only that role’s context and JSON schema. Eye gathers evidence, validates the results, applies graph changes and tracks recovery locally. [Devin service user authentication](https://docs.devin.ai/api-reference/authentication)

Start the test lab launcher.

```sh
EYE_MODEL=devin DEVIN_MODE=lite python -m eye operator
```

Start the test lab from the [operator panel](http://127.0.0.1:8001), then follow its dashboard link. Each Devin session can take a few minutes to start and respond.

For terminal-only verification, run `EYE_MODEL=devin DEVIN_MODE=lite python -m eye check`. The check requires the recovered payment report and an accepted `payment_timeout_rate` graph relationship to carry provenance from distinct Devin sessions. It exits after verification.

Omit `DEVIN_MODE` to use the organization’s default agent mode. [Devin session creation](https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions)

## Use Supabase or your own PostgreSQL

Set `DATABASE_URL` in `.env` to a PostgreSQL connection string. For a hosted Supabase project, use a direct connection or the session pooler. The app uses the private `eye` schema and needs a role that can create it. See [Supabase database connections](https://supabase.com/docs/guides/database/connecting-to-postgres).

```sh
python -m pip install -r requirements.txt
python -m eye init
python -m eye serve
```

In a second terminal with the same configuration, run `python -m eye simulate` to generate virtual hours continuously. The simulator advances the publication clock in five-minute increments in PostgreSQL. Scenario controls stay in the test lab.

## Interfaces

- `POST /api/metrics` registers a metric with `id`, `name`, `definition`, `calculation`, optional `dimensions` and `access`. For example, `{"op":"ratio","activity":"payment_timed_out","denominator_activity":"payment_attempted"}` calculates timeout rate from observed events. Use `where` for equality filters on event attributes. Interactive request documentation is at [`/docs`](http://127.0.0.1:8000/docs).
- [`/mcp`](http://127.0.0.1:8000/mcp) is a Streamable HTTP MCP endpoint. Tools read metric definitions, observations and breakdowns, graph neighbours, and supporting events. The investigation uses the same observation tools.
- `/api/state` and `/api/incidents/{id}/evidence` supply the dashboard with persisted graph, anomalies, reports and evidence.

The baseline and live events use a relational OCEL 2.0 representation with events, objects and qualified links. The monitor compares closed windows against historical patterns. Metric values stay in PostgreSQL and graph edges carry their type and provenance.
