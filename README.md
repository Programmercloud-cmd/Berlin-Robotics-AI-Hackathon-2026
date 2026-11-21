# Eye of the System

Autonomous marketplace monitoring for Almedia’s Fully Agentic Monitoring track at the Berlin Robotics & AI Hackathon 2026.

## Run the demo

Python 3.12 is the verified runtime.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-local.txt
python -m eye demo
```

Open [the dashboard](http://127.0.0.1:8000). This command provisions a temporary PostgreSQL database, generates a seeded historical baseline and advances a virtual hour at each demo step. The dashboard stays available after the scenario to inspect the recovered incident. Use `--delay 3` for a slower walkthrough or `python -m eye demo --check` to run the same scenario and exit with a verification result.

On systems where Python's `ensurepip` is unavailable, create the environment with `python3 -m venv --without-pip .venv` and install with `python3 -m pip --python .venv/bin/python install -r requirements-local.txt`.

1. PM4Py discovers shopping, payment and inventory lifecycles from qualified object-event relations. The dashboard shows the resulting OC-DFG and linked metrics.
2. Normal operation and a jump in traffic produce healthy windows.
3. The monitor opens an investigation from an observed payment-success anomaly. Inspect the affected segment, graph path, supporting events and estimated impact.
4. The demo registers `payment_timeout_rate` after detection. The curator receives a definition, calculation and dimensions. The graph extends automatically, and the observation history is backfilled.
5. Observe the incident close after two healthy windows.

Run `python -m eye demo --with-search` to continue into a second incident. The simulator emits search requests and clicks, the graph discovers a search lifecycle, and the demo registers request volume, zero-result searches and click rate. The monitor detects a segmented search disruption and follows it through recovery. Add `--check` to verify both cases without opening a server.

## Live reasoning

Run `EYE_MODEL=codex python -m eye demo` to use an authenticated Codex CLI on your PATH. To use an OpenAI-compatible JSON chat completions provider, copy [the environment example](.env.example) to `.env` and set `MODEL_NAME`, `MODEL_API_KEY` and, if applicable, `MODEL_BASE_URL`. With no reachable provider, graph curation uses the registered calculations and the investigator produces a statistical report. The report records which reasoning path ran.

### Devin API v3

Set `DEVIN_API_KEY` and `DEVIN_ORG_ID` in the repo-root `.env`. Use one organization service user with permission to create Devin sessions. Eye creates a separate on-demand session for each investigation or curation request and submits only that role’s context and JSON schema. Eye gathers evidence, validates the results, applies graph changes and tracks recovery locally. [Devin service user authentication](https://docs.devin.ai/api-reference/authentication)

Start the interactive demo and leave it running.

```sh
EYE_MODEL=devin DEVIN_MODE=lite python -m eye demo
```

Open [the dashboard](http://127.0.0.1:8000) once the server prints its URL. Eye generates the historical baseline before the server starts. Each Devin session can take a few minutes to start and respond. If Devin is unavailable or its output fails validation, Eye continues with its existing fallback.

For terminal-only verification, add `--check`. The check requires the recovered payment report and an accepted `payment_timeout_rate` graph relationship to carry provenance from distinct Devin sessions. It exits after verification.

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
