"""Lab-only support scenario fixtures, published as ordinary OCEL batches."""

from eye.db import connect, set_state
from eye.intervals import DAY, HOUR
from eye.lab_simulator import publish, reply, serve_commands, started
from eye.simulator import DEMO_START, publication_ticks
from eye.support_fixture import NORMAL_RATE, REGRESSION_RATE, TICKET_SCOPE, support_events

CASES = {
    "intake-surge": {
        "name": "Healthy intake surge", "scope": {}, "volume": 1.65, "duration_hours": 1,
        "lead_in_hours": 0, "follow_up_hours": 1, "regression": False,
        "effect": "Increase ticket intake across both queues. Keep the conditional resolution probability steady.",
        "resolution_probability": NORMAL_RATE,
    },
    "specialist-regression": {
        "name": "Specialist-queue regression", "scope": TICKET_SCOPE, "volume": 1.65, "duration_hours": 1,
        "lead_in_hours": 1, "follow_up_hours": 3, "regression": True,
        "effect": "After a healthy high-volume hour, reduce specialist-queue resolutions and schedule more retries. Keep the other queue healthy.",
        "resolution_probability": REGRESSION_RATE,
    },
}


def seed():
    # Supply aligned references for replays at any time of day on the advancing clock.
    with connect() as db:
        for hour in range(4 * DAY // HOUR, 0, -1):
            support_events(db, DEMO_START - hour * HOUR)
        set_state(db, "instrumentation", {"catalog": "none", "name": "Support Tickets"})


def generate(command, _controls):
    identifier = command["id"]
    plan = CASES[command["scenario"]["case"]]
    with connect() as db:
        next_hour = db.execute("select max(start_at) as last from windows").fetchone()["last"] + HOUR

    lead_in = plan["lead_in_hours"]
    duration = plan["duration_hours"]
    total = lead_in + duration + plan["follow_up_hours"]
    beginning = next_hour + lead_in * HOUR
    started(identifier, beginning, beginning + duration * HOUR, total)
    for offset in range(total):
        start = next_hour + offset * HOUR
        with connect() as db:
            support_events(db, start, volume=plan["volume"] if offset < lead_in + duration else 1,
                           regression=plan["regression"] and lead_in <= offset < lead_in + duration, staged=True)
            publish(db, start, publication_ticks(start, drain=offset + 1 == total))
        reply(id=identifier, event="window", window_start=start.isoformat(), published=offset + 1)
    reply(id=identifier, event="finished")


def main():
    seed()
    reply(event="baseline")
    serve_commands(generate)


if __name__ == "__main__":
    main()
