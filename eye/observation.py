"""Read-only MCP tools for marketplace observations."""

import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone
from statistics import fmean, quantiles
from typing import Any

from mcp.server.fastmcp import FastMCP
from psycopg.types.json import Jsonb

from eye.db import connect, state
from eye.intervals import CADENCE, DAY, GRACE, HOUR, aligned_history
from eye.monitor import DETECTION_OP, baseline_level, baseline_readings


def rows(db, query, params):
    return db.execute(query, params).fetchall()


def make_mcp(dsn=None, *, db=None):
    server = FastMCP("Eye of the System", stateless_http=True, json_response=True)

    def read(query, params, *, single=False):
        if db is not None:
            result = rows(db, query, params)
        else:
            with connect(dsn) as connection:
                result = rows(connection, query, params)
        return (result[0] if result else None) if single else result

    @server.tool()
    def metric_definition(metric_id: str) -> dict[str, Any]:
        """Retrieve the definition, calculation, dimensions, and source of a registered metric."""
        return {"data": read("select * from metrics where id = %s", (metric_id,), single=True)}

    @server.tool()
    def metric_readings(metric_id: str, start: datetime, end: datetime,
                        scope: dict | None = None, aligned_to: datetime | None = None,
                        as_of: datetime | None = None, limit: int = 500) -> dict[str, Any]:
        """Read observations in the half-open UTC time interval, optionally at a specified time of day."""
        return {"data": read(
            """select window_start, scope, value, sample_size, details from observations
               where metric_id = %s and window_start >= %s and window_start < %s and scope = %s
               and (%s::timestamptz is null or window_start::time = %s::timestamptz::time)
               and (%s::timestamptz is null or
                    coalesce((details->>'observation_snapshot')::timestamptz,
                             window_start + interval '1 hour 15 minutes') <= %s)
               order by window_start desc limit %s""",
            (metric_id, start, end, Jsonb(scope or {}), aligned_to, aligned_to, as_of, as_of, max(1, min(limit, 500))),
        )[::-1]}

    @server.tool()
    def alert_chart(metric_id: str, window_start: datetime, scope: dict | None = None) -> dict[str, Any]:
        """Read two hours of rolling observations and their time-aligned baseline for an alert's scope."""
        start = window_start.astimezone(timezone.utc) if window_start.tzinfo else window_start.replace(tzinfo=timezone.utc)
        starts = [start - tick * CADENCE for tick in range(2 * HOUR // CADENCE, -1, -1)]
        with (nullcontext(db) if db is not None else connect(dsn)) as connection:
            metric = connection.execute(f"select {DETECTION_OP} as op from metrics m where m.id = %s", (metric_id,)).fetchone()
            if metric is None:
                return {"data": []}
            observed = {row["window_start"]: row for row in rows(connection,
                """select window_start, value, sample_size from observations
                   where metric_id = %s and scope = %s and window_start = any(%s)""",
                (metric_id, Jsonb(scope or {}), starts))}
            baseline = state(connection, "baseline_through")
            matching = {tick: aligned_history(tick, datetime.fromisoformat(baseline)) for tick in starts} if baseline else {}
            history = {row["window_start"]: row for row in baseline_readings(connection,
                sorted({day for days in matching.values() for day in days}), metric_id, scope)} if matching else {}

        points = []
        for tick in starts:
            reading = observed.get(tick, {})
            samples = [history[day] for day in matching.get(tick, ()) if day in history]
            expected = None
            if len(samples) >= 3:
                value, _, size = baseline_level(samples, metric["op"])
                if size is None or size >= 90:
                    expected = value
            # Four qualified days are the detector's full reference for a tick.
            band = quantiles([row["value"] for row in samples], n=10, method="inclusive") if expected is not None and len(samples) >= 4 else []
            points.append({"window_end": tick + HOUR, "value": reading.get("value"),
                           "sample_size": reading.get("sample_size"), "expected": expected, "references": len(samples),
                           "p10": band[0] if band else None, "p90": band[-1] if band else None})
        return {"data": points}

    @server.tool()
    def metric_breakdown(metric_id: str, window_start: datetime, as_of: datetime | None = None,
                         scope: dict | None = None, limit: int = 250) -> dict[str, Any]:
        """Read available dimensional scopes of a metric in a closed observation window."""
        return {"data": read(
            """select scope, value, sample_size, details from observations
               where metric_id = %s and window_start = %s and scope @> %s
                 and (%s::timestamptz is null or
                      coalesce((details->>'observation_snapshot')::timestamptz,
                               window_start + interval '1 hour 15 minutes') <= %s)
               order by sample_size desc, scope limit %s""",
            (metric_id, window_start, Jsonb(scope or {}), as_of, as_of, max(1, min(limit, 250))),
        )}

    @server.tool()
    def graph_neighbors(node_id: str) -> dict[str, Any]:
        """Read the node and its incoming and outgoing typed, directed relationships."""
        node = read("select * from graph_nodes where id = %s", (node_id,), single=True)
        edges = read(
            """select * from graph_edges where source = %s or target = %s
               order by kind, source, target, provenance""",
            (node_id, node_id),
        )
        return {"data": {"node": node, "edges": edges}}

    @server.tool()
    def supporting_events(
        window_start: datetime, scope: dict | None = None, activities: list[str] | None = None,
        limit: int = 20, as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Read sample observed events and their qualified business object relationships."""
        start, end, cutoff = observation_bounds(window_start, as_of)
        return {"data": read(
            """select e.id, e.activity, e.occurred_at, e.ingested_at, e.attrs,
                      (select jsonb_agg(jsonb_build_object(
                          'id', o.id, 'type', o.type, 'qualifier', r.qualifier))
                       from event_objects r join objects o on o.id = r.object_id
                       where r.event_id = e.id) as objects
               from events e
               where e.occurred_at >= %s and e.occurred_at < %s and e.attrs @> %s
                 and e.ingested_at <= %s
                 and (%s::text[] is null or e.activity = any(%s))
               order by e.occurred_at, e.id limit %s""",
            (start, end, Jsonb(scope or {}), cutoff, activities, activities, max(1, min(limit, 100))),
        )}

    @server.tool()
    def activity_mix(window_start: datetime, activities: list[str], scope: dict | None = None,
                     as_of: datetime | None = None) -> dict[str, Any]:
        """Compare scoped event counts with covered historical windows at the same UTC time and ingestion maturity."""
        start, end, cutoff = observation_bounds(window_start, as_of)
        baseline = read("select value from runtime where key = 'baseline_through'", (), single=True)
        last_day = min(start - DAY, datetime.fromisoformat(baseline["value"])) if baseline else start - DAY
        days = aligned_history(start, last_day)
        activities = list(dict.fromkeys(activities))
        window = end - start
        maturity = cutoff - start

        def count(first, through):
            rows = read(
                """select activity, count(*) as n from events
                   where occurred_at >= %s and occurred_at < %s and ingested_at <= %s
                     and activity = any(%s) and attrs @> %s group by activity""",
                (first, first + window, through, activities, Jsonb(scope or {})),
            )
            return {row["activity"]: row["n"] for row in rows}

        current = count(start, cutoff)
        history = []
        for date in days:
            covered = read(
                """select coalesce(sum(least(end_at, %s) - greatest(start_at, %s)), interval '0') as duration
                   from windows where start_at < %s and end_at > %s""",
                (date + window, date, date + window, date), single=True,
            )
            if covered["duration"] >= window:
                history.append(count(date, date + maturity))
        comparisons = []
        for activity in activities:
            values = [row.get(activity, 0) for row in history]
            comparisons.append({"activity": activity, "observed": current.get(activity, 0),
                                "expected": round(fmean(values), 2) if values else None,
                                "historical_windows": len(values), "historical_counts": values})
        return {"data": {"window_start": start, "window_end": end, "observed_through": cutoff,
                         "ingestion_maturity_minutes": maturity.total_seconds() / 60 - window.total_seconds() / 60,
                         "scope": scope or {}, "historical_windows": len(history), "activities": comparisons}}

    @server.tool()
    def company_context(window_start: datetime, as_of: datetime | None = None) -> dict[str, Any]:
        """Read observed activity counts and the ingestion manifest for a window."""
        start, end, cutoff = observation_bounds(window_start, as_of)
        window = read(
            """select %s::timestamptz as start_at, %s::timestamptz as end_at,
                      %s::timestamptz as observation_snapshot,
                      coalesce((select sum(expected) from source_manifests where minute_at >= %s and minute_at < %s),
                               (select expected_events from windows where start_at = %s and end_at <= %s)) as expected_events""",
            (start, end, cutoff, start, min(end, cutoff), start, cutoff), single=True,
        )
        activities = read(
            """select activity, count(*) as events from events
               where occurred_at >= %s and occurred_at < %s and ingested_at <= %s
               group by activity order by activity""", (start, end, cutoff),
        )
        return {"data": {"window": window, "activities": activities}}

    def observation_bounds(start, as_of):
        row = read(
            """select coalesce((select end_at from windows where start_at = %s),
                               %s::timestamptz + interval '1 hour') as end_at,
                      max(coalesce(published_through, observation_snapshot, end_at + interval '15 minutes')) as published,
                      max(observation_snapshot) filter (where start_at = %s) as source_snapshot
               from windows""", (start, start, start), single=True,
        )
        cutoff = as_of or row["source_snapshot"] or row["end_at"] + GRACE
        return start, row["end_at"], min(cutoff, row["published"]) if row["published"] else cutoff

    return server


def observation_tool(db):
    """Invoke the registered tools against the transaction processing this window."""
    server = make_mcp(db=db)

    def call(name, **arguments):
        _, structured = asyncio.run(server.call_tool(name, arguments))
        return structured["data"]

    return call
