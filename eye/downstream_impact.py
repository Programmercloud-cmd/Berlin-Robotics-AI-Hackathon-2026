"""Attribute downstream volume to linked, earlier losses in eligibility."""

from collections import defaultdict
from math import sqrt

from eye.transitions import BY_ID

PROJECTIONS = {"checkout_sessions": "checkout", "payment_attempts": "payment",
               "successful_payments": "payment", "orders": "order"}


def linked_steps(events, carts):
    """Walk one session, preserving the cart, order and payment identities at each join."""
    cart = next((row for row in events if row["activity"] == "cart_added"), None)
    if not cart or not (cart_id := carts.get(cart["id"])):
        return {}
    checkout = next((row for row in events if row["activity"] == "checkout_started"
                     and row["occurred_at"] >= cart["occurred_at"] and carts.get(row["id"]) == cart_id), None)
    if not checkout or not (order_id := checkout["attrs"].get("order_id")):
        return {"checkout": (cart, None)}
    attempt = next((row for row in events if row["activity"] == "payment_attempted"
                    and row["occurred_at"] >= checkout["occurred_at"] and carts.get(row["id"]) == cart_id
                    and row["attrs"].get("order_id") == order_id), None)
    path = {"checkout": (cart, checkout)}
    if not attempt or not (payment_id := attempt["attrs"].get("payment_id")):
        return path
    outcome, order = None, None
    for row in events:
        if (row["occurred_at"] < attempt["occurred_at"] or row["attrs"].get("payment_id") != payment_id
                or row["attrs"].get("order_id") != order_id):
            continue
        if row["activity"] == "payment_succeeded":
            outcome = row
        if row["activity"] == "order_confirmed" and outcome:
            order = row
    path["payment"] = (attempt, outcome)
    if outcome:
        path["order"] = (outcome, order)
    return path


def build_paths(db, journeys):
    ids = {row["id"] for _, _, _, events in journeys for row in events
           if row["activity"] in {"cart_added", "checkout_started", "payment_attempted"}}
    carts = {row["event_id"]: row["object_id"] for row in db.execute(
        """select e.event_id, e.object_id from event_objects e join objects o on o.id=e.object_id
           where o.type='cart' and e.event_id=any(%s)""", (sorted(ids),),
    ).fetchall()} if ids else {}
    return [(key, exposed, entry, linked_steps(events, carts)) for key, exposed, entry, events in journeys]


def matching(row, scope):
    return row is not None and all(row["attrs"].get(key) == value for key, value in scope.items())


def projected_loss(paths, step, scope, measure):
    buckets = defaultdict(lambda: [[0, 0, 0], [0, 0, 0]])
    examples = []
    for key, exposed, anchor, path in paths:
        if any(name in anchor["attrs"] and anchor["attrs"][name] != value for name, value in scope.items()):
            continue
        attempt, completed = path.get(step, (None, None))
        eligible = matching(attempt, scope)
        reached = matching((attempt, completed)[measure], scope)
        counts = buckets[key][exposed]
        counts[0] += 1
        counts[1] += reached
        counts[2] += eligible
        if exposed and eligible and len(examples) < 2:
            examples.append({"anchor": anchor["id"], "attempt": attempt["id"], "completion": completed["id"] if completed else None})
    loss = control_eligible = control_completed = exposed_n = control_n = variance = 0
    for (nc, rc, ec), (ne, re, _) in buckets.values():
        if not ne or not nc:
            continue
        loss += ne * rc / nc - re
        exposed_n += ne
        control_n += nc
        control_eligible += ec
        control_completed += rc
        p = (rc + .5) / (nc + 1)
        variance += p * (1 - p) * (ne + ne * ne / nc)
    return {"lost": loss, "matched_exposed": exposed_n, "matched_control": control_n,
            "control_eligible": control_eligible, "control_completed": control_completed,
            "standard_error": sqrt(variance), "linked_event_ids": examples}


def assess(paths, kind, target, readings):
    """Check whether missing attempts can account for the scoped incident's lost completions."""
    step = PROJECTIONS[target["metric_id"]]
    scope = target["scope"]
    rate = readings.get(BY_ID[step], scope)
    if rate["expected"] is None or not rate["eligible"] or rate["baseline_eligible"] < 75:
        return {"status": "provisional", "reason": "Too few scoped opportunities to check downstream completion."}
    # Checkout is where inventory blocks completion. Use the eligible carts as its denominator,
    # and compare the unaffected carts separately to test for another checkout problem.
    gate = kind == "stock" and step == "checkout"
    projection = projected_loss(paths, step, scope, int(gate))
    baseline_rate = rate["expected"]
    baseline_attempts = rate["baseline_eligible"] / len(readings.historic)
    missing_attempts = max(0, baseline_attempts - rate["eligible"])
    conditional_loss = rate["eligible"] * baseline_rate - rate["completed"]
    if gate:
        denominator = projection["control_eligible"]
        control_rate = projection["control_completed"] / denominator if denominator else None
        independent = denominator >= 25 and baseline_rate - control_rate > max(.09, 2 * sqrt(baseline_rate * (1-baseline_rate) / denominator))
        enough = denominator >= 12 and not independent
        shortfall = conditional_loss
        explained = projection["lost"]
    else:
        control_rate = rate["observed"]
        independent = rate["status"] == "degraded"
        enough = rate["status"] == "steady"
        shortfall = missing_attempts if target["metric_id"] == "payment_attempts" else missing_attempts * baseline_rate + conditional_loss
        explained = projection["lost"] if target["metric_id"] == "payment_attempts" else projection["lost"] * baseline_rate
    remainder = max(0, shortfall - explained)
    status, reason = "provisional", "The linked cohorts leave a material volume gap or too little conditional evidence."
    if independent:
        status, reason = "independent", "Completion fell among comparable attempts even after accounting for upstream loss."
    elif (enough and projection["matched_exposed"] >= 12 and projection["matched_control"] >= 12
          and shortfall >= 4 and explained >= .55 * shortfall
          and remainder <= max(4, 1.64 * projection["standard_error"])):
        status, reason = "linked", "The linked upstream loss accounts for the scoped volume shortfall within sampling uncertainty."
    return {"status": status, "reason": reason, "scope": scope, "metric_id": target["metric_id"],
            "baseline_attempts": baseline_attempts, "eligible_attempts": rate["eligible"],
            "completed": rate["completed"], "baseline_completion_rate": baseline_rate, "checked_completion_rate": control_rate,
            "conditional_status": rate["status"], "fewer_attempts_at_baseline_rate": missing_attempts * baseline_rate,
            "conditional_completion_loss": conditional_loss, "scope_volume_loss": shortfall,
            "upstream_explained": explained, "unexplained": remainder, "linked_cohort": projection}
