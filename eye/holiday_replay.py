"""Replay the first hour of the planned shopping day in two isolated lab databases."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from psycopg import sql

from eye import runtime, simulator
from eye.db import connect, state
from eye.intervals import GRACE, HOUR, aligned_history
from eye.lab_history import HOLIDAY, holiday_traffic, seed_lab
from eye.scenario import LAB_SEED, Scenario
from eye.transitions import BY_ID, Readings

CHECKOUT_SCOPE = {"country": "DE", "platform": "mobile"}
log = logging.getLogger(__name__)


def replay(dsn, regression):
    plan = Scenario(effect="reduce_completions", transition="checkout", scope=CHECKOUT_SCOPE,
                    severity=.40, duration_hours=1) if regression else None
    with connect(dsn) as db:
        for hour in range(2):
            at = HOLIDAY + hour * HOUR
            simulator.emit(db, at, seed=LAB_SEED, patterned=True, event_traffic=holiday_traffic(at),
                           scenario=plan if hour == 0 else None)
    cutoff = HOLIDAY + HOUR + GRACE
    while runtime.step(dsn):
        with connect(dsn) as db:
            if datetime.fromisoformat(state(db, "last_observed_at")) >= cutoff:
                break
    with connect(dsn) as db:
        comparisons = [row["reading"] for row in db.execute(
            """select reading from planned_event_readings where window_start=%s
               and scope in ('{}','{"country":"DE"}') order by scope""", (HOLIDAY,),
        ).fetchall()]
        baseline = datetime.fromisoformat(state(db, "baseline_through"))
        checkout = Readings(db, HOLIDAY, baseline, aligned_history(HOLIDAY, baseline)).get(BY_ID["checkout"], CHECKOUT_SCOPE)
        incidents = db.execute("""select id, metric_id, scope, report->'break' as first_break,
                                  report->'observation_window' as alert_window, report->'planned_event' as event_evidence
                                  from incidents order by id""").fetchall()
        failures = db.execute("""select count(*) as count, (array_agg(id order by occurred_at,id))[1:3] as examples
                                 from events where activity='checkout_start_failed' and occurred_at >= %s and occurred_at < %s
                                 and ingested_at <= %s and attrs->>'country'=%s and attrs->>'platform'=%s""",
                              (HOLIDAY, HOLIDAY + HOUR, cutoff, CHECKOUT_SCOPE["country"], CHECKOUT_SCOPE["platform"])).fetchone()
    full = next(reading for reading in comparisons if not reading["scope"])
    break_incident = next((row for row in incidents if (row["first_break"] or {}).get("id") == "checkout"), None)
    assert full["observed"] > full["normal"], full
    if regression:
        assert checkout["status"] == "degraded" and break_incident, (checkout, incidents)
        assert full["status"] == "below expectation" and failures["count"] > 0, (full, failures)
    else:
        assert checkout["status"] == "steady" and not incidents, (checkout, incidents)
        assert full["status"] == "within expectation", full
    return {"orders": comparisons, "checkout": checkout, "checkout_failures": failures, "incidents": incidents}


def verify():
    from pgserver import get_server

    with TemporaryDirectory(prefix="eye-holiday-") as directory:
        server = get_server(Path(directory) / "postgres")
        try:
            dsn = server.get_uri()
            cache = seed_lab(dsn, LAB_SEED)
            log.info("Seeded archive %s", cache)
            with connect(dsn) as db:
                counts = db.execute("""select minutes, count(distinct start_at) as buckets, bool_and(complete) as complete
                                       from seasonal_buckets group by minutes order by minutes""").fetchall()
                at = simulator.DEMO_START
                while at < HOLIDAY:
                    simulator.emit(db, at, seed=LAB_SEED, patterned=True)
                    at += HOUR
            assert [(row["minutes"], row["buckets"], row["complete"]) for row in counts] == [(5, 56 * 288, True), (1440, 365, True)], counts
            runtime.initialize(dsn)
            with connect(dsn) as db:
                db.commit()
                db.autocommit = True
                for name in ("holiday_normal", "holiday_checkout"):
                    db.execute(sql.SQL("create database {} template postgres").format(sql.Identifier(name)))
            with ThreadPoolExecutor(max_workers=2) as workers:
                healthy = workers.submit(replay, server.get_uri(database="holiday_normal"), False)
                regression = workers.submit(replay, server.get_uri(database="holiday_checkout"), True)
                return {"seed": LAB_SEED, "history": cache, "buckets": counts,
                        "healthy": healthy.result(), "regression": regression.result()}
        finally:
            server.cleanup()


def main():
    result = verify()
    output = Path(__file__).resolve().parents[1] / ".runtime" / "seasonality" / "holiday-replay.json"
    output.write_text(json.dumps(result, indent=2, default=str) + "\n")
    for case in ("healthy", "regression"):
        report = result[case]
        market = next(row for row in report["orders"] if not row["scope"])
        checkout = report["checkout"]
        print(f"{case} · {market['observed']:.0f} orders · normal {market['normal']:.1f} · "
              f"event {market['adjusted']:.1f} ({market['lower']:.1f}–{market['upper']:.1f}) · "
              f"checkout {checkout['completed']}/{checkout['eligible']} vs {checkout['expected']:.1%} expected")
    print(output)
