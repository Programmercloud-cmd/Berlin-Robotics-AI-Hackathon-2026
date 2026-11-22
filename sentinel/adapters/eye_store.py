"""Agent-only tables in the Eye database: schema `sentinel`.

Built on Eye's own table layouts (eye.metrics, eye.observations, eye.incidents, eye.evidence) plus the
agent's columns. Only the Sentinel agent reads and writes them: Eye's simulator and Eye's metric graph
never touch this schema, and the agent never writes to `eye.*`.

  sentinel.metrics       = eye.metrics      + provenance, kind, status        (agent-discovered metrics)
  sentinel.observations  = eye.observations                                    (values of those metrics)
  sentinel.incidents     = eye.incidents    + fingerprint, state, label, first_cause, segment,
                                              impact_eur_total, hypotheses, report_md, alerted
  sentinel.evidence      = eye.evidence     + evidence_key, step, query, interpretation
  sentinel.known_patterns  patterns investigations confirmed as normal (baseline corrections, v2)
  sentinel.bus_events      every event published on the agent's bus (audit trail / replay)
"""
from __future__ import annotations

import json

from psycopg.types.json import Jsonb

SCHEMA = """
create schema if not exists sentinel;

create table if not exists sentinel.metrics (
    id text primary key,
    name text not null,
    definition text not null,
    calculation jsonb not null,
    dimensions text[] not null default '{}',
    access jsonb not null default '{}',
    direction text not null default 'both',
    registered_at timestamptz not null default now(),
    provenance text not null,                 -- e.g. meta.process_mining, pm4py.conformance
    kind text not null,                       -- count | ratio | gauge
    status text not null default 'proposed'   -- proposed | validated | rejected
);

create table if not exists sentinel.observations (
    metric_id text not null references sentinel.metrics(id),
    window_start timestamptz not null,        -- same windows as eye.windows
    scope jsonb not null,
    value double precision,
    sample_size integer not null,
    details jsonb not null default '{}',
    primary key (metric_id, window_start, scope)
);

create table if not exists sentinel.incidents (
    id text primary key,                      -- INC-001 ...
    metric_id text,                           -- eye.incidents.metric_id: the alerting metric
    scope jsonb not null default '{}',
    opened_at timestamptz not null,
    updated_at timestamptz not null,
    recovered_at timestamptz,
    healthy_windows integer not null default 0,
    report jsonb not null default '{}',
    fingerprint text,
    state text not null,
    label text,
    first_cause text,
    segment text not null default '',
    impact_eur_total double precision not null default 0,
    hypotheses jsonb not null default '[]',
    report_md text not null default '',
    alerted boolean not null default false
);

create table if not exists sentinel.evidence (
    id bigint generated always as identity primary key,
    incident_id text not null references sentinel.incidents(id),
    window_start timestamptz,
    kind text not null,
    node_id text,
    payload jsonb not null,
    evidence_key text,                        -- E1, E2 ...
    step text,
    query text,
    interpretation text,
    unique (incident_id, evidence_key)
);

create table if not exists sentinel.known_patterns (
    id bigint generated always as identity primary key,
    metric_id text not null,
    scope jsonb not null,
    factor double precision not null,          -- expected value is multiplied by this while active
    valid_from timestamptz not null,
    valid_until timestamptz not null,
    reason text not null,
    incident_id text
);

create table if not exists sentinel.bus_events (
    seq bigint primary key,
    type text not null,
    source text not null,
    sim_ts timestamptz,
    incident_id text,
    payload jsonb not null
);
"""


class AgentStore:
    def __init__(self, db):
        self.db = db
        self.db.execute(SCHEMA)

    # ------------------------------------------------------------------ agent metrics
    def register_metric(self, metric: dict):
        self.db.execute(
            """insert into sentinel.metrics (id, name, definition, calculation, dimensions, access, direction,
                                              provenance, kind, status)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (id) do update set name = excluded.name, definition = excluded.definition,
                 calculation = excluded.calculation, dimensions = excluded.dimensions, access = excluded.access,
                 direction = excluded.direction, provenance = excluded.provenance, kind = excluded.kind,
                 status = excluded.status""",
            (metric["id"], metric["name"], metric["definition"], Jsonb(metric["calculation"]),
             list(metric["dimensions"]), Jsonb(metric.get("access", {})), metric.get("direction", "both"),
             metric["provenance"], metric["kind"], metric.get("status", "proposed")))

    def set_status(self, metric_id: str, status: str):
        self.db.execute("update sentinel.metrics set status = %s where id = %s", (status, metric_id))

    def metrics(self) -> list[dict]:
        return self.db.execute("select * from sentinel.metrics order by id").fetchall()

    def write_observations(self, rows: list[tuple]):
        """rows: (metric_id, window_start, scope dict, value, sample_size, details dict)"""
        if not rows:
            return
        with self.db.cursor() as cur:
            cur.executemany(
                """insert into sentinel.observations (metric_id, window_start, scope, value, sample_size, details)
                   values (%s, %s, %s, %s, %s, %s)
                   on conflict (metric_id, window_start, scope) do update set value = excluded.value,
                     sample_size = excluded.sample_size, details = excluded.details""",
                [(m, w, Jsonb(s), v, n, Jsonb(d)) for m, w, s, v, n, d in rows])

    # ------------------------------------------------------------------ incidents, evidence, bus
    def attach(self, bus, incidents_manager):
        """Persist every bus event, and keep incidents / evidence in sync with the incident manager."""
        self.im = incidents_manager

        def on_event(ev):
            self.db.execute(
                """insert into sentinel.bus_events (seq, type, source, sim_ts, incident_id, payload)
                   values (%s, %s, %s, %s, %s, %s) on conflict (seq) do nothing""",
                (ev.seq, ev.type, ev.source, ev.sim_ts, ev.incident_id, Jsonb(json.loads(json.dumps(ev.payload, default=str)))))
            if ev.type == "BASELINE_LEARNED":
                p = ev.payload
                self.db.execute(
                    """insert into sentinel.known_patterns (metric_id, scope, factor, valid_from, valid_until, reason,
                                                            incident_id) values (%s, %s, %s, %s, %s, %s, %s)""",
                    (p["metric"], Jsonb(p["segment"]), p["factor"], p["from"], p["until"], p["reason"],
                     p.get("incident")))
            if ev.incident_id and ev.type in ("INCIDENT_OPENED", "INCIDENT_UPDATED", "INCIDENT_RESOLVED",
                                              "FINAL_REPORT", "OBSERVATION"):
                self.sync_incident(ev.incident_id, ev)
        bus.subscribe(on_event)

    def sync_incident(self, incident_id: str, ev=None):
        inc = self.im.incidents.get(incident_id) if self.im else None
        if inc is None:
            return
        first = inc.anomalies[0] if inc.anomalies else None
        self.db.execute(
            """insert into sentinel.incidents (id, metric_id, scope, opened_at, updated_at, recovered_at, report,
                 fingerprint, state, label, first_cause, segment, impact_eur_total, hypotheses, report_md, alerted)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (id) do update set updated_at = excluded.updated_at, recovered_at = excluded.recovered_at,
                 report = excluded.report, fingerprint = excluded.fingerprint, state = excluded.state,
                 label = excluded.label, first_cause = excluded.first_cause, segment = excluded.segment,
                 impact_eur_total = excluded.impact_eur_total, hypotheses = excluded.hypotheses,
                 report_md = excluded.report_md, alerted = excluded.alerted""",
            (inc.id, first.metric if first else None,
             Jsonb(dict(p.split("=", 1) for p in inc.segment.split("|")) if inc.segment else {}),
             inc.opened_at, inc.updated_at, inc.updated_at if inc.state == "resolved" else None,
             Jsonb({"summary": (inc.report_md.splitlines() or [""])[0], "reasoning": "sentinel"}),
             inc.fingerprint, inc.state, inc.label, inc.first_cause, inc.segment, inc.impact_eur_total,
             Jsonb([json.loads(h.model_dump_json()) for h in inc.hypotheses]), inc.report_md,
             incident_id in self.im.alerted))
        if ev is not None and ev.type == "OBSERVATION":
            p = ev.payload
            self.db.execute(
                """insert into sentinel.evidence (incident_id, window_start, kind, node_id, payload, evidence_key,
                                                   step, query, interpretation)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict (incident_id, evidence_key) do nothing""",
                (incident_id, ev.sim_ts, p.get("stage", "observation"), f"metric:{p.get('focus')}",
                 Jsonb(json.loads(json.dumps(p.get("numbers", {}), default=str))), p.get("evidence_id"),
                 p.get("stage"), p.get("query", ""), p.get("text", "")))
