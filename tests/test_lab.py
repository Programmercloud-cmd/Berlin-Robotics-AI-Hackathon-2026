import unittest
from collections import Counter
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from pydantic import ValidationError

from eye import simulator
from eye.db import connect, setup
from eye.lab_results import evaluate, workflow_readings
from eye.scenario import Scenario
from eye.support_fixture import support_events
from eye.support_lab import evaluate_support
from eye.support_lab_simulator import CASES

try:
    from pgserver import get_server
except ImportError:
    get_server = None


PAYMENT = {
    "effect": "payment_timeout", "transition": "payment", "scope": {"country": "DE"},
    "severity": 0.65, "duration_hours": 3,
}
COMPLETION_LOSS = {"effect": "reduce_completions", "severity": 0.65, "duration_hours": 1}


class TestLab(unittest.TestCase):
    @unittest.skipUnless(get_server, "Install requirements-local.txt for PostgreSQL tests.")
    def test_support_cases_report_outcomes_and_wait_for_overlapping_checks(self):
        with TemporaryDirectory(prefix="eye-support-lab-") as directory:
            server = get_server(directory + "/data")
            try:
                with connect(server.get_uri()) as db:
                    setup(db)
                    start = simulator.DEMO_START
                    measured = {}
                    for index, (case, plan) in enumerate(CASES.items()):
                        beginning = start + index * simulator.HOUR
                        end = beginning + simulator.HOUR
                        support_events(db, beginning, volume=plan["volume"], regression=plan["regression"], staged=True)
                        run = {"scenario": plan, "start_at": beginning.isoformat(), "end_at": end.isoformat(),
                               "published": plan["lead_in_hours"] + 1}
                        before = evaluate_support(db, run, beginning)
                        self.assertFalse(before["windows_checked"])
                        self.assertFalse(before["outcomes"])
                        simulator.publish_through(db, beginning, end + simulator.GRACE)
                        after = evaluate_support(db, run, end)
                        self.assertTrue(after["windows_checked"])
                        measured[case] = {row["queue"]: row for row in after["outcomes"]}
                    healthy = measured["intake-surge"]["specialist"]
                    sick = measured["specialist-regression"]["specialist"]
                    self.assertGreater(healthy["resolved"] / healthy["eligible"], .85)
                    self.assertLess(sick["resolved"] / sick["eligible"], .40)
                    self.assertGreater(sick["retries"], healthy["retries"])
            finally:
                server.cleanup()

    def test_normal_cycle_repeats_and_scenario_rejects_invalid_controls(self):
        start = simulator.DEMO_START
        self.assertEqual(simulator.traffic_cycle(start), simulator.traffic_cycle(start + timedelta(weeks=4)))
        self.assertNotEqual(simulator.traffic_cycle(start)[0], simulator.traffic_cycle(start + timedelta(weeks=2))[0])
        self.assertEqual(Scenario.model_validate(PAYMENT).model_dump(exclude_none=True), PAYMENT)
        for transition in ("product_view", "cart_add"):
            plan = {**COMPLETION_LOSS, "transition": transition}
            self.assertEqual(Scenario.model_validate(plan).model_dump(exclude_none=True), {**plan, "scope": {}})
        legacy = {"fault_stage": "product_page_load", "scope": {}, "severity": 0.65, "duration_hours": 1}
        self.assertEqual(Scenario.model_validate(legacy).model_dump(exclude_none=True),
                         {**COMPLETION_LOSS, "transition": "product_view", "scope": {}})
        self.assertEqual(Scenario.model_validate({**PAYMENT, "scope": {}}).scope.model_dump(exclude_none=True), {})
        for change in ({"scope": {"country": "US"}}, {"duration_hours": 25},
                       {"transition": "checkout"}, {"severity": 1.1}, {"effect": "reduce_completions"}):
            with self.assertRaises(ValidationError):
                Scenario.model_validate({**PAYMENT, **change})
        with self.assertRaises(ValidationError):
            Scenario.model_validate({**COMPLETION_LOSS, "transition": "cart_add", "scope": {"provider": "Stripe"}})

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
    def test_payment_receipts_distinguish_gateway_timeouts_from_reporting_delay(self):
        with TemporaryDirectory(prefix="eye-lab-receipts-") as directory:
            server = get_server(directory + "/data")
            try:
                with connect(server.get_uri()) as db:
                    setup(db)
                    for effect in ("payment_timeout", "payment_success_delay"):
                        with self.subTest(effect=effect), db.transaction(force_rollback=True):
                            plan = Scenario(effect=effect, transition="payment", severity=0.8,
                                            scope=simulator.PAYMENT_SCOPE, duration_hours=1)
                            simulator.emit(db, simulator.DEMO_START, patterned=True, scenario=plan)
                            rows = db.execute(
                                """select activity, ingested_at > observation_snapshot as late, count(*) as n
                                   from events join windows on window_start = start_at
                                   where attrs->>'country' = 'DE' and attrs->>'platform' = 'mobile'
                                     and attrs->>'provider' = 'Stripe'
                                     and activity in ('payment_succeeded', 'payment_timed_out', 'order_confirmed')
                                   group by activity, late"""
                            ).fetchall()
                            counts = Counter({(row["activity"], row["late"]): row["n"] for row in rows})
                            start = simulator.DEMO_START
                            run = {"start_at": start.isoformat(), "end_at": (start + simulator.HOUR).isoformat(),
                                   "published": 1, "scenario": plan.model_dump(exclude_none=True)}
                            mapped = workflow_readings(db, run)["counts"]
                            self.assertEqual(mapped["timed_out"], counts["payment_timed_out", False])
                            self.assertEqual(mapped["in_transit_at_cutoff"], counts["payment_succeeded", True])
                            self.assertEqual(mapped["authorized"], mapped["confirmed"])
                            if effect == "payment_timeout":
                                self.assertGreater(counts["payment_timed_out", False], 40)
                                self.assertEqual(counts["payment_succeeded", True], 0)
                            else:
                                self.assertGreater(counts["payment_succeeded", True], 40)
                                self.assertLess(counts["payment_timed_out", False], 15)
                                self.assertEqual(counts["order_confirmed", False],
                                                 counts["payment_succeeded", False] + counts["payment_succeeded", True])
                                self.assertEqual(mapped["in_transit_now"], mapped["in_transit_at_cutoff"])
                                simulator.emit(db, start + simulator.HOUR, patterned=True)
                                simulator.emit(db, start + 2 * simulator.HOUR, patterned=True)
                                later = workflow_readings(db, run)["counts"]
                                self.assertEqual(later["received_since_cutoff"], mapped["in_transit_at_cutoff"])
                                self.assertEqual(later["confirmed"], mapped["confirmed"])
                                self.assertEqual(later["in_transit_now"], 0)
            finally:
                server.cleanup()

    @unittest.skipUnless(get_server, "Install requirements-local.txt for PostgreSQL tests.")
    def test_completion_losses_preserve_attempts_and_the_upstream_step(self):
        def counts(db, scenario=None):
            with db.transaction(force_rollback=True):
                simulator.emit(db, simulator.DEMO_START, patterned=True, scenario=scenario)
                rows = db.execute(
                    """select activity, count(distinct attrs->>'session_id') as sessions
                       from events group by activity"""
                ).fetchall()
                self.assertFalse(db.execute(
                    """select exists (select 1 from events where
                       attrs ?| array['premise', 'effect', 'transition', 'severity']) as leaked"""
                ).fetchone()["leaked"])
                return {row["activity"]: row["sessions"] for row in rows}

        with TemporaryDirectory(prefix="eye-completion-test-") as directory:
            server = get_server(directory + "/data")
            try:
                with connect(server.get_uri()) as db:
                    setup(db)
                    normal = counts(db)
                    for transition, requested, succeeded, failed, upstream in (
                        ("product_view", "product_page_requested", "product_viewed",
                         "product_page_load_failed", "session_started"),
                        ("cart_add", "cart_add_requested", "cart_added", "cart_add_failed", "product_viewed"),
                    ):
                        with self.subTest(transition=transition):
                            plan = Scenario.model_validate({**COMPLETION_LOSS, "transition": transition,
                                                            "premise": "Private hypothetical cause"})
                            affected = counts(db, plan)
                            self.assertEqual(affected[upstream], normal[upstream])
                            self.assertEqual(affected[requested], normal[requested])
                            self.assertLess(affected[succeeded], normal[succeeded] / 2)
                            self.assertEqual(affected[succeeded] + affected[failed], normal[requested])
            finally:
                server.cleanup()

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
