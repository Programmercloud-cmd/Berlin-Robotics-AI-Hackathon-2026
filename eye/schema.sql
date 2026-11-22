create schema if not exists eye;
set search_path to eye;

create table if not exists windows (
    start_at timestamptz primary key,
    end_at timestamptz not null,
    expected_events integer not null
);
alter table windows add column if not exists observation_snapshot timestamptz;
alter table windows add column if not exists published_through timestamptz;

create table if not exists events (
    id text primary key,
    activity text not null,
    occurred_at timestamptz not null,
    ingested_at timestamptz not null,
    window_start timestamptz not null references windows(start_at),
    attrs jsonb not null default '{}'
);
create index if not exists events_window_activity on events(window_start, activity);
create index if not exists events_occurrence on events(occurred_at);
create index if not exists inventory_occurrence on events(occurred_at) where activity = 'inventory_snapshot';

create table if not exists source_manifests (
    window_start timestamptz not null references windows(start_at),
    minute_at timestamptz not null,
    source_id text not null,
    event_type text not null,
    expected integer not null,
    primary key (minute_at, source_id, event_type)
);

create table if not exists objects (
    id text primary key,
    type text not null,
    attrs jsonb not null default '{}'
);

create table if not exists event_objects (
    event_id text not null references events(id),
    object_id text not null references objects(id),
    qualifier text not null,
    primary key (event_id, object_id, qualifier)
);

create table if not exists metrics (
    id text primary key,
    name text not null,
    definition text not null,
    calculation jsonb not null,
    dimensions text[] not null default '{}',
    access jsonb not null default '{}',
    direction text not null default 'both',
    registered_at timestamptz not null default now()
);

create table if not exists observations (
    metric_id text not null references metrics(id),
    window_start timestamptz not null,
    scope jsonb not null,
    value double precision not null,
    sample_size integer not null,
    primary key (metric_id, window_start, scope)
);
alter table observations alter column value drop not null;
alter table observations add column if not exists details jsonb not null default '{}';
-- Observation intervals can start between source batches.
alter table observations drop constraint if exists observations_window_start_fkey;
create index if not exists aggregate_observations_recent on observations(metric_id, window_start desc)
    where scope = '{}';
create index if not exists observations_by_window on observations(window_start);

create table if not exists graph_nodes (
    id text primary key,
    kind text not null,
    label text not null,
    properties jsonb not null default '{}'
);

create table if not exists graph_edges (
    source text not null references graph_nodes(id),
    target text not null references graph_nodes(id),
    kind text not null,
    provenance text not null,
    confidence double precision,
    properties jsonb not null default '{}',
    primary key (source, target, kind, provenance)
);
create index if not exists graph_edges_target on graph_edges(target);

create table if not exists anomalies (
    id bigint generated always as identity primary key,
    metric_id text not null references metrics(id),
    window_start timestamptz not null,
    scope jsonb not null,
    observed double precision not null,
    expected double precision not null,
    score double precision not null,
    sample_size integer not null,
    unique (metric_id, window_start, scope)
);
alter table anomalies add column if not exists detected_at timestamptz default clock_timestamp();
alter table anomalies add column if not exists window_end timestamptz;
alter table anomalies add column if not exists observed_at timestamptz;
alter table anomalies drop constraint if exists anomalies_window_start_fkey;

create table if not exists incidents (
    id bigint generated always as identity primary key,
    metric_id text not null references metrics(id),
    scope jsonb not null,
    opened_at timestamptz not null,
    updated_at timestamptz not null,
    recovered_at timestamptz,
    healthy_windows integer not null default 0,
    report jsonb not null default '{}'
);
create unique index if not exists incidents_open_scope on incidents(metric_id, scope)
    where recovered_at is null;

create table if not exists incident_relationships (
    incident_id bigint primary key references incidents(id),
    parent_id bigint not null references incidents(id),
    status text not null check (status in ('provisional', 'linked', 'independent')),
    observed_at timestamptz not null,
    decision jsonb not null,
    check (incident_id <> parent_id)
);
create index if not exists incident_relationships_parent on incident_relationships(parent_id);

create or replace view incident_groups as
select i.id,
       coalesce(r.parent_id, i.id) as group_id,
       coalesce(m.members, array[]::bigint[]) as linked_incident_ids,
       case when i.recovered_at is not null and coalesce(m.all_recovered, true)
            then greatest(i.recovered_at, m.last_recovered) end as recovered_at,
       case when i.recovered_at is not null and coalesce(m.all_recovered, true)
            then greatest((i.report->>'recovery_observed_at')::timestamptz, m.last_recovery_observed) end as recovery_observed_at
from incidents i
left join incident_relationships r on r.incident_id = i.id and r.status = 'linked'
left join lateral (
    select array_agg(c.id order by c.id) as members, bool_and(c.recovered_at is not null) as all_recovered,
           max(c.recovered_at) as last_recovered,
           max((c.report->>'recovery_observed_at')::timestamptz) as last_recovery_observed
    from incident_relationships k join incidents c on c.id = k.incident_id
    where k.parent_id = i.id and k.status = 'linked'
) m on true;

create table if not exists pending_reports (
    incident_id bigint primary key references incidents(id),
    context jsonb not null,
    fallback jsonb not null
);

create table if not exists report_snapshots (
    incident_id bigint not null references incidents(id),
    phase text not null,
    report jsonb not null,
    telemetry jsonb not null,
    observed_through timestamptz,
    written_at timestamptz not null default clock_timestamp(),
    primary key (incident_id, phase)
);

create table if not exists evidence (
    id bigint generated always as identity primary key,
    incident_id bigint not null references incidents(id),
    window_start timestamptz not null,
    kind text not null,
    node_id text references graph_nodes(id),
    payload jsonb not null
);
alter table evidence drop constraint if exists evidence_window_start_fkey;

create table if not exists runtime (
    key text primary key,
    value jsonb not null
);
