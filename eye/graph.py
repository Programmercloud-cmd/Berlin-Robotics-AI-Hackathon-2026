from collections import defaultdict

from psycopg.types.json import Jsonb


def put_node(db, node_id, kind, label, properties):
    db.execute(
        """insert into graph_nodes (id, kind, label, properties) values (%s, %s, %s, %s)
        on conflict (id) do update set label = excluded.label, properties = excluded.properties""",
        (node_id, kind, label, Jsonb(properties)),
    )


def put_edge(db, source, target, kind, provenance, properties, confidence=None):
    db.execute(
        """insert into graph_edges (source, target, kind, provenance, properties, confidence)
        values (%s, %s, %s, %s, %s, %s)
        on conflict (source, target, kind, provenance) do update
        set properties = excluded.properties, confidence = excluded.confidence""",
        (source, target, kind, provenance, Jsonb(properties), confidence),
    )


def calculation_activities(calculation):
    return dict.fromkeys(
        value for key in ("activity", "denominator_activity")
        if isinstance(value := calculation.get(key), str) and value
    ) | dict.fromkeys(calculation.get("activities", []))


def attach_measures(db, metric_id, calculation, nodes):
    for activity in calculation_activities(calculation):
        target = f"activity:{activity}"
        if target in nodes:
            put_edge(db, f"metric:{metric_id}", target, "measures", "calculation",
                     {"activity": activity})


def discover(db, through=None, cutoff=None):
    import pandas as pd
    import pm4py
    from pm4py.objects.ocel.obj import OCEL

    # A rolling log keeps discovery responsive as the simulation continues.
    windows = db.execute(
        """select start_at, coalesce(observation_snapshot, end_at + interval '15 minutes') as as_of
           from windows where (%s::timestamptz is null or start_at <= %s)
           order by start_at desc limit 24""",
        (through, through),
    ).fetchall()
    if not windows:
        return
    starts = [row["start_at"] for row in windows]
    as_of = cutoff or windows[0]["as_of"]
    occurrence_limit = through if cutoff else as_of
    events = db.execute(
        """select id as "ocel:eid", activity as "ocel:activity", occurred_at as "ocel:timestamp"
        from events where window_start = any(%s) and ingested_at <= %s and occurred_at < %s
        order by occurred_at, id""",
        (starts, as_of, occurrence_limit),
    ).fetchall()
    if not events:
        return
    relations = db.execute(
        """select e.id as "ocel:eid", e.activity as "ocel:activity",
        e.occurred_at as "ocel:timestamp", o.id as "ocel:oid", o.type as "ocel:type",
        eo.qualifier as "ocel:qualifier"
        from events e join event_objects eo on eo.event_id = e.id
        join objects o on eo.object_id = o.id
        where e.window_start = any(%s) and e.ingested_at <= %s and e.occurred_at < %s
        order by e.occurred_at, e.id, o.id, eo.qualifier""",
        (starts, as_of, occurrence_limit),
    ).fetchall()
    if not relations:
        return

    event_frame = pd.DataFrame(events)
    relation_frame = pd.DataFrame(relations)
    object_frame = relation_frame[["ocel:oid", "ocel:type"]].drop_duplicates().reset_index(drop=True)
    # Keep qualified links in the OCEL log. Count an event once in each object's lifecycle.
    ocel = OCEL(event_frame, object_frame, relation_frame)
    if relation_frame.duplicated(["ocel:eid", "ocel:oid"]).any():
        ocel = OCEL(event_frame, object_frame,
                    relation_frame.drop_duplicates(["ocel:eid", "ocel:oid"]).reset_index(drop=True))
    model = pm4py.discover_ocdfg(ocel, compute_edges_performance=False)
    counts = model["activities_indep"]["events"]
    activities = {f"activity:{activity}" for activity in model["activities"]}
    for activity in sorted(model["activities"]):
        put_node(db, f"activity:{activity}", "activity", activity.replace("_", " ").capitalize(),
                 {"activity": activity, "events": len(counts.get(activity, ()))})

    observed = model["activities_ot"]["events"]
    starts_at = model["start_activities"]["events"]
    ends_at = model["end_activities"]["events"]
    for object_type in sorted(model["object_types"]):
        process = f"process:{object_type}"
        info = {
            "object_type": object_type,
            "start_activities": sorted(starts_at.get(object_type, {})),
            "end_activities": sorted(ends_at.get(object_type, {})),
        }
        if object_type == "payment":
            lifecycle = pm4py.ocel_flattening(ocel, object_type)
            info["inductive_tree"] = str(pm4py.discover_process_tree_inductive(lifecycle))
        put_node(db, process, "process", f"{object_type.replace('_', ' ').capitalize()} lifecycle", info)
        for activity, event_ids in sorted(observed.get(object_type, {}).items()):
            put_edge(db, f"activity:{activity}", process, "part_of", "pm4py.ocdfg",
                     {"events": len(event_ids)})

    transitions = defaultdict(dict)
    for object_type, edges in model["edges"]["event_couples"].items():
        for pair, event_pairs in edges.items():
            transitions[pair][object_type] = len(event_pairs)
    for (before, after), object_types in sorted(transitions.items()):
        put_edge(db, f"activity:{before}", f"activity:{after}", "precedes", "pm4py.ocdfg",
                 {"object_types": object_types})

    for metric in db.execute("select id, calculation from metrics").fetchall():
        attach_measures(db, metric["id"], metric["calculation"], activities)


def associate(db):
    """Add correlations for metrics that measure the same observed activity."""
    pairs = db.execute(
        """select a.metric_id as source, b.metric_id as target,
                  corr(a.value, b.value) as correlation, count(*) as windows
        from observations a join observations b using (window_start, scope)
        join windows w on w.start_at = a.window_start
        where a.metric_id < b.metric_id and a.scope = '{}'
        and exists (
            select 1 from graph_edges x join graph_edges y on x.target = y.target
            where x.kind = 'measures' and y.kind = 'measures'
              and x.source = 'metric:' || a.metric_id and y.source = 'metric:' || b.metric_id
        )
        group by a.metric_id, b.metric_id
        having count(*) >= 8 and abs(corr(a.value, b.value)) >= 0.85"""
    ).fetchall()
    db.execute("delete from graph_edges where kind = 'associated_with' and provenance = 'pearson'")
    for row in pairs:
        put_edge(db, f"metric:{row['source']}", f"metric:{row['target']}",
                 "associated_with", "pearson", {
                     "pearson_r": row["correlation"], "paired_windows": row["windows"],
                     "scope": {}, "symmetric": True,
                 })
