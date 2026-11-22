"""Read-only MCP tools for marketplace observations."""

import asyncio
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from psycopg.types.json import Jsonb

from eye.db import connect


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
                        scope: dict | None = None) -> dict[str, Any]:
        """Read observations in the half-open UTC time interval. Empty scope selects the total."""
        return {"data": read(
            """select window_start, scope, value, sample_size, details from observations
               where metric_id = %s and window_start >= %s and window_start < %s and scope = %s
               order by window_start desc limit 500""",
            (metric_id, start, end, Jsonb(scope or {})),
        )[::-1]}

    @server.tool()
    def metric_breakdown(metric_id: str, window_start: datetime) -> dict[str, Any]:
        """Read available dimensional scopes of a metric in a closed observation window."""
        return {"data": read(
            """select scope, value, sample_size, details from observations
               where metric_id = %s and window_start = %s order by sample_size desc limit 250""",
            (metric_id, window_start),
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
        limit: int = 20,
    ) -> dict[str, Any]:
        """Read sample observed events and their qualified business object relationships."""
        return {"data": read(
            """select e.id, e.activity, e.occurred_at, e.ingested_at, e.attrs,
                      (select jsonb_agg(jsonb_build_object(
                          'id', o.id, 'type', o.type, 'qualifier', r.qualifier))
                       from event_objects r join objects o on o.id = r.object_id
                       where r.event_id = e.id) as objects
               from events e join windows w on w.start_at = e.window_start
               where e.window_start = %s and e.attrs @> %s
                 and e.ingested_at <= coalesce(w.observation_snapshot, w.end_at + interval '15 minutes')
                 and (%s::text[] is null or e.activity = any(%s))
               order by e.occurred_at, e.id limit %s""",
            (window_start, Jsonb(scope or {}), activities, activities, max(1, min(limit, 100))),
        )}

    @server.tool()
    def company_context(window_start: datetime) -> dict[str, Any]:
        """Read observed activity counts and the ingestion manifest for a window."""
        window = read("select * from windows where start_at = %s", (window_start,), single=True)
        activities = read(
            """select activity, count(*) as events from events e join windows w on w.start_at = e.window_start
               where e.window_start = %s
                 and e.ingested_at <= coalesce(w.observation_snapshot, w.end_at + interval '15 minutes')
               group by activity order by activity""",
            (window_start,),
        )
        return {"data": {"window": window, "activities": activities}}

    return server


def observation_tool(db):
    """Invoke the registered tools against the transaction processing this window."""
    server = make_mcp(db=db)

    def call(name, **arguments):
        _, structured = asyncio.run(server.call_tool(name, arguments))
        return structured["data"]

    return call
