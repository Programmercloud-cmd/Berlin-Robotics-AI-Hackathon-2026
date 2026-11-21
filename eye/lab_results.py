"""Read Eye's own alerts and recorded recovery for the lab timeline."""

from datetime import datetime

from eye.simulator import HOUR


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
