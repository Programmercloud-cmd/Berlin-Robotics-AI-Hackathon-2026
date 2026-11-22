"""Seeded support-ticket publisher fixture and its instrumented metric definitions."""

from collections import Counter
from datetime import timedelta
from random import Random

from psycopg.types.json import Jsonb

from eye.ingestion import publish_window
from eye.scenario import LAB_SEED

SEED = LAB_SEED
NORMAL_RATE = .90
REGRESSION_RATE = .29
RESOLUTION = "support_ticket_resolution_rate"
TICKET_SCOPE = {"queue": "specialist"}
RESOLUTION_DEFINITION = {
    "id": RESOLUTION, "name": "Resolution per eligible ticket",
    "definition": "Share of resolution-eligible support tickets resolved in the current observation window.",
    "calculation": {"op": "ratio", "activity": "support_ticket_resolved",
                    "denominator_activity": "support_ticket_resolution_eligible"},
    "access": {"source": "support event stream", "unit": "ratio"},
    "direction": "down", "cadence_minutes": 5, "dimensions": ["queue"],
}
INTAKE_DEFINITION = {
    "id": "support_ticket_intake", "name": "Ticket intake", "definition": "Tickets received by each support queue.",
    "calculation": {"op": "count", "activity": "support_ticket_received"},
    "access": {"source": "support event stream", "unit": "count"},
    "direction": "none", "cadence_minutes": 5, "dimensions": ["queue"],
}


def support_events(db, start, *, volume=1.0, regression=False, staged=False):
    rng = Random(f"{SEED}:{start.isoformat()}:tickets")
    objects, events, links, manifest = [], [], [], Counter()
    for i in range(round(volume * (320 + rng.randrange(-9, 10)))):
        ticket = f"ticket:{int(start.timestamp())}:{i}"
        queue = "specialist" if rng.random() < .47 else "standard"
        minute = start + timedelta(minutes=rng.randrange(60))
        attrs = {"source_id": "support", "queue": queue, "ticket_id": ticket}
        objects.append((ticket, "support_ticket", Jsonb(attrs)))
        stages = [("received", {})]
        if rng.random() < .92:
            stages.append(("resolution_eligible", {}))
            rate = REGRESSION_RATE if regression and queue == "specialist" else NORMAL_RATE
            if rng.random() < rate:
                stages.append(("resolved", {"result": "resolved"}))
            else:
                stages.append(("retry_scheduled", {"result": "retry_required"}))
        for index, (stage, extra) in enumerate(stages):
            activity, identifier = "support_ticket_" + stage, ticket + ":" + stage
            timestamp = minute + timedelta(seconds=3 * index)
            events.append((identifier, activity, timestamp, timestamp + timedelta(seconds=2), start,
                           Jsonb({**attrs, **extra})))
            links.append((identifier, ticket, "support ticket"))
            manifest[minute, "support", activity] += 1
    publish_window(db, start, objects, events, links, manifest, staged=staged)
