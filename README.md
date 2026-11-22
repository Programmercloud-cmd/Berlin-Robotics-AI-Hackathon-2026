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

Run `python -m eye lab` to open the lab directly. The Marketplace dashboard uses port 8000, the Lab uses 8002, and Support Eye uses 8004. Each company runs its own simulator process, PostgreSQL instance, and Eye dashboard. The first marketplace startup saves the seed-42 history under `.runtime/seasonality/`. Later startups restore that baseline into a fresh database. Ctrl+C stops the session.

On systems where Python's `ensurepip` is unavailable, create the environment with `python3 -m venv --without-pip .venv` and install with `python3 -m pip --python .venv/bin/python install -r requirements-local.txt`.

### Two-company demo

With Devin configured in `.env`, start the Lab with the shared investigation policy for both companies.

```sh
EYE_INVESTIGATION=generic EYE_MODEL=devin DEVIN_MODE=lite python -m eye lab
```

Open the [Marketplace tab](http://127.0.0.1:8002/) or the [Support Tickets tab](http://127.0.0.1:8002/?company=support). In **Support Tickets**, preview and apply the healthy intake surge, then the specialist regression. The regression includes a healthy high-volume lead-in and three recovery hours. **Preview replay** reviews the same case settings before publishing at the next virtual time. Use **Open Support Eye** or **Open incident** to follow Devin's saved queries from the report. Support registers its resolution metric through Eye's metric registration endpoint. The formula attaches to discovered activities during registration, and real Devin investigates any breach.

To rehearse alongside a running marketplace demo, pass `--port 8066 --eye-port 8065 --support-port 8067` to the same command. The operator continues to use 8001.

## Metric catalog

Eye’s **Metric catalog** lists all 36 definitions, their data sources, units, dimensions, latest reading availability, and alert settings. The 33 baseline metrics register at startup. `payment_timeout_rate`, `unavailable_feed_share`, and `marketplace_contribution` remain available for separate runtime registration. The payment regression registers `payment_timeout_rate` after detection. Use `POST /api/catalog/{metric_id}/register` to register a catalog metric or `POST /api/metrics` to register a custom calculation.

The original `product_views` and `cart_additions` map to distinct session-cohort counts. Payment attempts count finalized outcomes, with `payment_provider` as the catalog dimension. Search, feed, orders, inventory, advertising, and ingestion use their own populations and source metadata. The source of truth is [the 36-metric catalog on `plan/jacob`](https://github.com/Programmercloud-cmd/Berlin-Robotics-AI-Hackathon-2026/blob/plan/jacob/docs/metrics.md).

`GET /api/catalog/{metric_id}/readings` defaults to five-minute buckets. Pass `start`, `end`, `bucket_minutes`, an optional JSON `scope`, and an optional `as_of` timestamp to recompute from facts. Readings include units, dimensions, population, maturity, quality, and numerator and denominator where applicable. Eye evaluates a trailing 60-minute window every five virtual minutes after the 15-minute ingestion grace period. Baselines use comparable UTC hour and minute windows for the same scope, with rates calculated from pooled numerators and denominators. Budget utilization uses campaign budgets and spend from one Europe/Berlin business day per request.

## Run a scenario

1. Set `DEVIN_API_KEY` and `DEVIN_ORG_ID` in the repo-root `.env` for chat. Try “Frankfurt product pages fail to load” or “people struggle to add products to their cart.” Devin can map both premises to the same **Fewer successful completions** effect at different workflow transitions. Other premises can model gateway timeouts, delayed payment success receipts, or a faster search rollout with a holdout.
2. Review the transition, affected traffic, severity, and duration. Unspecified scope is marketplace-wide. For completion loss, severity is the fraction of otherwise successful attempts that the simulator prevents from completing. Click **Apply to upcoming windows** or **Inject into live traffic** when the preview is ready.
3. Read **Run timeline** for the applied plan, scenario start and end, Eye’s first alert and scope, and recorded recovery. Follow the incident link for the evidence and diagnosis. Alert details show both window bounds and the virtual evaluation time after the ingestion grace period. Real latency measures from the start of publishing the scenario to the persisted alert. Each scenario publishes four healthy hours afterward.

Use **Generate traffic** for a fixed number of normal hours, or click **Start continuous traffic** to keep publishing until you stop it. While traffic is streaming, approve a scenario with **Inject into live traffic**. It starts at the next unpublished virtual hour and normal traffic resumes at the scenario end. **Stop continuous traffic** finishes the current virtual hour or any already accepted injection, then allows late events to arrive. Normal traffic follows a repeating 28-day volume pattern with seeded variation, and each window advances the virtual clock and uses fresh event IDs. Select **Review replay** to preview a previous plan, or use **Download JSON** and **Review saved scenario** to preview it in a later lab session.

For the rolling detection replay, use a fresh seed-42 lab and import the following plan, then apply the preview.

```json
{"effect":"payment_timeout","transition":"payment","scope":{"country":"DE","platform":"mobile","provider":"Stripe"},"severity":0.85,"duration_hours":2}
```

The simulator map outlines a draft’s affected branch with dashes and uses a solid outline during the active scenario windows. Use **Show on map** in the run timeline to inspect an earlier run. The map counts outcomes within the selected scenario’s scope and affected windows, and the receipt lane updates as later windows publish.

The workflow diagram, scenario premises, and controls stay in the test lab. Eye reads observed requests, outcomes, and closed windows from PostgreSQL and compares successive funnel steps with their baselines. Both the lab chat and Eye use Devin by default when `DEVIN_API_KEY` and `DEVIN_ORG_ID` are set.

On Eye’s dashboard, **Graph** shows the observed workflow and **Table** opens the metric catalog. The eye menu includes **Journey**, which arranges the shopping path from left to right with search, recommendations, and receipt delivery in separate lanes. Selecting an incident highlights its observed evidence connections in plum and shows the scope above the graph. Use **Clear selection** to restore the full contrast. All three local views use the Bright and Dark palettes from `AI_Chronicle_UI`, and the header toggle follows you between views on the same host.

Each incident card shows the first measured workflow break, scope, completion rate versus baseline at detection, and recovery status. Eye measures completion among shoppers or attempts that reached the transition. Downstream volume alerts attach to the upstream incident when the downstream completion rate holds. Overlapping breaches of the same step and compatible scope stay attached to the open incident. A separate drop in conditional completion opens its own incident. Eye declares recovery after twelve consecutive healthy evaluations. The first report appears while a separate worker gathers deeper evidence. Each evidence branch shows what supports the explanation, what weakens it, and what remains unresolved. Later observations can update the branches after the incident recovers. **View evidence** shows the observations and event IDs.

## Regression checks

Run `python -m eye check --with-search` for the payment and search regressions in local PostgreSQL. Run `python -m unittest discover -s tests -p test_incident_grouping.py` to reproduce the cart-only and concurrent cart/payment cases.

### Planned November shopping replay

Run `python -m eye check-holiday` to replay the first hour of the November 22, 2026 shopping period with healthy checkout and with a checkout regression. The command uses the shared seed-42 history and writes the comparisons and incident evidence to `.runtime/seasonality/holiday-replay.json`.

Eye’s **Planned events** panel shows the date and scope, observed orders, the ordinary expectation, and the event-adjusted expectation and range. The archive keeps eight weeks of five-minute funnel counts and rate components, plus 365 days of daily aggregates. Eye uses complete same-weekday rolling windows for normal traffic and compares the prior event with same-weekday non-event days in each country/platform segment. A local trend fit estimates what the prior occasion would have looked like on an ordinary day. The display identifies the single prior occurrence and the uncertainty allowance. Traffic settings and checkout controls are private to the lab.

## Benchmark

Commit the code and start the lab from a clean checkout with `DEVIN_API_KEY` and `DEVIN_ORG_ID` set. Open [Benchmark](http://127.0.0.1:8002/benchmark) and click **Run benchmark**. The same SHA can run again. Eye uses the model settings from the lab launch. To benchmark statistical reports with Devin as the reviewer, set `EYE_MODEL=local` and leave `MODEL_NAME` unset. The reviewer uses its own Devin session.

Suite v1 runs ten seed-42 cases. Each case starts from an isolated copy of the same historical baseline, applies one hour of traffic or a fault, and follows it with three hours of normal traffic. Detection time is measured from the case start to Eye’s first alert. Recovery time is measured from the case end until all its incidents recover. The scoreboard displays any incidents in the two benign cases as false incidents. Case details show the actual alert, the incidents, and separate Devin verdicts for the initial report and deeper findings, with the captured telemetry for each phase.

The runner executes an archive of the recorded SHA. Scoreboard records and local logs are kept in `.runtime/benchmarks/` across lab restarts. Open the SHA link to read the local Git commit, or the run link to read case results. The Benchmark view and review rubrics are served by the Test Lab.

Click a scoreboard column heading to sort. Click again to reverse the order. Timing medians use measured values from fault cases, and runs without a measurement sort last.

Add a **Run description** before starting to record the change or question you are testing. The scoreboard uses the commit subject when the description is blank. Open **Case results** and expand **Run description and methodology** for the saved replay, timing, recovery, and review rules. The Seaborn plots compare detection, recovery, and incident counts by case and show Devin verdict counts by report phase. New runs save standalone PNGs beside the run record. Existing runs can render the same plots from their saved data.

## Live reasoning

The existing marketplace investigation and grouping remain the default. Opt in to the shared investigation policy with `EYE_INVESTIGATION=generic EYE_MODEL=devin`. Set both variables before launching the lab or Eye server. The feature branch starts from Eye commit `66c414f29a283699e87ce3df8bf11b22b34160db`.

In shared mode, Eye opens the incident from observed measurements and gives Devin the alert, scoped historical readings, sample sizes, delivery completeness, and typed graph neighbours. Devin can select up to three local reads per batch, with at most two batches. It can compare event counts or decompose a declared ratio into observed numerator and denominator contributions with a residual. Eye stores the reads, their fixed observation cutoff, and the cited conclusion inside **View evidence**. It attempts to continue the same Devin session and starts a continuation with the same evidence if necessary. Monitoring and recovery run independently. If the model cannot return a valid cited report, Eye saves its statistical report.

Shared grouping uses overlapping time and matching observed measurement inputs. A scoped rate alert can explain a broader alert when the estimated rate outside that scope holds near baseline. A count can join a ratio alert when it matches the declared numerator and the eligible volume holds near its historical reference. Sparse evidence and independent degradation stay separate. Marketplace workflow, checkout, search, inventory, and source-specific grouping stay in the default mode. The shared replays use only explicitly registered metrics. A publisher with its own catalog can set runtime instrumentation metadata to `{"catalog":"none"}` before startup.

To replay seed 42 with a local PostgreSQL instance, configure Devin in `.env` and run these in separate terminals. The dashboards remain available for inspecting the evidence until the commands stop. The fixture writes evidence to `.runtime/devin-investigation/` and prints each run directory.

```sh
EYE_INVESTIGATION=generic EYE_MODEL=devin DEVIN_MODE=lite python -m eye.investigation_replay marketplace --port 8030
EYE_INVESTIGATION=generic EYE_MODEL=devin DEVIN_MODE=lite python -m eye.investigation_replay support --port 8031
EYE_MODEL=local python -m eye.investigation_replay marketplace --port 8032
```

The support replay discovers ticket activities from the same event ingestion path, registers resolution per eligible ticket without placement hints, and compares a healthy intake increase with a scoped resolution break. The last command exercises the default marketplace path with the statistical writer.

Run `EYE_MODEL=codex python -m eye lab` to use an authenticated Codex CLI on your PATH. To use an OpenAI-compatible JSON chat completions provider, leave `EYE_MODEL` empty and set `MODEL_NAME`, `MODEL_API_KEY` and, if applicable, `MODEL_BASE_URL`. Set `EYE_MODEL=local` to use Eye’s statistical reports and calculation-based curation directly. Eye also uses those fallbacks when its reasoning provider is unavailable. The report records which reasoning path ran. [Environment example](.env.example)

### Devin API v3

Set `DEVIN_API_KEY` and `DEVIN_ORG_ID` in the repo-root `.env`. Use one organization service user with permission to create Devin sessions. Eye creates a separate on-demand session for each investigation or curation request and submits only that role’s context and JSON schema. Eye gathers evidence, validates the results, applies graph changes and tracks recovery locally. [Devin service user authentication](https://docs.devin.ai/api-reference/authentication)

Start the test lab launcher.

```sh
DEVIN_MODE=lite python -m eye operator
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
