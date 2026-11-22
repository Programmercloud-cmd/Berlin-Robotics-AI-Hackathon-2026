"""Learn order expectations from observed history and a public event calendar."""

from collections import defaultdict
from math import sqrt
from statistics import fmean, pstdev

from psycopg.types.json import Jsonb

from eye.intervals import CADENCE, DAY, GRACE, HOUR
from eye.metric_catalog import CATALOG
from eye.metric_readings import Facts, metric_readings

COUNTS = ("sessions", "product_view_sessions", "add_to_cart_sessions", "checkout_sessions", "orders")
RATES = ("session_conversion_rate", "checkout_completion_rate")


def contains(scope, segment):
    return all(segment.get(key) == value for key, value in scope.items())


def segment(row):
    return {key: row[key] for key in ("country", "platform")}


def pool(rows):
    counts = {metric: sum(row["counts"][metric] for row in rows) for metric in COUNTS}
    rates = {metric: [sum(row["rates"][metric][index] for row in rows) for index in range(2)] for metric in RATES}
    return counts, rates


def record_bucket(db, end, segments):
    start = end - CADENCE
    facts = Facts(db, start, end, end + GRACE)
    values = {identifier: dict((scope, (value, details)) for scope, value, _, details in metric_readings(CATALOG[identifier], facts))
              for identifier in (*COUNTS, *RATES)}
    complete = values["orders"][()][1]["quality"] == "complete"
    with db.cursor() as cursor:
        for country, platform in segments:
            key = (("country", country), ("platform", platform))
            counts = {metric: values[metric].get(key, (0, {}))[0] or 0 for metric in COUNTS}
            rates = {}
            for metric in RATES:
                details = values[metric].get(key, (None, {}))[1]
                rates[metric] = [details.get(field) or 0 for field in ("numerator", "denominator")]
            cursor.execute("""insert into seasonal_buckets values (5,%s,%s,%s,%s,%s,%s)
                              on conflict (minutes,start_at,country,platform) do update
                              set counts=excluded.counts,rates=excluded.rates,complete=excluded.complete""",
                           (start, country, platform, Jsonb(counts), Jsonb(rates), complete))
    if end.hour == end.minute == 0:
        daily = db.execute("select * from seasonal_buckets where minutes=5 and start_at >= %s and start_at < %s",
                           (end - DAY, end)).fetchall()
        for country, platform in segments:
            rows = [row for row in daily if (row["country"], row["platform"]) == (country, platform)]
            counts, rates = pool(rows)
            db.execute("""insert into seasonal_buckets values (1440,%s,%s,%s,%s,%s,%s)
                          on conflict (minutes,start_at,country,platform) do update
                          set counts=excluded.counts,rates=excluded.rates,complete=excluded.complete""",
                       (end - DAY, country, platform, Jsonb(counts), Jsonb(rates), len(rows) == 288 and all(r["complete"] for r in rows)))
        db.execute("delete from seasonal_buckets where minutes=5 and start_at < %s", (end - 56 * DAY,))
        db.execute("delete from seasonal_buckets where minutes=1440 and start_at < %s", (end - 365 * DAY,))


def weekly_history(db, start):
    dates = [start - days * DAY for days in range(7, 57, 7)]
    by_window = defaultdict(lambda: defaultdict(list))
    rows = db.execute("""select t.start as reference, b.* from unnest(%s::timestamptz[]) t(start)
                        join seasonal_buckets b on b.minutes=5 and b.start_at >= t.start and b.start_at < t.start + interval '1 hour'
                        where not exists (select 1 from planned_events p where p.starts_at < t.start + interval '1 hour'
                            and p.ends_at > t.start and (p.scope->>'country' is null or p.scope->>'country'=b.country)
                            and (p.scope->>'platform' is null or p.scope->>'platform'=b.platform))""", (dates,)).fetchall()
    for row in rows:
        by_window[row["reference"]][row["country"], row["platform"]].append(row)
    segments = {key for window in by_window.values() for key in window}
    return [{key: rows for key, rows in window.items() if len(rows) == 12 and all(r["complete"] for r in rows)}
            for window in by_window.values()], segments


def samples_for(windows, segments, scope, metric):
    if metric not in (*COUNTS, *RATES):
        return []
    keys = {key for key in segments if contains(scope, dict(zip(("country", "platform"), key)))}
    if not keys:
        return []
    samples = []
    for window in windows:
        if not keys <= window.keys():
            continue
        rows = [row for key in keys for row in window[key]]
        start = rows[0]["reference"]
        if metric in COUNTS:
            value = sum(row["counts"][metric] for row in rows)
            samples.append({"window_start": start, "value": value, "sample_size": value, "details": {}})
        elif metric in RATES:
            numerator, denominator = [sum(row["rates"][metric][index] for row in rows) for index in range(2)]
            if denominator:
                samples.append({"window_start": start, "value": numerator / denominator, "sample_size": denominator,
                                "details": {"numerator": numerator, "denominator": denominator}})
    return samples


def trend_at_event(controls, event_date):
    x = [(row["start_at"] - event_date).days / 7 for row in controls]
    y = [row["counts"]["orders"] for row in controls]
    center, level = fmean(x), fmean(y)
    slope = sum((at - center) * (value - level) for at, value in zip(x, y)) / sum((at - center) ** 2 for at in x)
    expected = level - center * slope
    if expected <= 0:
        return None
    residual = pstdev([value - (expected + slope * at) for at, value in zip(x, y)])
    return expected, slope, residual


def learn_event(db, event):
    prior = db.execute("""select * from planned_events where series=%s and starts_at < %s
                         order by starts_at desc limit 1""", (event["series"], event["starts_at"])).fetchone()
    if not prior:
        return {}
    date = prior["starts_at"]
    rows = db.execute("""select b.* from seasonal_buckets b where b.minutes=1440 and b.complete
                         and b.start_at between %s and %s and not exists (
                           select 1 from planned_events p where (p.series,p.starts_at) <> (%s,%s) and p.starts_at < b.start_at + interval '1 day'
                           and p.ends_at > b.start_at and (p.scope->>'country' is null or p.scope->>'country'=b.country)
                           and (p.scope->>'platform' is null or p.scope->>'platform'=b.platform)) order by b.start_at""",
                      (date - 49 * DAY, date + 49 * DAY, prior["series"], date)).fetchall()
    observed, controls = {}, defaultdict(list)
    for row in rows:
        key = row["country"], row["platform"]
        if not contains(event["scope"], segment(row)) or not contains(prior["scope"], segment(row)):
            continue
        if date <= row["start_at"] < prior["ends_at"]:
            observed[key] = row["counts"]["orders"]
        elif (row["start_at"] - date).days % 7 == 0:
            controls[key].append(row)
    result = {}
    for key, sales in observed.items():
        reference = trend_at_event(controls[key], date) if len(controls[key]) >= 4 else None
        if reference:
            normal, trend, residual = reference
            result[key] = {"occurrence": date.isoformat(), "observed": sales, "expected": normal, "factor": sales / normal,
                           "control_days": len(controls[key]), "weekly_trend": trend,
                           "margin": max(.10, 2 * residual / normal)}
    return result


def active_event(db, start):
    return db.execute("""select * from planned_events where starts_at < %s and ends_at > %s
                         order by starts_at limit 1""", (start + HOUR, start)).fetchone()


def comparison(windows, scope, normal, event, lifts, start, observed):
    extra, margin, prior = 0, 0, []
    for (country, platform), reference in lifts.items():
        if not contains(scope, {"country": country, "platform": platform}):
            continue
        samples = [sum(row["counts"]["orders"] for row in by_scope[country, platform]
                       if event["starts_at"] <= start + (row["start_at"] - row["reference"]) < event["ends_at"])
                   for by_scope in windows if (country, platform) in by_scope]
        if len(samples) < 3:
            continue
        volume = fmean(samples)
        extra += volume * (reference["factor"] - 1)
        margin += volume * reference["factor"] * reference["margin"]
        prior.append({"scope": {"country": country, "platform": platform}, **reference})
    if not prior:
        return None
    adjusted = normal + extra
    band = sqrt(margin ** 2 + 4 * max(1, adjusted))
    return {"event": event["name"], "scope": scope, "window_start": start.isoformat(), "observed": observed,
            "normal": normal, "adjusted": adjusted, "lower": max(0, adjusted - band), "upper": adjusted + band,
            "occurrences": 1, "history": prior, "status": "below expectation" if observed < adjusted - band else
            "above expectation" if observed > adjusted + band else "within expectation"}


def readings_at(db, start, scope):
    row = db.execute("""select reading from planned_event_readings
                       where window_start=%s and scope in (%s,'{}') order by (scope=%s) desc limit 1""",
                     (start, Jsonb(scope), Jsonb(scope))).fetchone()
    return row["reading"] if row else None
