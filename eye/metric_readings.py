"""Catalog readings recomputed from deduplicated facts for a half-open interval."""

from collections import Counter, defaultdict
from datetime import timedelta, timezone
from math import ceil
from zoneinfo import ZoneInfo

from eye.metric_catalog import OUTCOMES
from eye.simulator import SOURCES

BERLIN = ZoneInfo("Europe/Berlin")
MINUTE = timedelta(minutes=1)
MAX_MATURITY = timedelta(minutes=15)
EVENT_COLUMNS = "id, activity, attrs, occurred_at, ingested_at"
OBJECT_ACTIVITY = {
    "session_started": "session", "product_viewed": "session", "cart_added": "session",
    "checkout_started": "session", "order_confirmed": "session", "search_requested": "search",
    "search_result_clicked": "search", "payment_attempted": "payment",
    **dict.fromkeys(OUTCOMES, "payment"),
}


def scopes_for(attrs, dimensions, alert_dimensions):
    fields = {key: attrs[key] for key in dimensions if attrs.get(key) is not None}
    scopes = {(), *((pair,) for pair in fields.items())}
    for keys in (dimensions, alert_dimensions):
        if len(keys) > 1 and all(key in fields for key in keys):
            scopes.add(tuple(sorted((key, fields[key]) for key in keys)))
    return scopes


def matches(attrs, scope):
    return all(attrs.get(name) == value for name, value in scope.items())


class Facts:
    def __init__(self, db, start, end, as_of):
        self.db, self.start, self.end, self.as_of = db, start, end, as_of
        self.windows = db.execute(
            """select * from windows where start_at < %s and end_at > %s
               order by start_at""", (end, start),
        ).fetchall()
        cursor = start
        for window in self.windows:
            if window["start_at"] > cursor:
                break
            cursor = max(cursor, window["end_at"])
        self.covered = cursor >= end
        self.extended = bool(self.windows) and all(w["observation_snapshot"] for w in self.windows)
        self.manifest = {
            (row["source_id"], row["event_type"]): row["expected"]
            for row in db.execute(
                """select source_id, event_type, sum(expected) as expected from source_manifests
                   where minute_at >= %s and minute_at < %s group by source_id, event_type""",
                (start, end),
            ).fetchall()
        }
        self.full_windows = all(w["start_at"] >= start and w["end_at"] <= end for w in self.windows)
        self.expected = sum(w["expected_events"] for w in self.windows)
        self.missing_manifest = (self.extended and self.full_windows and
                                 sum(self.manifest.values()) != self.expected)
        rows = db.execute(
            f"""select {EVENT_COLUMNS} from events
                where occurred_at >= %s and occurred_at < %s and ingested_at <= %s
                order by occurred_at, id""",
            (start, end + MAX_MATURITY, as_of),
        ).fetchall()
        missing_links = [r["id"] for r in rows if (kind := OBJECT_ACTIVITY.get(r["activity"]))
                         and not r["attrs"].get(f"{kind}_id")]
        # Older OCEL rows already have the business identifiers in qualified relations.
        links = defaultdict(dict)
        if missing_links:
            for row in db.execute(
                """select r.event_id, o.type, o.id from event_objects r join objects o on o.id = r.object_id
                   where r.event_id = any(%s) and o.type in ('session', 'search', 'payment', 'order')""",
                (missing_links,),
            ).fetchall():
                links[row["event_id"]][f"{row['type']}_id"] = row["id"]
        self.rows = rows
        self.by_activity = defaultdict(list)
        self.following = defaultdict(list)
        for row in rows:
            attrs, activity = row["attrs"], row["activity"]
            attrs.update({key: value for key, value in links[row["id"]].items() if key not in attrs})
            for canonical, previous in (("payment_provider", "provider"), ("sku_id", "sku")):
                if previous in attrs:
                    attrs.setdefault(canonical, attrs[previous])
            attrs.setdefault("source_id", SOURCES.get(activity, "storefront"))
            attrs.setdefault("event_type", activity)
            self.following[activity].append(row)
            if row["occurred_at"] < end:
                self.by_activity[activity].append(row)
        self.received = Counter((r["attrs"]["source_id"], r["activity"])
                                for rs in self.by_activity.values() for r in rs)
        self._inventory = None
        self._budget = None

    def inventory(self):
        if self._inventory is None:
            self._inventory = self.db.execute(
                f"""select distinct on (coalesce(attrs->>'sku_id', attrs->>'sku')) {EVENT_COLUMNS}
                    from events where activity = 'inventory_snapshot'
                      and occurred_at <= %s and ingested_at <= %s
                    order by coalesce(attrs->>'sku_id', attrs->>'sku'), occurred_at desc, id desc""",
                (self.end, self.as_of),
            ).fetchall()
            for row in self._inventory:
                attrs = row["attrs"]
                attrs.setdefault("sku_id", attrs.get("sku"))
        return [row for row in self._inventory if row["attrs"].get("active", True)]

    def daily(self):
        if self._budget is None:
            day = (self.end - timedelta(microseconds=1)).astimezone(BERLIN)
            midnight = day.replace(hour=0, minute=0, second=0, microsecond=0)
            if self.start < midnight:
                raise ValueError("Daily budget utilization supports one Berlin business day per reading.")
            rows = self.db.execute(
                f"""select {EVENT_COLUMNS} from events
                    where occurred_at >= %s and occurred_at < %s and ingested_at <= %s
                      and activity in ('campaign_budget', 'ad_clicked') order by occurred_at, id""",
                (midnight.astimezone(timezone.utc), self.end, self.as_of),
            ).fetchall()
            budgets, clicks = {}, []
            for row in rows:
                if row["activity"] == "campaign_budget":
                    budgets[row["attrs"]["campaign_id"]] = row
                else:
                    clicks.append(row)
            self._budget = budgets, clicks
        return self._budget

    def linked_clicks(self, activity):
        clicks = self.by_activity[activity]
        ids = [r["attrs"]["impression_id"] for r in clicks if r["attrs"].get("impression_id")]
        if not ids:
            return []
        impressions = {r["id"]: r["occurred_at"] for r in self.db.execute(
            """select id, occurred_at from events where id = any(%s)
                 and activity = 'feed_impression' and ingested_at <= %s""",
            (ids, self.as_of),
        ).fetchall()}
        return [r for r in clicks if (at := impressions.get(r["attrs"].get("impression_id")))
                and at < r["occurred_at"]]


def unique(rows, field):
    if not field:
        return rows
    indexed = {}
    for row in rows:
        indexed.setdefault(row["attrs"].get(field) or row["id"], row)
    return list(indexed.values())


def occurrences(facts, activity, key):
    groups = defaultdict(list)
    for row in facts.following[activity]:
        if identifier := row["attrs"].get(key):
            groups[identifier].append(row["occurred_at"])
    return groups


def within(groups, key, start, duration):
    return any(start <= time <= start + duration for time in groups.get(key, ()))


def contributions(facts, formula):
    """Yield attributes, numerator and denominator population for each contributing fact."""
    method, activity = formula["method"], formula.get("activity")
    rows = facts.by_activity[activity]
    if method in {"cohort", "cohort_ratio"}:
        numerator = occurrences(facts, activity, "session_id")
        denominator = occurrences(facts, formula.get("denominator_activity", "session_started"), "session_id")
        for row in unique(facts.by_activity["session_started"], "session_id"):
            attrs, started = row["attrs"], row["occurred_at"]
            key = attrs.get("session_id", row["id"])
            result = activity == "session_started" or within(numerator, key, started, MAX_MATURITY)
            included = within(denominator, key, started, MAX_MATURITY)
            if method == "cohort":
                yield attrs, int(result), None
            elif included:
                yield attrs, int(result), 1
    elif method in {"search_click", "search_ratio"}:
        clicks = occurrences(facts, "search_result_clicked", "search_id")
        for row in unique(facts.by_activity["search_requested"], "search_id"):
            attrs = row["attrs"]
            clicked = attrs.get("result_count", 0) > 0 and within(
                clicks, attrs.get("search_id", row["id"]), row["occurred_at"], 2 * MINUTE,
            )
            yield attrs, int(clicked), 1 if method == "search_ratio" else None
    elif method in {"payments", "payment_ratio"}:
        terminal = sorted((r for outcome in OUTCOMES for r in facts.by_activity[outcome]),
                          key=lambda r: (r["occurred_at"], r["id"]))
        for row in unique(terminal, "payment_id"):
            outcome = formula.get("outcome")
            yield row["attrs"], int(not outcome or row["activity"] == outcome), 1 if method == "payment_ratio" else None
    elif method in {"inventory_sum", "inventory_ratio"}:
        for row in facts.inventory():
            attrs = row["attrs"]
            units = attrs.get("available_units")
            if units is not None:
                yield attrs, units if method == "inventory_sum" else int(units == 0), 1 if method == "inventory_ratio" else None
    elif method == "budget":
        budgets, clicks = facts.daily()
        for row in budgets.values():
            yield row["attrs"], 0, row["attrs"]["budget_cents"]
        for row in clicks:
            if row["attrs"]["campaign_id"] in budgets:
                yield row["attrs"], row["attrs"]["cost_cents"], 0
    elif method == "contribution":
        for row in unique(facts.by_activity["order_confirmed"], "order_line_id"):
            attrs = row["attrs"]
            yield attrs, attrs["fee_cents"] - attrs["discount_cents"] - attrs["operating_cost_cents"], None
        for row in facts.by_activity["ad_clicked"]:
            attrs = row["attrs"]
            yield {"country": attrs["target_country"], "acquisition_channel": attrs["advertising_channel"]}, -attrs["cost_cents"], None
    else:
        if method == "feed_click":
            rows = facts.linked_clicks(activity)
        for row in unique(rows, formula.get("distinct")):
            attrs = row["attrs"]
            amount = attrs.get(formula["value"]) if "value" in formula else 1
            if amount is not None:
                yield attrs, amount if matches(attrs, formula.get("where", {})) else 0, 1 if method == "ratio" else None


def metric_readings(definition, facts, scope=None):
    formula, access = definition["calculation"], definition["access"]
    method = formula["method"]
    wanted = {} if scope is None else scope
    selected = tuple(sorted(wanted.items()))
    maturity = timedelta(seconds=access["maturity_seconds"])
    base = {
        "unit": access["unit"], "population": access["population"],
        "definition_version": access["definition_version"], "observation_snapshot": facts.as_of.isoformat(),
        "bucket_end": facts.end.isoformat(), "mature": facts.as_of >= facts.end + maturity,
    }
    groups = {selected: [0, 0, 0, []]}
    quality = "complete"

    def keys(attrs):
        if scope is None:
            return scopes_for(attrs, definition["dimensions"], access["alert_dimensions"])
        return (selected,) if matches(attrs, wanted) else ()

    def add(attrs, total=0, denominator=0, count=0, delay=None):
        for key in keys(attrs):
            result = groups.setdefault(key, [0, 0, 0, []])
            result[0] += total
            result[1] += denominator
            result[2] += count
            if delay is not None:
                result[3].append(delay)

    if not facts.covered:
        quality = "missing_window"
    elif not base["mature"]:
        quality = "immature"
    elif formula.get("extended") and not facts.extended:
        quality = "missing_source"
    elif access["source"] == "search_requests" and not facts.extended and not facts.by_activity["search_requested"]:
        quality = "missing_source"
    elif method == "completeness":
        if facts.extended and facts.manifest and not facts.missing_manifest:
            for (source, activity), expected in facts.manifest.items():
                add({"source_id": source, "event_type": activity}, facts.received[(source, activity)], expected, expected)
        elif not facts.extended and not facts.manifest and not wanted and facts.full_windows:
            add({}, sum(facts.received.values()), facts.expected, facts.expected)
        else:
            quality = "missing_manifest"
    elif method == "lag":
        for rows in facts.by_activity.values():
            for row in rows:
                add(row["attrs"], count=1, delay=(row["ingested_at"] - row["occurred_at"]).total_seconds())
    elif (any(facts.received[key] < count for key, count in facts.manifest.items()) or
          (facts.full_windows and sum(facts.received.values()) < facts.expected)):
        quality = "incomplete_data"
    else:
        for attrs, numerator, denominator in contributions(facts, formula):
            add(attrs, numerator, denominator or 0, 1)
        if method.startswith("inventory") and not facts.inventory():
            quality = "missing_source"
        if method == "budget" and not facts.daily()[0]:
            quality = "missing_source"

    pending = Counter()
    if method in {"payments", "payment_ratio"}:
        finalized = {row["attrs"].get("payment_id", row["id"])
                     for activity in OUTCOMES for row in facts.following[activity]}
        for row in facts.by_activity["payment_attempted"]:
            if row["attrs"].get("payment_id", row["id"]) not in finalized:
                pending.update(keys(row["attrs"]))
    ratio = access["unit"] in {"ratio", "EUR cents per click"}
    for key, (numerator, denominator, count, delays) in groups.items():
        details = {**base, "dimensions": dict(key), "quality": quality}
        if method in {"payments", "payment_ratio"}:
            details["pending_attempts"] = pending[key]
            details["pending_attempts_mature"] = not pending[key]
        if ratio:
            details.update(numerator=numerator if quality == "complete" else None,
                           denominator=denominator if quality == "complete" else None)
        if quality != "complete":
            value = None
        elif ratio:
            value = numerator / denominator if denominator else None
            if denominator == 0:
                details["quality"] = "zero_denominator"
        elif method == "lag":
            value = sorted(delays)[ceil(0.95 * len(delays)) - 1] if delays else None
            if not delays:
                details["quality"] = "no_events"
        else:
            value = numerator
        size = denominator if ratio and method != "budget" else count
        yield key, value, size, details
