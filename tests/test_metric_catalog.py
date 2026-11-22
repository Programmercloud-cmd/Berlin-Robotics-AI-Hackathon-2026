import unittest
from datetime import timedelta
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from eye import runtime, simulator
from eye.db import connect, setup
from eye.metric_catalog import CATALOG, STAGED
from eye.web import make_app

try:
    from pgserver import get_server
except ImportError:
    get_server = None


@unittest.skipUnless(get_server, "Install requirements-local.txt for PostgreSQL tests.")
class MetricCatalogTest(unittest.TestCase):
    def test_seeded_readings_and_runtime_registration(self):
        start = simulator.DEMO_START
        end = start + 2 * simulator.HOUR
        with TemporaryDirectory(prefix="eye-metric-catalog-") as directory:
            server = get_server(directory + "/data")
            try:
                dsn = server.get_uri()
                with connect(dsn) as db:
                    setup(db)
                    for at in (start, start + simulator.HOUR):
                        simulator.emit(db, at)
                runtime.initialize(dsn)
                client = TestClient(make_app(dsn))
                try:
                    entries = client.get("/api/catalog").raise_for_status().json()
                    self.assertEqual(len(entries), 36)
                    self.assertEqual({entry["id"] for entry in entries if not entry["registered"]}, set(STAGED))
                    for identifier in STAGED:
                        client.post(f"/api/catalog/{identifier}/register").raise_for_status()
                    entries = client.get("/api/catalog").raise_for_status().json()
                    self.assertTrue(all(entry["readings_available"] for entry in entries))
                    self.assertTrue(all(entry["access"]["source"] for entry in entries))
                    self.assertTrue(any(entry["alerts_enabled"] for entry in entries))
                    self.assertTrue(any(not entry["alerts_enabled"] for entry in entries))

                    values = {entry["id"]: entry["latest"]["value"] for entry in entries}
                    self.assertEqual(values["payment_attempts"], sum(values[key] for key in (
                        "successful_payments", "payment_timeouts", "other_failed_payments",
                    )))
                    self.assertLessEqual(values["checkout_sessions"], values["add_to_cart_sessions"])
                    self.assertLessEqual(values["add_to_cart_sessions"], values["product_view_sessions"])
                    self.assertLessEqual(values["product_view_sessions"], values["sessions"])
                    self.assertEqual(values["data_completeness_ratio"], 1)
                    self.assertAlmostEqual(values["effective_cost_per_click"], values["advertising_spend"] / values["ad_clicks"])
                    self.assertEqual(values["marketplace_contribution"], values["platform_fee_revenue"] -
                                     values["platform_funded_discounts"] - values["advertising_spend"] -
                                     values["variable_operating_cost"])

                    def reading(metric_id, **params):
                        response = client.get(f"/api/catalog/{metric_id}/readings", params=params)
                        return response.raise_for_status().json()["readings"]

                    self.assertEqual(len(reading("sessions")), 12)
                    interval = {"start": start.isoformat(), "end": end.isoformat(), "bucket_minutes": 120}
                    totals = reading("payment_success_rate", **interval)[0]
                    self.assertEqual(totals["value"], totals["details"]["numerator"] / totals["details"]["denominator"])
                    inventory = reading("available_units", **interval)[0]
                    self.assertEqual(inventory["value"], values["available_units"])
                    ads = reading("advertising_spend", **interval)[0]["value"]
                    budget = reading("daily_budget_utilization", **interval)[0]
                    self.assertEqual(budget["details"]["denominator"], 120000)
                    self.assertAlmostEqual(budget["value"], ads / 120000)
                    immature = reading("session_conversion_rate", start=start.isoformat(),
                                       end=(start + timedelta(minutes=5)).isoformat(),
                                       as_of=(start + timedelta(minutes=10)).isoformat())[0]
                    self.assertIsNone(immature["value"])
                    self.assertEqual(immature["details"]["quality"], "immature")
                    self.assertEqual(client.get("/api/catalog/daily_budget_utilization/readings", params={
                        **interval, "end": (end + timedelta(days=1)).isoformat(),
                    }).status_code, 422)
                finally:
                    client.close()
            finally:
                server.cleanup()
