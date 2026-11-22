"""Read Eye's own alerts and recorded recovery for the lab timeline."""

from datetime import datetime

from psycopg.types.json import Jsonb

from eye.investigation_branches import search_cohort
from eye.simulator import HOUR


COMPLETIONS = {
    "product_view": ("product_page_requested", "product_viewed", "product_page_load_failed"),
    "cart_add": ("cart_add_requested", "cart_added", "cart_add_failed"),
}
PAYMENT = ("payment_attempted", "payment_succeeded", "payment_declined", "payment_timed_out", "order_confirmed")
SEARCH = ("search_requested", "feed_impression", "order_confirmed")


def workflow_readings(db, run):
    """Summarize the applied scenario's scoped simulator events, excluding recovery traffic."""
    if not run.get("start_at") or not run["published"]:
        return None
    start, end = (datetime.fromisoformat(run[key]) for key in ("start_at", "end_at"))
    plan = run["scenario"]
    transition = plan["transition"]
    end = min(end, start + run["published"] * HOUR)
    cutoff = db.execute(
        """select max(coalesce(observation_snapshot, end_at)) filter (where start_at < %s) as effect,
                  max(coalesce(observation_snapshot, end_at)) as latest
           from windows where start_at >= %s""",
        (end, start),
    ).fetchone()
    if not cutoff["effect"]:
        return None
    scope = {("payment_provider" if key == "provider" else key): value for key, value in plan["scope"].items()}
    activities = PAYMENT if transition == "payment" else SEARCH if transition == "search" else COMPLETIONS[transition]
    events = db.execute(
        """select id, activity, occurred_at, ingested_at, attrs
           from events where window_start >= %s and window_start < %s
             and activity = any(%s) and attrs @> %s and occurred_at <= %s
           order by occurred_at, id""",
        (start, end, list(activities), Jsonb(scope), cutoff["effect"]),
    ).fetchall()
    readings = {"transition": transition, "effect_cutoff": cutoff["effect"], "observed_through": cutoff["latest"]}
    if transition == "search":
        fields = ("sessions", "exposed", "orders", "direct_orders", "recommendation_orders",
                  "unclassified_orders", "median_response_ms", "releases")
        readings["cohorts"] = {
            arm: {key: row[key] for key in fields}
            for arm, row in search_cohort([event for event in events if event["ingested_at"] <= cutoff["effect"]]).items()
        }
        return readings

    identity = "payment_id" if transition == "payment" else "session_id"
    request, completion, failure = activities[:3]
    requested = {row["attrs"][identity] for row in events if row["activity"] == request and identity in row["attrs"]}
    def reached(activity, through):
        return {row["attrs"].get(identity) for row in events
                if row["activity"] == activity and row["ingested_at"] <= through} & requested

    first = cutoff["effect"]
    if transition == "payment":
        authorized = {row["attrs"].get(identity) for row in events if row["activity"] == completion} & requested
        timely = reached(completion, first)
        received = reached(completion, cutoff["latest"])
        readings["counts"] = {
            "attempted": len(requested), "authorized": len(authorized), "declined": len(reached(failure, first)),
            "timed_out": len(reached("payment_timed_out", first)), "confirmed": len(reached("order_confirmed", first)),
            "receipts_at_cutoff": len(timely), "in_transit_at_cutoff": len(authorized - timely),
            "received_since_cutoff": len(received - timely), "in_transit_now": len(authorized - received),
        }
    else:
        readings["counts"] = {"requested": len(requested), "completed": len(reached(completion, first)),
                              "failed": len(reached(failure, first))}
    return readings


def evaluate(db, run, checked_through):
    start, end = (datetime.fromisoformat(run[key]) for key in ("start_at", "end_at"))
    alert = db.execute(
        """select a.id, a.window_start, a.detected_at, a.scope, a.metric_id, m.name as metric_name
           from anomalies a join metrics m on m.id = a.metric_id
           where a.window_start >= %s and a.window_start < %s
           order by a.window_start, a.detected_at, a.id limit 1""",
        (start, end),
    ).fetchone()
    result = {"windows_checked": checked_through is not None and checked_through >= end - HOUR}
    if not alert:
        return result

    incident = db.execute(
        """select i.id, i.recovered_at
           from evidence e join incidents i on i.id = e.incident_id
           where e.window_start = %s and e.kind in ('anomaly', 'related anomaly', 'ongoing anomaly')
             and e.payload->>'id' = %s
           order by (e.kind = 'anomaly') desc, e.id limit 1""",
        (alert["window_start"], str(alert["id"])),
    ).fetchone()
    recovered = incident["recovered_at"] if incident else None
    result.update(
        first_alert=alert,
        alert_at=alert["window_start"] + HOUR,
        seconds_to_alert=round((alert["detected_at"] - datetime.fromisoformat(run["wall_started_at"])).total_seconds(), 1)
        if alert["detected_at"] else None,
        incident_id=incident["id"] if incident else None,
        recovery_declared_at=recovered + HOUR if recovered else None,
    )
    return result
