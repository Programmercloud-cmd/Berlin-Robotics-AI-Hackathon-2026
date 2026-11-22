"""Local simulator process. Read private lab commands and publish observed PostgreSQL windows."""

import json
import os
import sys
from datetime import datetime, timezone
from queue import Empty, Queue
from threading import Thread
from time import sleep

from eye import simulator
from eye.db import connect
from eye.lab_history import holiday_traffic, seed_lab
from eye.scenario import LAB_SEED, RECOVERY_HOURS, Scenario

TICK_DELAY = 0.6
CONTINUOUS_TICK_DELAY = 3.0


def reply(**message):
    print(json.dumps(message, default=str), flush=True)


def publish(db, start, cutoffs, *, delay=TICK_DELAY):
    for cutoff in cutoffs:
        simulator.publish_through(db, start, cutoff)
        db.commit()
        sleep(delay)


def started(identifier, start, end, total):
    reply(id=identifier, event="started", start_at=start.isoformat(), end_at=end.isoformat() if end else None,
          wall_started_at=datetime.now(timezone.utc).isoformat(), total=total)


def generate(command, controls):
    identifier = command["id"]
    continuous = command.get("continuous", False)
    spec = Scenario.model_validate(command["scenario"]) if command.get("scenario") else None
    hours = spec.duration_hours if spec else command["hours"]
    with connect() as db:
        clock = db.execute("select max(start_at) as last from windows").fetchone()["last"] + simulator.HOUR
    start = clock
    end = None if continuous else start + hours * simulator.HOUR
    total = None if continuous else hours + (RECOVERY_HOURS if spec else 0)
    delay = CONTINUOUS_TICK_DELAY if continuous else TICK_DELAY
    started(identifier, start, end, total)

    published = 0
    injection = None
    while total is None or published < total:
        if continuous and injection is None:
            try:
                control = controls.get_nowait()
            except Empty:
                control = None
            if control and control.get("stop"):
                break
            if control:
                plan = Scenario.model_validate(control["scenario"])
                injection = (control["id"], clock, plan)
                started(control["id"], clock, clock + plan.duration_hours * simulator.HOUR, plan.duration_hours)
        effect = injection[2] if injection else (spec if spec and published < hours else None)
        with connect() as db:
            simulator.emit(db, clock, seed=LAB_SEED, patterned=True, scenario=effect,
                           staged=True, event_traffic=holiday_traffic(clock))
            publish(db, clock, simulator.publication_ticks(clock, drain=published + 1 == total), delay=delay)
        published += 1
        reply(id=identifier, event="window", window_start=clock.isoformat(), published=published)
        if injection:
            injected_id, injected_start, plan = injection
            elapsed = (clock - injected_start) // simulator.HOUR + 1
            reply(id=injected_id, event="window", window_start=clock.isoformat(), published=elapsed)
            if elapsed == plan.duration_hours:
                reply(id=injected_id, event="finished")
                injection = None
        clock += simulator.HOUR
    if continuous:
        if published:
            last = clock - simulator.HOUR
            with connect() as db:
                publish(db, last, (cutoff for cutoff in simulator.publication_ticks(last, drain=True) if cutoff > clock))
        reply(id=identifier, event="finished", end_at=clock.isoformat(), total=published)
    else:
        reply(id=identifier, event="finished")


def read_commands(commands):
    active_id, controls = None, Queue()
    # Keep the reader off sys.stdin's buffered stream during process shutdown.
    with os.fdopen(sys.stdin.fileno(), "rb", buffering=0, closefd=False) as source:
        for line in source:
            command = json.loads(line)
            if command.get("stop"):
                if command["id"] == active_id:
                    controls.put(command)
                continue
            if stream := command.get("inject_into"):
                if stream == active_id:
                    controls.put(command)
                else:
                    reply(id=command["id"], event="error", error="StreamUnavailable")
                continue
            active_id, controls = command["id"], Queue()
            commands.put((command, controls))
    controls.put({"stop": True})
    commands.put(None)


def main():
    reply(event="baseline", history=seed_lab(os.environ["DATABASE_URL"], LAB_SEED))
    commands = Queue()
    Thread(target=read_commands, args=(commands,), daemon=True).start()
    while (job := commands.get()) is not None:
        command, controls = job
        try:
            generate(command, controls)
        except Exception as exc:
            reply(id=command["id"], event="error", error=type(exc).__name__)


if __name__ == "__main__":
    main()
