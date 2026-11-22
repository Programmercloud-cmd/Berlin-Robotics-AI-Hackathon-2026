"""Private Test Lab fixtures and rubrics for benchmark suite v1."""

from eye.scenario import Scenario

SUITE_VERSION = "1"
CASE_HOURS = 4  # One scored hour and three hours of ordinary recovery traffic.
SEGMENT = {"country": "DE", "platform": "mobile"}
GATEWAY = {**SEGMENT, "provider": "Stripe"}


def scenario(effect, transition, severity, scope):
    return Scenario(effect=effect, transition=transition, severity=severity, duration_hours=1, scope=scope)


CART = scenario("reduce_completions", "cart_add", .72, SEGMENT)
TIMEOUT = scenario("payment_timeout", "payment", .85, GATEWAY)

# Controls go exclusively to the publisher. Rubrics go exclusively to the reviewer.
CASES = (
    ("normal", "Normal traffic", {}, True,
     "Normal traffic. Treat any claimed incident as a possible false positive. Evaluate its actual support and avoid inventing a fault."),
    ("surge", "Benign traffic surge", {"traffic": 1.65}, True,
     "An increase in traffic with normal conditional completion. Look for volume evidence without claiming a broken workflow."),
    ("timeouts", "Scoped gateway timeouts", {"scenarios": (TIMEOUT,)}, False,
     "Find payment completion in Germany/mobile/Stripe. Explicit gateway_timeout events support the mechanism. Provider root cause remains unknown."),
    ("receipts", "Delayed payment success receipts", {"scenarios": (
        scenario("payment_success_delay", "payment", .9, GATEWAY),)}, False,
     "Find missing or delayed success telemetry in payments, especially Germany/mobile/Stripe. Timely linked orders and later receipts support delay. Missing outcomes alone do not establish timeouts or declines."),
    ("frankfurt", "Frankfurt product-page failures", {"scenarios": (
        scenario("reduce_completions", "product_view", .65, {}),)}, False,
     "Reward the marketplace-wide product-view completion break, page requests, and failed loads. Frankfurt is an operator premise. Leave hosting location unresolved unless the supplied telemetry explicitly identifies it."),
    ("cart", "Cart-add failures and downstream payment volume loss", {"scenarios": (CART,)}, False,
     "Find cart-add completion in Germany/mobile. Downstream payment volume can fall with steady conditional payment success. Independent payment failure needs conditional rate evidence."),
    ("concurrent", "Concurrent cart and payment faults", {"scenarios": (CART, TIMEOUT)}, False,
     "Expect separate cart-add completion and payment-success breaks. Cart is Germany/mobile. Payment is Germany/mobile/Stripe. Credit each report for its own evidence and avoid inferring payment failure from volume alone."),
    ("search", "Faster search rollout and recommendation-assisted sales", {"scenarios": (
        scenario("search_rollout", "search", .82, {}),)}, False,
     "Trace lower recommendation exposure and assisted orders to faster search where release, timing and holdout telemetry support it. Check whether direct purchases offset assisted losses and distinguish evidence from causal certainty."),
    ("inventory", "Inventory depletion and checkout loss", {"inventory_depletion": .8}, False,
     "Find marketplace checkout completion loss with observed unavailable stock where available. Do not mistake fewer payment attempts for an independent gateway fault or invent a supply-chain cause."),
    ("storefront", "Dropped storefront telemetry", {"storefront_loss": .35}, False,
     "Find storefront event-delivery completeness loss using expected and received events. Allow uncertainty about business completions. Missing observations are not evidence of real shopper abandonment."),
)
