"""Materialize registered metrics and compare closed windows with history."""

from collections import defaultdict
from math import ceil, sqrt
from statistics import fmean, pstdev

from psycopg.types.json import Jsonb
from scipy.stats import binom

from eye.metric_catalog import CATALOG, INITIAL_METRICS, TIMEOUT_RATE_METRIC
from eye.metric_readings import Facts, MAX_MATURITY, metric_readings


def metric(id, name, definition, op, activity=None, dimensions=(), direction="down", **calculation):
    if activity:
        calculation["activity"] = activity
    return {
        "id": id, "name": name, "definition": definition,
        "calculation": {"op": op, **calculation}, "dimensions": list(dimensions),
        "access": {"source": "closed OCEL event windows"}, "direction": direction,
    }


def validate_calculation(formula):
    op = formula.get("op")
    if op == "catalog" and formula.get("metric") in CATALOG:
        return
    if op not in {"count", "sum", "ratio", "share", "lag", "completeness"}:
        raise ValueError(f"Unknown calculation operation {op!r}")
    if op not in {"lag", "completeness"} and not isinstance(formula.get("activity"), str):
        raise ValueError("An event calculation requires an activity name.")
    if op == "sum" and not isinstance(formula.get("value"), str):
        raise ValueError("A sum calculation requires the numeric event attribute in 'value'.")


def counts_by_scope(events, dimensions, filters, value=None):
    groups = {(): (0.0, 0)}
    for event in events:
        attrs = event["attrs"]
        if any(attrs.get(key) != expected for key, expected in filters.items()):
            continue
        amount = attrs.get(value) if value else 1
        if amount is None:
            continue
        fields = tuple((key, attrs[key]) for key in dimensions if attrs.get(key) is not None)
        scopes = [(), *((field,) for field in fields)]
        if len(fields) == len(dimensions) and len(fields) > 1:
            scopes.append(fields)
        for scope in scopes:
            total, size = groups.get(scope, (0.0, 0))
            groups[scope] = total + amount, size + 1
    return groups


def readings(definition, events, by_activity, expected_events):
    formula = definition["calculation"]
    dims = definition["dimensions"]
    op = formula.get("op")
    if op == "completeness":
        return [((), len(events) / expected_events, expected_events)] if expected_events else []
    if op == "lag":
        delays = sorted((row["ingested_at"] - row["occurred_at"]).total_seconds() for row in events)
        return [((), delays[ceil(0.95 * len(delays)) - 1], len(delays))] if delays else []
    activity = formula["activity"]
    numerator = counts_by_scope(
        by_activity.get(activity, []), dims, formula.get("where", {}),
        formula.get("value") if op == "sum" else None,
    )
    if op in {"count", "sum"}:
        return [(scope, amount, size) for scope, (amount, size) in numerator.items()]
    denominator = counts_by_scope(
        by_activity.get(formula.get("denominator_activity", activity), []),
        dims, formula.get("denominator_where", {}),
    )
    return [
        (scope, numerator.get(scope, (0.0, 0))[0] / count, size)
        for scope, (count, size) in denominator.items() if count
    ]


def materialize(db, window_start=None, metric_id=None):
    windows = db.execute(
        "select * from windows where start_at = %s order by start_at" if window_start else
        "select * from windows order by start_at",
        (window_start,) if window_start else (),
    ).fetchall()
    definitions = db.execute(
        "select * from metrics where id = %s" if metric_id else "select * from metrics",
        (metric_id,) if metric_id else (),
    ).fetchall()
    catalog = [definition for definition in definitions if definition["calculation"]["op"] == "catalog"]
    custom = [definition for definition in definitions if definition["calculation"]["op"] != "catalog"]
    known_scopes = defaultdict(list)
    if window_start and not metric_id:
        scopes = db.execute(
            """select distinct o.metric_id, o.scope from observations o
               join metrics m on m.id = o.metric_id
               where o.window_start < %s and m.calculation->>'op' in ('count', 'sum')""",
            (window_start,),
        ).fetchall()
        for row in scopes:
            known_scopes[row["metric_id"]].append(tuple(sorted(row["scope"].items())))
    for window in windows:
        start, end = window["start_at"], window["end_at"]
        values = []
        if catalog:
            facts = Facts(db, start, end, window["observation_snapshot"] or end + MAX_MATURITY)
            for definition in catalog:
                values.extend((definition["id"], start, Jsonb(dict(scope)), value, size, Jsonb(details))
                              for scope, value, size, details in metric_readings(definition, facts))
        if custom:
            events = db.execute(
                """select activity, attrs, occurred_at, ingested_at from events
                   where window_start = %s and ingested_at <= %s""",
                (start, window["observation_snapshot"] or end + MAX_MATURITY),
            ).fetchall()
            by_activity = defaultdict(list)
            for event in events:
                by_activity[event["activity"]].append(event)
            for definition in custom:
                calculated = {
                    tuple(sorted(scope)): (amount, size)
                    for scope, amount, size in readings(definition, events, by_activity, window["expected_events"])
                }
                for scope in known_scopes[definition["id"]]:
                    calculated.setdefault(scope, (0.0, 0))
                values.extend((definition["id"], start, Jsonb(dict(scope)), amount, size, Jsonb({}))
                              for scope, (amount, size) in calculated.items())
        if metric_id:
            db.execute("delete from observations where metric_id = %s and window_start = %s",
                       (metric_id, start))
        with db.cursor() as cursor:
            cursor.executemany(
                """insert into observations (metric_id, window_start, scope, value, sample_size, details)
                   values (%s, %s, %s, %s, %s, %s)
                   on conflict (metric_id, window_start, scope) do update
                   set value = excluded.value, sample_size = excluded.sample_size, details = excluded.details""",
                values,
            )


def detect(db, window_start, baseline_through):
    recent = db.execute(
        """select o.*, m.direction, m.access->'alert_dimensions' as alert_dimensions,
                  case when m.calculation->>'method' in ('lag', 'completeness') then m.calculation->>'method'
                       when m.calculation->>'op' = 'catalog' and m.access->>'unit' = 'ratio'
                       then 'ratio' else m.calculation->>'op' end as op
           from observations o join metrics m on m.id = o.metric_id
           where o.window_start = %s and o.value is not null order by o.metric_id""",
        (window_start,),
    ).fetchall()
    history = defaultdict(list)
    same_hour = defaultdict(list)
    reference = db.execute(
        """select o.metric_id, o.window_start, o.scope, o.value, o.sample_size from observations o
           join metrics m on m.id = o.metric_id and m.direction <> 'none'
           left join observations q on q.window_start = o.window_start
               and q.metric_id = 'data_completeness_ratio' and q.scope = '{}'
           where o.window_start <= %s and o.value is not null and coalesce(q.value, 1) >= 0.97""",
        (baseline_through,),
    ).fetchall()
    for row in reference:
        key = row["metric_id"], tuple(sorted(row["scope"].items()))
        history[key].append(row)
        if row["window_start"].hour == window_start.hour:
            same_hour[key].append(row)

    complete = next((row["value"] for row in recent
                     if row["metric_id"] == "data_completeness_ratio" and not row["scope"]), 1)
    anomalies = []
    for reading in recent:
        metric_id = reading["metric_id"]
        if reading["direction"] == "none" or (complete < 0.97 and metric_id != "data_completeness_ratio"):
            continue
        if reading["alert_dimensions"] is not None and not set(reading["scope"]).issubset(reading["alert_dimensions"]):
            continue
        key = metric_id, tuple(sorted(reading["scope"].items()))
        samples = same_hour[key] if len(same_hour[key]) >= 3 else history[key]
        if len(samples) < 3:
            continue
        value, size = reading["value"], reading["sample_size"]
        past = [row["value"] for row in samples]
        if reading["op"] in {"ratio", "share", "completeness"}:
            n = sum(row["sample_size"] for row in samples)
            if size < 30 or n < 90:
                continue
            successes = sum(row["value"] * row["sample_size"] for row in samples)
            expected = successes / n
            if reading["op"] != "completeness" and 0 <= expected <= 1:
                broad = history[key]
                broad_size = sum(row["sample_size"] for row in broad)
                broad_successes = sum(row["value"] * row["sample_size"] for row in broad)
                # Stabilize rare outcomes when an otherwise useful hourly sample has none.
                prior = (broad_successes + 0.5) / (broad_size + 1)
                expected = (successes + 30 * prior) / (n + 30) if samples is not broad else prior
            noise = max(pstdev(past), sqrt(max(0, expected * (1 - expected)) / size), 0.0125)
        elif reading["op"] == "lag":
            expected = fmean(past)
            noise = max(pstdev(past), 0.25, 0.045 * expected)
        else:
            expected = fmean(past)
            if expected < 5:
                continue
            noise = max(pstdev(past), sqrt(expected), 0.045 * expected)
        score = (value - expected) / noise
        if reading["direction"] == "up" and score <= 0:
            continue
        if reading["direction"] == "down" and score >= 0:
            continue
        if abs(score) < 4.0:
            continue
        if reading["op"] in {"ratio", "share"} and 0 <= value <= 1 and 0 <= expected <= 1:
            successes = round(value * size)
            tail = binom.cdf(successes, size, expected) if score < 0 else binom.sf(
                successes - 1, size, expected
            )
            if tail > 0.000032:
                continue
        anomalies.append(db.execute(
            """insert into anomalies (metric_id, window_start, scope, observed, expected, score, sample_size)
               values (%s, %s, %s, %s, %s, %s, %s)
               on conflict (metric_id, window_start, scope) do update
               set observed = excluded.observed, expected = excluded.expected,
                   score = excluded.score, sample_size = excluded.sample_size
               returning *""",
            (metric_id, window_start, Jsonb(reading["scope"]), value, expected, abs(score), size),
        ).fetchone())
    return sorted(anomalies, key=lambda row: -row["score"])
