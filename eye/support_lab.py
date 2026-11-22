"""Support Ticket controls and readouts on the private Test Lab."""

from datetime import datetime

import httpx
from fastapi import HTTPException

from eye import runtime
from eye.lab import Lab, log
from eye.lab_results import evaluate
from eye.support_fixture import INTAKE_DEFINITION, RESOLUTION, RESOLUTION_DEFINITION
from eye.support_lab_simulator import CASES


def evaluate_support(db, run, checked):
    result = evaluate(db, run, checked, metric=RESOLUTION, scope=run["scenario"]["scope"], include_tail=True)
    if identifier := result.get("incident_id"):
        result.update(db.execute(
            """select report->>'reasoning_provider' as reasoning_provider,
                      exists(select 1 from pending_reports where incident_id=i.id) as investigation_pending
               from incidents i where i.id=%s""", (identifier,),
        ).fetchone())
    if run["published"] <= run["scenario"]["lead_in_hours"]:
        return result

    start, end = (datetime.fromisoformat(run[key]) for key in ("start_at", "end_at"))
    result["outcomes"] = db.execute(
        """select e.attrs->>'queue' as queue,
                  count(*) filter (where e.activity='support_ticket_received') as received,
                  count(*) filter (where e.activity='support_ticket_resolution_eligible') as eligible,
                  count(*) filter (where e.activity='support_ticket_resolved') as resolved,
                  count(*) filter (where e.activity='support_ticket_retry_scheduled') as retries
           from events e join windows w on w.start_at=e.window_start
           where e.window_start >= %s and e.window_start < %s
             and e.ingested_at <= coalesce(w.published_through, w.observation_snapshot)
           group by rollup(e.attrs->>'queue') having count(*) > 0 order by queue nulls first""", (start, end),
    ).fetchall()
    return result


class SupportLab(Lab):
    publisher = "eye.support_lab_simulator"
    eye_environment = {"EYE_MODEL": "devin", "EYE_INVESTIGATION": "generic"}
    evaluate_run = staticmethod(evaluate_support)

    def progress(self, phase):
        log.info("Support Tickets Lab · %s", phase)

    def configure_eye(self):
        if not self.devin:
            raise RuntimeError("Configure DEVIN_API_KEY and DEVIN_ORG_ID to use Support Tickets.")
        with self.lock:
            self.phase = "registration"
        runtime.register(self.dsn, INTAKE_DEFINITION)
        # The registered formula and discovered activities give Eye the placement.
        # Keep the cloud provider on the report worker so it cannot delay seeded startup.
        response = httpx.post(f"http://127.0.0.1:{self.eye_port}/api/metrics", params={"use_model": "false"},
                              json=RESOLUTION_DEFINITION, timeout=45)
        response.raise_for_status()

    def queue_case(self, case):
        if case not in CASES:
            raise HTTPException(404, "Unknown support case.")
        return self.queue(scenario={"case": case, **CASES[case]}, plan_id=case)

    def snapshot(self):
        return {**super().snapshot(), "cases": CASES}
