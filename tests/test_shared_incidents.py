import unittest
from datetime import datetime, timedelta, timezone

from eye.cohort_evidence import matched_cohorts, supports_link
from eye.downstream_impact import assess, linked_steps
from eye.shared_incidents import conflicting_step, source_plans


START = datetime(2026, 11, 22, 10, tzinfo=timezone.utc)


def event(identifier, activity, second, **attrs):
    return {"id": identifier, "activity": activity, "occurred_at": START + timedelta(seconds=second), "attrs": attrs}


class SharedEvidenceTest(unittest.TestCase):
    def test_payment_completion_requires_matching_cart_order_and_payment(self):
        rows = [event("cart", "cart_added", 1), event("wrong-checkout", "checkout_started", 2, order_id="x"),
                event("checkout", "checkout_started", 2, order_id="o"),
                event("attempt", "payment_attempted", 3, order_id="o", payment_id="p"),
                event("other", "payment_succeeded", 4, order_id="o", payment_id="q"),
                event("paid", "payment_succeeded", 5, order_id="o", payment_id="p"),
                event("order", "order_confirmed", 6, order_id="o", payment_id="p")]
        carts = {"cart": "c", "checkout": "c", "wrong-checkout": "other", "attempt": "c"}
        self.assertEqual(linked_steps(rows, carts)["payment"][1]["id"], "paid")
        self.assertIsNone(linked_steps([row for row in rows if row["id"] != "paid"], carts)["payment"][1])

    def test_stock_loss_accounts_for_depleted_carts_without_contemporaneous_controls(self):
        class Readings:
            historic = [1, 2, 3]

            def get(self, _step, _scope):
                return {"expected": .8, "eligible": 100, "completed": 40, "status": "degraded",
                        "baseline_eligible": 300, "baseline_completed": 240}

        target = {"metric_id": "checkout_sessions", "scope": {}}
        cart = event("cart", "cart_added", 1)
        checkout = event("checkout", "checkout_started", 2)
        paths = [(i < 25, True, cart, {"checkout": (cart, None)}) for i in range(50)]
        paths += [(True, False, cart, {"checkout": (cart, checkout if i < 40 else None)}) for i in range(50)]
        decision = assess(paths, "stock", target, Readings())
        self.assertEqual(decision["status"], "linked")
        self.assertEqual(decision["upstream_explained"], 40)
        bad_controls = [(True, False, cart, {"checkout": (cart, None)}) for _ in range(50)]
        self.assertEqual(assess(paths[:50] + bad_controls, "stock", target, Readings())["status"], "independent")

    def test_later_stock_reading_cannot_explain_an_earlier_cart(self):
        rows = [event("s0", "inventory_snapshot", -4, sku="a", available_units=4),
                event("s1", "inventory_snapshot", 30, sku="a", available_units=0)]
        rows.append(event("cart", "cart_added", 20, sku="a", session_id="customer", country="FR", platform="web"))
        evidence = matched_cohorts(rows, "stock", START, START + timedelta(minutes=15), {"country": "FR"})
        self.assertEqual(evidence["preceded"], 0)
        self.assertFalse(supports_link("stock", evidence))

    def test_stock_effect_uses_matched_cart_populations_and_reports_sparse_payment(self):
        rows = [event("a0", "inventory_snapshot", -4, sku="a", available_units=4),
                event("b0", "inventory_snapshot", -4, sku="b", available_units=4),
                event("a1", "inventory_snapshot", 10, sku="a", available_units=0)]
        for i in range(35):
            for sku in ("a", "b"):
                attrs = {"sku": sku, "session_id": f"{sku}{i}", "country": "FR", "platform": "web", "category": "home"}
                rows.append(event(f"cart-{sku}{i}", "cart_added", 20 + i, **attrs))
                if sku == "b":
                    rows.append(event(f"checkout-{i}", "checkout_started", 22 + i, **attrs))
        evidence = matched_cohorts(rows, "stock", START, START + timedelta(minutes=15), {"country": "FR"})
        self.assertTrue(supports_link("stock", evidence))
        self.assertEqual(evidence["rates"]["checkout entry"]["matched_exposed"], 35)
        self.assertEqual(evidence["rates"]["checkout entry"]["control"], 1)
        self.assertEqual(evidence["rates"]["payment success"]["status"], "insufficient")

    def test_two_independent_sources_and_a_degraded_step_keep_their_evidence(self):
        def receipt(source, kind):
            return {"metric_id": "data_completeness_ratio", "scope": {"source_id": source, "event_type": kind}}
        events = [receipt("store", kind) for kind in ("opened", "viewed")]
        events += [receipt("payments", kind) for kind in ("authorized", "attempted")]
        plans = list(source_plans(events, [], None))
        self.assertEqual([plan["scope"] for plan in plans], [{"source_id": "store"}, {"source_id": "payments"}])
        self.assertEqual([len(plan["alerts"]) for plan in plans], [2, 2])
        row = {"metric_id": "successful_payments", "scope": {"country": "FR"}}
        steps = [{"completion": {"status": "degraded", "id": "payment", "scope": {"country": "FR"}}}]
        self.assertTrue(conflicting_step(row, steps, except_step="checkout"))
