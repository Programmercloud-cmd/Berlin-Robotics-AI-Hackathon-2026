# Eye of the System

Autonomous marketplace monitoring for Almedia’s Fully Agentic Monitoring track at the Berlin Robotics & AI Hackathon 2026.

## Run the demo

Python 3.12 is the verified runtime.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-local.txt
python -m eye operator
```

Open the [operator panel on port 8001](http://127.0.0.1:8001) and click **Start fresh payment rehearsal**. The panel is available before PostgreSQL starts, shows baseline and discovery progress, and links to the [judge dashboard on port 8000](http://127.0.0.1:8000) when it is ready. Keep the launcher running to keep both views available.

Each rehearsal uses a fresh temporary PostgreSQL database. Starting another rehearsal replaces the launcher's previous run. If a separate Eye process already owns port 8000, stop it before starting a rehearsal from the panel.

Run `python -m eye demo` to launch the dashboard directly. Use `python -m eye demo --delay 3` for a slower walkthrough or `python -m eye demo --check` for terminal-only verification. The demo advances a virtual hour at each step and leaves the interactive dashboard available after the scenario finishes.

On systems where Python's `ensurepip` is unavailable, create the environment with `python3 -m venv --without-pip .venv` and install with `python3 -m pip --python .venv/bin/python install -r requirements-local.txt`.

1. PM4Py discovers shopping, payment and inventory lifecycles from qualified object-event relations. The dashboard shows the resulting OC-DFG and linked metrics.
2. Normal operation and a jump in traffic produce healthy windows.
3. The monitor shows the observed payment-success anomaly, affected segment, graph path and supporting evidence. With a reasoning provider configured, the incident shows **Investigation pending** while Eye continues processing windows.
4. The demo registers `payment_timeout_rate` after detection and backfills its observation history. Calculation-based graph links are available during the rehearsal. Provider curation adds its validated graph relationships after the recovery windows.
5. Eye tracks recovery after two healthy windows. The validated report attaches to the same incident even if recovery is already recorded. Eye uses its statistical report when the provider fails or returns unusable output.

Run `python -m eye demo --with-search` to continue into a second incident. The simulator emits search requests and clicks in both modes. With the search scenario enabled, the monitor detects a segmented drop in search click rate and follows it through recovery. Add `--check` to verify both cases without opening a server.

## Metric catalog

Eye’s **Metric catalog** lists all 36 definitions, their data sources, units, dimensions, latest reading availability, and alert settings. The 33 baseline metrics register at startup. `payment_timeout_rate`, `unavailable_feed_share`, and `marketplace_contribution` remain available for separate runtime registration. The payment rehearsal registers `payment_timeout_rate` after detection. For other runs, use `POST /api/catalog/{metric_id}/register` to register a catalog metric or `POST /api/metrics` to register a custom calculation.

The original `product_views` and `cart_additions` map to distinct session-cohort counts. Payment attempts count finalized outcomes, with `payment_provider` as the catalog dimension. Search, feed, orders, inventory, advertising, and ingestion use their own populations and source metadata. The source of truth is [the 36-metric catalog on `plan/jacob`](https://github.com/Programmercloud-cmd/Berlin-Robotics-AI-Hackathon-2026/blob/plan/jacob/docs/metrics.md).

`GET /api/catalog/{metric_id}/readings` defaults to five-minute buckets. Pass `start`, `end`, `bucket_minutes`, an optional JSON `scope`, and an optional `as_of` timestamp to recompute from facts. Readings include units, dimensions, population, maturity, quality, and numerator and denominator where applicable. The simulator publishes closed hourly windows with minute source manifests, and the payment and search rehearsals retain hourly alerting. Budget utilization uses campaign budgets and spend from one Europe/Berlin business day per request.

## Local test lab

Start `python -m eye operator` and choose **Start test lab** on the [operator panel](http://127.0.0.1:8001). Open its [test lab link on port 8002](http://127.0.0.1:8002). The lab starts a fresh local PostgreSQL instance, a separate simulator process, and the [Eye dashboard on port 8000](http://127.0.0.1:8000). To open the lab directly with port 8000 free, run `python -m eye lab`. Stop the launcher or lab with Ctrl+C to close the local run.

1. Set `DEVIN_API_KEY` and `DEVIN_ORG_ID` in the repo-root `.env` for chat. Try “simulate payment timeouts in Germany for three hours” or “product pages hosted in Frankfurt fail to load.”
2. Review the proposed simulator effect, affected traffic, severity, and duration. The Frankfurt premise can be modeled as fewer successful product views. Hosting geography stays in the lab. Click **Apply to upcoming windows** when the preview is ready.
3. Read **Run timeline** for the applied plan, scenario start and end, Eye’s first alert and scope, and recorded recovery. Follow the incident link for the evidence and diagnosis. The alert’s virtual timestamp is the end of its observation window. Real latency measures from the start of publishing the scenario to the persisted alert. Each scenario publishes four healthy hours afterward.

Use **Generate traffic** to publish normal hours while Devin is preparing a plan. Normal traffic follows a repeating 28-day volume pattern with seeded variation. Every generated window advances the virtual clock and uses fresh event IDs. Select **Review replay** to preview a previous plan, or use **Download JSON** and **Review saved scenario** to preview it in a later lab session. Applying the preview uses the next virtual windows.

The workflow diagram, scenario premises, and controls stay in the test lab. Eye reads observed events and closed windows from PostgreSQL. Set `EYE_MODEL=devin` when launching the operator or lab if Eye should also use Devin for incident reports. The lab chat uses Devin with valid credentials regardless of Eye’s chosen report provider.

On Eye’s dashboard, **Graph** shows the observed workflow and **Table** opens the metric catalog. Use the eye button to switch graph views, **Fullscreen** to expand the graph, and the edge controls to reopen collapsed panels. All three views use the light and dark theme tokens from `AI_Chronicle_UI`.

## Live reasoning

Run `EYE_MODEL=codex python -m eye demo` to use an authenticated Codex CLI on your PATH. To use an OpenAI-compatible JSON chat completions provider, copy [the environment example](.env.example) to `.env` and set `MODEL_NAME`, `MODEL_API_KEY` and, if applicable, `MODEL_BASE_URL`. With no reachable provider, graph curation uses the registered calculations and the investigator produces a statistical report. The report records which reasoning path ran.

### Devin API v3

Set `DEVIN_API_KEY` and `DEVIN_ORG_ID` in the repo-root `.env`. Use one organization service user with permission to create Devin sessions. Eye creates a separate on-demand session for each investigation or curation request and submits only that role’s context and JSON schema. Eye gathers evidence, validates the results, applies graph changes and tracks recovery locally. [Devin service user authentication](https://docs.devin.ai/api-reference/authentication)

Start the interactive demo and leave it running.

```sh
EYE_MODEL=devin DEVIN_MODE=lite python -m eye operator
```

Start a rehearsal from the [operator panel](http://127.0.0.1:8001), then follow its dashboard link. Each Devin session can take a few minutes to start and respond.

For terminal-only verification, run `EYE_MODEL=devin DEVIN_MODE=lite python -m eye demo --check`. The check requires the recovered payment report and an accepted `payment_timeout_rate` graph relationship to carry provenance from distinct Devin sessions. It exits after verification.

Omit `DEVIN_MODE` to use the organization’s default agent mode. [Devin session creation](https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions)

## Use Supabase or your own PostgreSQL

Set `DATABASE_URL` in `.env` to a PostgreSQL connection string. For a hosted Supabase project, use a direct connection or the session pooler. The app uses the private `eye` schema and needs a role that can create it. See [Supabase database connections](https://supabase.com/docs/guides/database/connecting-to-postgres).

```sh
python -m pip install -r requirements.txt
python -m eye init
python -m eye serve
```

In a second terminal with the same configuration, run `python -m eye simulate` to emit committed virtual hours continuously. In a clean Eye database, `python -m eye demo` can also use the configured PostgreSQL instance. The simulator and the monitoring service coordinate through completed windows in PostgreSQL. The operator’s controls stay in the simulator and demo CLI.

## Interfaces

- `POST /api/metrics` registers a metric with `id`, `name`, `definition`, `calculation`, optional `dimensions` and `access`. For example, `{"op":"ratio","activity":"payment_timed_out","denominator_activity":"payment_attempted"}` calculates timeout rate from observed events. Use `where` for equality filters on event attributes. Interactive request documentation is at [`/docs`](http://127.0.0.1:8000/docs).
- [`/mcp`](http://127.0.0.1:8000/mcp) is a Streamable HTTP MCP endpoint. Tools read metric definitions, observations and breakdowns, graph neighbours, and supporting events. The investigation uses the same observation tools.
- `/api/state` and `/api/incidents/{id}/evidence` supply the dashboard with persisted graph, anomalies, reports and evidence.

The baseline and live events use a relational OCEL 2.0 representation with events, objects and qualified links. The monitor compares closed windows against historical patterns. Metric values stay in PostgreSQL and graph edges carry their type and provenance.
