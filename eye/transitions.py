"""Compare observed workflow outcomes with the shoppers who reached each step."""

from collections import defaultdict
from dataclasses import dataclass
from math import sqrt

from scipy.stats import binom

from eye.intervals import GRACE, HOUR

@dataclass(frozen=True)
class Transition:
    id: str
    label: str
    entry: str
    outcome: str
    metric: str
    metrics: tuple[str, ...]
    failure: str | None = None
    previous_entry: str | None = None
    identity: str = "session_id"
    dimensions: tuple[str, ...] = ("country", "platform")


WORKFLOW = (
    Transition("product_view", "Product view completion", "product_page_requested", "product_viewed",
               "product_view_sessions", ("sessions", "product_view_sessions"), "product_page_load_failed", "session_started"),
    Transition("cart_add", "Cart add completion", "cart_add_requested", "cart_added",
               "add_to_cart_sessions", ("product_view_sessions", "add_to_cart_sessions"), "cart_add_failed", "product_viewed"),
    Transition("checkout", "Checkout start", "cart_added", "checkout_started",
               "checkout_sessions", ("add_to_cart_sessions", "checkout_sessions")),
    Transition("payment_attempt", "Payment initiation", "checkout_started", "payment_attempted",
               "payment_attempts", ("checkout_sessions", "payment_attempts")),
    Transition("payment", "Payment success", "payment_attempted", "payment_succeeded",
               "payment_success_rate", ("payment_attempts", "payment_success_rate", "successful_payments"),
               "payment_timed_out", identity="payment_id", dimensions=("country", "platform", "payment_provider")),
    Transition("order", "Order confirmation", "payment_succeeded", "order_confirmed",
               "orders", ("successful_payments", "orders"), identity="payment_id",
               dimensions=("country", "platform", "payment_provider")),
)
POSITIONS = {step.id: index for index, step in enumerate(WORKFLOW)}
BY_ID = {step.id: step for step in WORKFLOW}
ALERT_STEPS = {
    "sessions": -1, "product_view_sessions": 0, "add_to_cart_sessions": 1,
    "checkout_sessions": 2, "payment_attempts": 3, "successful_payments": 4,
    "payment_success_rate": 4, "payment_timeout_rate": 4, "payment_timeouts": 4,
    "other_failed_payments": 4, "orders": 5, "session_conversion_rate": 5,
    "checkout_completion_rate": 5,
}


def compatible(left, right):
    return all(right.get(key, value) == value for key, value in left.items())


def scopes(segment, dimensions):
    pairs = tuple((key, segment[key]) for key in dimensions if segment.get(key))
    yield ()
    for index in range(1, 1 << len(pairs)):
        yield tuple(pair for bit, pair in enumerate(pairs) if index & (1 << bit))


class Readings:
    def __init__(self, db, start, baseline, history=None, facts=None):
        activities = {name for step in WORKFLOW for name in (step.entry, step.outcome, step.previous_entry) if name}
        following = defaultdict(lambda: defaultdict(list))
        windows = defaultdict(lambda: defaultdict(list))
        if history is not None:
            self.historic = history
            facts = facts or {}
            dates = [start, *history]
            for beginning in dates:
                if beginning not in facts:
                    continue
                for activity in activities:
                    windows[beginning][activity] = [row["attrs"] for row in facts[beginning].by_activity[activity]]
                    following[beginning][activity] = [row["attrs"] for row in facts[beginning].following[activity]]
            missing = [beginning for beginning in dates if beginning not in facts]
            rows = []
            if missing:
                rows = db.execute(
                    """select p.start_at as window_start, e.occurred_at, e.activity, e.attrs
                       from unnest(%s::timestamptz[]) p(start_at) join events e
                         on e.occurred_at >= p.start_at and e.occurred_at < p.start_at + %s
                        and e.ingested_at <= p.start_at + %s
                       where e.activity = any(%s)""",
                    (missing, HOUR + GRACE, HOUR + GRACE, sorted(activities)),
                ).fetchall()
            for row in rows:
                window = row["window_start"]
                following[window][row["activity"]].append(row["attrs"])
                if row["occurred_at"] < window + HOUR:
                    windows[window][row["activity"]].append(row["attrs"])
        else:
            self.historic = self.legacy_windows(db, start, baseline, activities, windows)
            following = windows
        self.data = {}
        for step in WORKFLOW:
            # Compare like populations when a database predates request telemetry.
            entry = step.entry
            if step.previous_entry and not any(windows[window][entry] for window in self.historic):
                entry = step.previous_entry
            current = self.counts(windows[start], step, entry, following[start])
            history_counts = defaultdict(lambda: [0, 0])
            for window in self.historic:
                for key, (completed, eligible) in self.counts(windows[window], step, entry, following[window]).items():
                    history_counts[key][0] += completed
                    history_counts[key][1] += eligible
            self.data[step.id] = (entry, current, history_counts)

    @staticmethod
    def legacy_windows(db, start, baseline, activities, windows):
        historic = db.execute(
            """select start_at from windows where start_at <= %s
               order by (extract(hour from start_at) = extract(hour from %s::timestamptz)) desc,
                        start_at desc limit 4""",
            (baseline, start),
        ).fetchall()
        dates = [row["start_at"] for row in historic]
        for row in db.execute(
            """select e.window_start, e.activity, e.attrs
               from events e join windows w on w.start_at = e.window_start
               where e.window_start = any(%s) and e.activity = any(%s)
                 and (w.observation_snapshot is null or e.ingested_at <= w.observation_snapshot)""",
            ([start, *dates], sorted(activities)),
        ).fetchall():
            windows[row["window_start"]][row["activity"]].append(row["attrs"])
        return dates

    @staticmethod
    def counts(events, step, entry, following):
        completed = {row[step.identity] for row in following[step.outcome] if row.get(step.identity)}
        eligible = {row[step.identity]: row for row in events[entry] if row.get(step.identity)}
        result = defaultdict(lambda: [0, 0])
        for identifier, row in eligible.items():
            for key in scopes(row, step.dimensions):
                result[key][0] += identifier in completed
                result[key][1] += 1
        return result

    def get(self, step, scope):
        entry, current, history = self.data[step.id]
        key = tuple((dim, scope[dim]) for dim in step.dimensions if dim in scope)
        completed, eligible = current.get(key, (0, 0))
        baseline_completed, baseline_eligible = history.get(key, (0, 0))
        expected = baseline_completed / baseline_eligible if baseline_eligible else None
        observed = completed / eligible if eligible else None
        gap = expected - observed if expected is not None and observed is not None else None
        status, strength = "uncertain", 0
        if eligible >= 25 and baseline_eligible >= 75:
            prior = (baseline_completed + 0.5) / (baseline_eligible + 1)
            if eligible * gap >= 4 and binom.cdf(completed, eligible, prior) <= .001:
                status = "degraded"
                strength = gap - 1.96 * sqrt(observed * (1 - observed) / eligible)
            elif gap < .07:
                status = "steady"
        return {
            "id": step.id, "label": step.label, "scope": dict(key), "observed": observed,
            "expected": expected, "completed": completed, "eligible": eligible,
            "baseline_completed": baseline_completed, "baseline_eligible": baseline_eligible,
            "status": status, "strength": strength, "metrics": list(step.metrics), "activities": [entry, step.outcome],
        }

    def degraded(self, step):
        _, current, _ = self.data[step.id]
        readings = (self.get(step, dict(scope)) for scope in current)
        candidates = [row for row in readings if row["status"] == "degraded"]
        return sorted((row for row in candidates if not any(
            shares_degradation(parent, row) for parent in candidates)),
                      key=lambda row: (row["strength"], -len(row["scope"]), row["eligible"]), reverse=True)


def shares_degradation(parent, child):
    if not parent["scope"].items() < child["scope"].items():
        return False
    eligible = parent["eligible"] - child["eligible"]
    baseline_eligible = parent["baseline_eligible"] - child["baseline_eligible"]
    if eligible < 20 or baseline_eligible < 75:
        return False
    completed = parent["completed"] - child["completed"]
    baseline_completed = parent["baseline_completed"] - child["baseline_completed"]
    observed = completed / eligible
    expected = baseline_completed / baseline_eligible
    remainder_gap = expected - observed
    child_gap = child["expected"] - child["observed"]
    # Keep the broader scope when its remaining shoppers show a similar loss.
    variation = sqrt(observed * (1 - observed) / eligible +
                     child["observed"] * (1 - child["observed"]) / child["eligible"])
    prior = (baseline_completed + .5) / (baseline_eligible + 1)
    return (remainder_gap * eligible >= 4 and binom.cdf(completed, eligible, prior) <= .001
            and child_gap - remainder_gap <= max(.04, 1.96 * variation))
