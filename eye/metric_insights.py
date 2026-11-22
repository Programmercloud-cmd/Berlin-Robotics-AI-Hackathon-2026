"""Metric history and alert evidence for Eye's shared dashboard overlay."""

from base64 import b64encode
from datetime import datetime, timedelta, timezone
from io import BytesIO
from itertools import groupby
from math import nan
from threading import Lock

from psycopg.types.json import Jsonb

from eye import seasonality
from eye.db import state
from eye.intervals import CADENCE, HOUR, aligned_history
from eye.monitor import DETECTION_OP, SCORE_THRESHOLD, baseline_level, baseline_readings, reference_range, score_noise

PAGE = 6 * HOUR
plot_lock = Lock()


def saved_score_boundary(alert):
    reference = alert.get("detection_reference") or {}
    expected, cutoff, noise = (reference.get(key) for key in ("expected", "score_threshold", "noise"))
    if expected is None or cutoff is None or noise is None:
        return None
    direction = -1 if reference.get("observed", alert["observed"]) < expected else 1
    return expected + direction * cutoff * noise


def history(db, metric, scope, anchor, theme):
    last = state(db, "last_window")
    latest = datetime.fromisoformat(last) if last else None
    end = min(anchor, latest) if anchor and latest else anchor or latest
    if end is None:
        return {"metric": metric, "scope": scope, "points": [], "alerts": [], "scopes": [scope]}
    page = max(PAGE, timedelta(minutes=8 * metric["access"].get("cadence_minutes", 5)))
    start = end - page
    scope_json = Jsonb(scope)
    scopes = [row["scope"] for row in db.execute(
        """select distinct scope from observations where metric_id=%s and window_start >= %s
           and window_start <= %s and value is not null order by scope limit 100""", (metric["id"], start, end),
    ).fetchall()]
    for candidate in ({}, scope):
        if candidate not in scopes:
            scopes.insert(0, candidate)
    observed = db.execute(
        """select window_start,value,sample_size,details from observations
           where metric_id=%s and scope=%s and window_start between %s and %s order by window_start""",
        (metric["id"], scope_json, start, end),
    ).fetchall()
    alerts = db.execute(
        """select a.*, o.details as observation_details, q.value as window_completeness,
                  g.group_id as incident_id, e.payload->>'relationship' as relationship,
                  e.payload->'completion_checks' as checks,
                  case when g.recovered_at is not null then 'recovered'
                       when i.id is null or (p.incident_id is not null and i.report->>'deep_investigation'='pending')
                           then 'new' else 'investigating' end as status
           from anomalies a
           left join observations o on o.metric_id=a.metric_id and o.window_start=a.window_start and o.scope=a.scope
           left join observations q on q.metric_id='data_completeness_ratio' and q.window_start=a.window_start and q.scope='{}'
           left join lateral (select e.incident_id,e.payload from evidence e
               where e.window_start=a.window_start and e.payload->>'id'=a.id::text
               and e.kind in ('anomaly','related anomaly','ongoing anomaly')
               order by (e.kind='anomaly') desc,e.id limit 1) e on true
           left join incidents i on i.id=e.incident_id
           left join pending_reports p on p.incident_id=i.id
           left join incident_groups link on link.id=i.id
           left join incident_groups g on g.id=link.group_id
           where a.metric_id=%s and a.scope=%s and a.window_start between %s and %s order by a.window_start""",
        (metric["id"], scope_json, start, end),
    ).fetchall()
    seasonal = metric["id"] in (*seasonality.COUNTS, *seasonality.RATES) and state(db, "seasonal_history")
    baseline = state(db, "baseline_through")
    dates = {row["window_start"]: aligned_history(row["window_start"], datetime.fromisoformat(baseline))
             for row in observed} if baseline else {}
    historical = {row["window_start"]: row for row in baseline_readings(
        db, sorted({day for days in dates.values() for day in days}), metric["id"], scope)} if dates else {}
    planned = {row["window_start"]: row["reading"] for row in db.execute(
        "select * from planned_event_readings where scope=%s and window_start between %s and %s",
        (scope_json, start, end),
    ).fetchall()} if metric["id"] == "orders" else {}
    points = []
    for row in observed:
        tick = row["window_start"]
        reference = []
        if seasonal:
            weeks, segments = seasonality.weekly_history(db, tick)
            reference = seasonality.samples_for(weeks, segments, scope, metric["id"])
        method = "Same-weekday rolling windows" if len(reference) >= 3 else "Time-aligned historical windows"
        if len(reference) < 3:
            reference = [historical[day] for day in dates.get(tick, ()) if day in historical]
        normal, noise = None, None
        event = planned.get(tick)
        if len(reference) >= 3:
            value, _, size = baseline_level(reference, metric["op"])
            if size is None or size >= 90:
                normal = value
                if row["value"] is not None and value is not None and metric["direction"] != "none":
                    expected = event["adjusted"] if event else value
                    noise = score_noise(metric["op"], expected, row["sample_size"], size, reference)
        p10, p90 = reference_range(reference) if normal is not None else (None, None)
        window_end = row["details"].get("bucket_end")
        points.append({"window_start": tick, "window_end": datetime.fromisoformat(window_end) if window_end else tick + HOUR,
                       "value": row["value"], "sample_size": row["sample_size"], "quality": row["details"].get("quality"),
                       "normal": normal, "references": len(reference), "method": method if normal is not None else None,
                       "reference_windows": sorted(sample["window_start"] for sample in reference) if normal is not None else [],
                       "p10": p10, "p90": p90, "planned": event,
                       "live_lower": expected - SCORE_THRESHOLD * noise if noise is not None and metric["direction"] != "up" else None,
                       "live_upper": expected + SCORE_THRESHOLD * noise if noise is not None and metric["direction"] != "down" else None})
    for alert in alerts:
        alert["window_end"] = alert["window_end"] or alert["window_start"] + HOUR
        alert["score_boundary"] = saved_score_boundary(alert)
    plot = render(points, alerts, start + HOUR, end + HOUR, metric["access"].get("unit"), theme) if points or alerts else None
    return {"metric": metric, "scope": scope, "scopes": scopes, "points": points, "alerts": alerts, "plot": plot,
            "latest": latest, "anchor": end, "page_minutes": int(page.total_seconds() / 60)}


def render(points, alerts, start, end, unit, theme):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import PercentFormatter
    import seaborn as sns

    dark = theme == "dark"
    background, foreground, grid = ("#191816", "#eeeae4", "#403c37") if dark else ("#ffffff", "#241f1a", "#ded9d2")
    observed, reference, planned = ("#75bfff", "#aaa39a", "#f0a4db") if dark else ("#1769aa", "#716a62", "#762a66")
    threshold = "#ffa981" if dark else "#a85030"
    live_threshold = "#72cbb4" if dark else "#28796a"
    with plot_lock, sns.axes_style("whitegrid", {"grid.color": grid, "axes.facecolor": background}):
        figure = Figure(figsize=(10, 3.6), dpi=105, facecolor=background)
        canvas = FigureCanvasAgg(figure)
        ax = figure.add_axes((.10, .20, .86, .72))
        seconds = lambda time: (time - start).total_seconds()
        times = [seconds(point["window_end"]) for point in points]
        for field, color, width, linestyle in (("value", observed, 2, "-"), ("normal", reference, 1.4, "--"),
                                                ("adjusted", planned, 1.6, "-."), ("live_lower", live_threshold, 1.2, ":"),
                                                ("live_upper", live_threshold, 1.2, ":")):
            values = [point.get(field) if field != "adjusted" else (point["planned"] or {}).get(field) for point in points]
            for present, run in groupby(zip(times, values), key=lambda sample: sample[1] is not None):
                if present:
                    x, y = zip(*run)
                    sns.lineplot(x=x, y=y, estimator=None, errorbar=None, ax=ax, color=color, lw=width, ls=linestyle,
                                 marker="o" if len(x) == 1 else None)
        for low, high, color, event in (("p10", "p90", reference, False), ("lower", "upper", planned, True)):
            bounds = [point["planned"] or {} if event else point for point in points]
            ax.fill_between(times, [row.get(low) if row.get(low) is not None else nan for row in bounds],
                            [row.get(high) if row.get(high) is not None else nan for row in bounds], color=color, alpha=.15)
        for alert in alerts:
            if alert["score_boundary"] is not None:
                at = alert["window_end"]
                ax.hlines(alert["score_boundary"], seconds(at - CADENCE), seconds(at), colors=threshold,
                          linewidth=1.5, linestyles=":")
        ticks = [(end - start).total_seconds() * index / 6 for index in range(7)]
        clock_format = "%d %b %H:%M" if end - start >= timedelta(days=1) else "%H:%M"
        ax.set(xlim=(0, (end - start).total_seconds()), xticks=ticks,
               xticklabels=[(start + timedelta(seconds=tick)).astimezone(timezone.utc).strftime(clock_format) for tick in ticks],
               xlabel="Window end · UTC")
        if unit == "ratio":
            ax.yaxis.set_major_formatter(PercentFormatter(1))
        elif unit:
            ax.set_ylabel(unit)
        sns.despine(ax=ax)
        ax.tick_params(colors=foreground, labelsize=9)
        ax.xaxis.label.set_color(foreground)
        ax.yaxis.label.set_color(foreground)
        canvas.draw()
        width, height = canvas.get_width_height()
        for row, value_key in [*((point, "value") for point in points), *((alert, "observed") for alert in alerts)]:
            if row[value_key] is None:
                continue
            x, y = ax.transData.transform((seconds(row["window_end"]), row[value_key]))
            row.update(x=100 * x / width, y=100 * (1 - y / height))
        for alert in alerts:
            left = max(start, alert["window_end"] - CADENCE)
            x, _ = ax.transData.transform((seconds(left), 0))
            alert["span_left"] = 100 * x / width
        image = BytesIO()
        canvas.print_png(image)
        return b64encode(image.getvalue()).decode("ascii")
