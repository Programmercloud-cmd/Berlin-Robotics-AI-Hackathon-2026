import unittest
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from eye import investigation, runtime, simulator
from eye.db import connect, setup, state
from eye.scenario import Scenario
from eye.transitions import BY_ID, Readings

try:
    from pgserver import get_server
except ImportError:
    get_server = None


SCOPE = {"country": "DE", "platform": "mobile"}


class IncidentGroupingTest(unittest.TestCase):
    def test_missing_current_request_events_leave_completion_unknown(self):
        start = simulator.DEMO_START
        history = start - timedelta(days=1)
        db = Mock()
        db.execute.side_effect = [
            Mock(fetchall=Mock(return_value=[{"start_at": history}])),
            Mock(fetchall=Mock(return_value=[
                {"window_start": history, "activity": "cart_add_requested", "attrs": {"session_id": "old"}},
                {"window_start": start, "activity": "product_viewed", "attrs": {"session_id": "current"}},
            ])),
        ]
        rate = Readings(db, start, history).get(BY_ID["cart_add"], {})
        self.assertEqual(rate["eligible"], 0)
        self.assertIsNone(rate["observed"])
        self.assertEqual(rate["status"], "uncertain")

    def test_sparse_downstream_evidence_does_not_imply_an_independent_break(self):
        readings = Mock()
        readings.get.side_effect = lambda step, scope: {
            "id": step.id, "label": step.label, "status": "uncertain" if step.id == "payment" else "steady",
        }
        cart = {
            "completion": {"id": "cart_add", "status": "degraded", "scope": SCOPE, "strength": .5},
            "alerts": [],
        }
        payment_volume = {"metric_id": "successful_payments", "scope": SCOPE}
        self.assertEqual(investigation.assign_workflow_alerts(readings, [cart], [payment_volume]), [])
        self.assertEqual(cart["alerts"][0]["relationship"], "uncertain")

    @unittest.skipUnless(get_server, "Install requirements-local.txt for PostgreSQL tests.")
    def test_cart_volume_loss_and_independent_payment_failure(self):
        cart = Scenario(effect="reduce_completions", transition="cart_add", scope=SCOPE,
                        severity=.62, duration_hours=1)
        payment = Scenario(effect="payment_timeout", transition="payment", scope=SCOPE,
                           severity=.65, duration_hours=1)
        first = simulator.DEMO_START
        with TemporaryDirectory(prefix="eye-incident-grouping-") as directory:
            server = get_server(directory + "/data")
            try:
                dsn = server.get_uri()
                with connect(dsn) as db:
                    setup(db)
                    for day in (3, 2, 1):
                        for hour in range(8):
                            start = first - timedelta(days=day) + hour * simulator.HOUR
                            simulator.emit(db, start, patterned=True)
                self.assertTrue(runtime.initialize(dsn))
                windows = [(cart,), (), (), (cart, payment), (cart,), (cart,), (), ()]
                for hour, effects in enumerate(windows):
                    start = first + hour * simulator.HOUR
                    with connect(dsn) as db:
                        simulator.emit(db, start, patterned=True, scenarios=effects)
                    self.assertTrue(runtime.step(dsn))
                    if hour not in (0, 2, 3, 5, 7):
                        continue
                    with connect(dsn) as db:
                        incidents = db.execute(
                            "select * from incidents where recovered_at is null order by id"
                        ).fetchall()
                        steps = {incident["report"]["break"]["id"]: incident for incident in incidents}
                        if hour == 0:
                            self.assertEqual(len(incidents), 1)
                            self.assertEqual(set(steps), {"cart_add"})
                            baseline = datetime.fromisoformat(state(db, "baseline_through"))
                            rate = Readings(db, start, baseline).get(BY_ID["payment"], SCOPE)
                            self.assertEqual(rate["status"], "steady")
                            self.assertAlmostEqual(rate["observed"], rate["expected"], delta=.04)
                            rows = db.execute("select payload from evidence where incident_id = %s",
                                              (steps["cart_add"]["id"],)).fetchall()
                            self.assertTrue(any(row["payload"].get("metric_id") == "payment_attempts"
                                                and row["payload"].get("relationship") == "downstream volume"
                                                for row in rows if isinstance(row["payload"], dict)))
                        elif hour == 3:
                            self.assertEqual(len(incidents), 2)
                            self.assertEqual(set(steps), {"cart_add", "payment"})
                            for incident in incidents:
                                self.assertEqual(incident["scope"], SCOPE)
                                self.assertLess(incident["report"]["break"]["observed"],
                                                incident["report"]["break"]["expected"] - .3)
                        elif hour == 5:
                            self.assertEqual(set(steps), {"cart_add"})
                            recovered = db.execute(
                                "select recovered_at from incidents where metric_id = 'payment_success_rate'"
                            ).fetchone()
                            self.assertEqual(recovered["recovered_at"], start)
                        else:
                            self.assertFalse(incidents)
            finally:
                server.cleanup()
