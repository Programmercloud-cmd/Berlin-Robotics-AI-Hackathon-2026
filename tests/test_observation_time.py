import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from eye.db import connect, set_state, setup
from eye.intervals import GRACE, aligned_history
from eye.metric_catalog import CATALOG
from eye.monitor import detect
from eye.observation import observation_tool
from eye.web import make_app

try:
    from pgserver import get_server
except ImportError:
    get_server = None


@unittest.skipUnless(get_server, "Install requirements-local.txt for PostgreSQL tests.")
class ObservationTimeTest(unittest.TestCase):
    def test_receipt_loss_can_alert_at_source_scope_with_healthy_global_completeness(self):
        start = datetime(2026, 11, 21, 9, 5, tzinfo=timezone.utc)
        hour = timedelta(hours=1)
        metric = CATALOG["data_completeness_ratio"]
        scope = {"source_id": "payments", "event_type": "payment_succeeded"}
        with TemporaryDirectory(prefix="eye-observation-alert-") as directory:
            server = get_server(directory + "/data")
            try:
                with connect(server.get_uri()) as db:
                    setup(db)
                    db.execute(
                        """insert into metrics(id, name, definition, calculation, dimensions, access, direction)
                           values (%s, %s, %s, %s, %s, %s, %s)""",
                        (metric["id"], metric["name"], metric["definition"], Jsonb(metric["calculation"]),
                         metric["dimensions"], Jsonb(metric["access"]), metric["direction"]),
                    )
                    historic_successes = 0
                    historic_population = 0
                    for day in (3, 2, 1, 0):
                        stamp = start - timedelta(days=day)
                        db.execute("insert into windows (start_at, end_at, expected_events) values (%s, %s, 6000)",
                                   (stamp, stamp + hour))
                        count = day * 100 if day else 300
                        received = count - day ** 2 if day else 192
                        if day:
                            historic_successes += received
                            historic_population += count
                        for segment, count, value in (({}, 6000, 1 if day else .982), (scope, count, received / count)):
                            db.execute(
                                """insert into observations (metric_id, window_start, scope, value, sample_size)
                                   values (%s, %s, %s, %s, %s)""",
                                (metric["id"], stamp, Jsonb(segment), value, count),
                            )
                    # A different five-minute phase must not influence the scoped baseline.
                    db.execute("""insert into observations (metric_id, window_start, scope, value, sample_size)
                                  values (%s, %s, %s, .5, 10000)""",
                               (metric["id"], stamp - timedelta(days=1, minutes=5), Jsonb(scope)))
                    alerts = detect(db, start, aligned_history(start, start - timedelta(days=1)), start + hour + GRACE)
                    self.assertEqual([alert["scope"] for alert in alerts], [scope])
                    self.assertEqual(alerts[0]["window_end"], start + hour)
                    self.assertAlmostEqual(alerts[0]["expected"], historic_successes / historic_population)
            finally:
                server.cleanup()

    def test_late_receipt_is_hidden_until_the_observed_cutoff_advances(self):
        start = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)
        hour = timedelta(hours=1)
        next_start = start + hour
        with TemporaryDirectory(prefix="eye-observation-time-") as directory:
            server = get_server(directory + "/data")
            try:
                dsn = server.get_uri()
                with connect(dsn) as db:
                    setup(db)
                    for stamp in (start, next_start):
                        db.execute(
                            """insert into windows (start_at, end_at, expected_events, observation_snapshot)
                               values (%s, %s, 2, %s)""",
                            (stamp, stamp + hour, stamp + hour + timedelta(minutes=15)),
                        )
                    db.execute(
                        """insert into events (id, activity, occurred_at, ingested_at, window_start)
                           values ('attempt', 'payment_attempted', %s, %s, %s),
                                  ('late-success', 'payment_succeeded', %s, %s, %s)""",
                        (start + timedelta(minutes=10), start + timedelta(minutes=10), start,
                         start + timedelta(minutes=10, seconds=2), start + timedelta(minutes=90), start),
                    )
                    set_state(db, "last_window", start.isoformat())
                    tools = observation_tool(db)
                    self.assertEqual([row["id"] for row in tools("supporting_events", window_start=start)], ["attempt"])
                    context = tools("company_context", window_start=start)
                    self.assertEqual(context["activities"], [{"activity": "payment_attempted", "events": 1}])
                    rolling_start = start + timedelta(minutes=5)
                    cutoff = rolling_start + hour + GRACE
                    self.assertEqual([row["id"] for row in tools("supporting_events", window_start=rolling_start,
                                      as_of=cutoff)], ["attempt"])
                    self.assertEqual({row["id"] for row in tools("supporting_events", window_start=rolling_start,
                                     as_of=cutoff + GRACE)}, {"attempt", "late-success"})
                    set_state(db, "last_window", rolling_start.isoformat())
                    set_state(db, "last_observed_at", cutoff.isoformat())

                client = TestClient(make_app(dsn))
                self.assertEqual([row["id"] for row in client.get("/api/events").json()], ["attempt"])
                with connect(dsn) as db:
                    set_state(db, "last_window", next_start.isoformat())
                    set_state(db, "last_observed_at", (next_start + hour + GRACE).isoformat())
                self.assertEqual({row["id"] for row in client.get("/api/events").json()}, {"attempt", "late-success"})
            finally:
                server.cleanup()
