"""Local simulator process. Read private lab commands and publish observed PostgreSQL windows."""

import json
import sys
from datetime import datetime, timezone
from time import sleep

from eye import simulator
from eye.db import connect
from eye.scenario import LAB_SEED, RECOVERY_HOURS, Scenario


def reply(**message):
    print(json.dumps(message, default=str), flush=True)


def generate(command):
    identifier = command["id"]
    spec = Scenario.model_validate(command["scenario"]) if command.get("scenario") else None
    hours = spec.duration_hours if spec else command["hours"]
    with connect() as db:
        clock = db.execute("select max(start_at) as last from windows").fetchone()["last"] + simulator.HOUR
    start = clock
    end = start + hours * simulator.HOUR
    total = hours + (RECOVERY_HOURS if spec else 0)
    reply(id=identifier, event="started", start_at=start.isoformat(), end_at=end.isoformat(),
          wall_started_at=datetime.now(timezone.utc).isoformat(), total=total)

    for index in range(total):
        with connect() as db:
            simulator.emit(db, clock, seed=LAB_SEED, patterned=True, scenario=spec if index < hours else None)
        reply(id=identifier, event="window", window_start=clock.isoformat(), published=index + 1)
        clock += simulator.HOUR
        sleep(0.8)
    reply(id=identifier, event="finished")


def main():
    with connect() as db:
        simulator.seed(db, seed=LAB_SEED, patterned=True)
    reply(event="baseline")
    for line in sys.stdin:
        command = json.loads(line)
        try:
            generate(command)
        except Exception as exc:
            reply(id=command["id"], event="error", error=type(exc).__name__)


if __name__ == "__main__":
    main()
