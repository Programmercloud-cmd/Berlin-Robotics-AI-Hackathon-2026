import unittest
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from pydantic import ValidationError

from eye import simulator
from eye.db import connect, setup
from eye.lab_results import evaluate
from eye.scenario import Scenario

try:
    from pgserver import get_server
except ImportError:
    get_server = None


PAYMENT = {
    "fault_stage": "payment_timeout", "scope": {"country": "DE"},
    "severity": 0.65, "duration_hours": 3,
}


class TestLab(unittest.TestCase):
    def test_normal_cycle_repeats_and_scenario_rejects_invalid_controls(self):
        start = simulator.DEMO_START
        self.assertEqual(simulator.traffic_cycle(start), simulator.traffic_cycle(start + timedelta(weeks=4)))
        self.assertNotEqual(simulator.traffic_cycle(start)[0], simulator.traffic_cycle(start + timedelta(weeks=2))[0])
        self.assertEqual(Scenario.model_validate(PAYMENT).model_dump(exclude_none=True), PAYMENT)
        product_page = {**PAYMENT, "fault_stage": "product_page_load", "scope": {},
                        "premise": "Product pages hosted in Frankfurt fail to load."}
        self.assertEqual(Scenario.model_validate(product_page).model_dump(exclude_none=True), product_page)
        for change in ({"scope": {}}, {"scope": {"country": "US"}}, {"duration_hours": 25},
                       {"fault_stage": "checkout"}, {"severity": 1.1}):
            with self.assertRaises(ValidationError):
                Scenario.model_validate({**PAYMENT, **change})
        with self.assertRaises(ValidationError):
            Scenario.model_validate({**product_page, "scope": {"provider": "Stripe"}})

    def test_timeline_uses_the_actual_alert_and_its_incident(self):
        start = simulator.DEMO_START
        end = start + 3 * simulator.HOUR
        run = {"scenario": PAYMENT, "start_at": start.isoformat(), "end_at": end.isoformat(),
               "wall_started_at": start.isoformat()}
        first = {"id": 4, "window_start": start, "scope": {"country": "FR"},
                 "metric_id": "successful_payments", "metric_name": "Successful payments",
                 "detected_at": start + timedelta(seconds=2)}
        db = Mock()
        recovery_time = end + 2 * simulator.HOUR
        db.execute.side_effect = [Mock(fetchone=Mock(return_value=first)),
                                  Mock(fetchone=Mock(return_value={"id": 3, "recovered_at": recovery_time - simulator.HOUR}))]
        result = evaluate(db, run, end)
        self.assertEqual(db.execute.call_args_list[0].args[1], (start, end))
        self.assertEqual(db.execute.call_args_list[1].args[1], (start, "4"))
        self.assertEqual(result["first_alert"], first)
        self.assertEqual(result["incident_id"], 3)
        self.assertEqual(result["seconds_to_alert"], 2)
        self.assertEqual(result["recovery_declared_at"], recovery_time)

    @unittest.skipUnless(get_server, "Install requirements-local.txt for PostgreSQL tests.")
    def test_repeated_traffic_uses_fresh_ids_and_only_scoped_outcomes_change(self):
        start = simulator.DEMO_START
        later = start + timedelta(weeks=4)
        with TemporaryDirectory(prefix="eye-cycle-test-") as directory:
            server = get_server(directory + "/data")
            try:
                with connect(server.get_uri()) as db:
                    setup(db)
                    simulator.emit(db, start, patterned=True)
                    simulator.emit(db, later, patterned=True, scenario=Scenario.model_validate(PAYMENT))
                    windows = db.execute(
                        "select window_start, min(occurred_at) as first, max(occurred_at) as last, "
                        "min(id) as first_id from events "
                        "group by window_start order by window_start"
                    ).fetchall()
                    self.assertLess(windows[0]["last"], windows[1]["first"])
                    self.assertNotEqual(windows[0]["first_id"], windows[1]["first_id"])
                    rates = db.execute(
                        """select attrs->>'country' as country,
                                  avg((activity = 'payment_timed_out')::int) as timeout_rate
                           from events where window_start = %s and activity in
                           ('payment_timed_out', 'payment_succeeded', 'payment_declined')
                           group by attrs->>'country'""",
                        (later,),
                    ).fetchall()
                    self.assertFalse(db.execute(
                        """select exists (select 1 from events where
                           attrs ?| array['scenario', 'fault_stage', 'severity', 'expected_scope']) as leaked"""
                    ).fetchone()["leaked"])
                by_country = {row["country"]: row["timeout_rate"] for row in rates}
                self.assertGreater(by_country["DE"], 0.5)
                self.assertLess(by_country["FR"], 0.08)
            finally:
                server.cleanup()
