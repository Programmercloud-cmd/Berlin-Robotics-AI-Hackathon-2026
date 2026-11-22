"""Compare linked, contemporaneous traffic at a measured upstream change."""

from collections import defaultdict
from math import sqrt

from eye.intervals import GRACE, HOUR

PATH = ("product_viewed", "cart_add_requested", "cart_added", "checkout_started",
        "payment_attempted", "payment_succeeded", "order_confirmed")
RATES = {
    "recommendation exposure": ("exposure", None),
    "cart entry": ("cart_add_requested", "product_viewed"),
    "cart completion": ("cart_added", "cart_add_requested"),
    "checkout entry": ("checkout_started", "cart_added"),
    "payment initiation": ("payment_attempted", "checkout_started"),
    "payment success": ("payment_succeeded", "payment_attempted"),
    "order confirmation": ("order_confirmed", "payment_succeeded"),
    "orders per shopper": ("order_confirmed", None),
    "direct orders per shopper": ("direct", None),
    "assisted orders per shopper": ("assisted", None),
}
ANCHORS = {"stock": ("inventory_snapshot", "cart_added", "product"),
           "search": ("search_requested", "feed_impression", "session")}


def graph_path(db, kind):
    source, target, object_type = ANCHORS[kind]
    return db.execute(
        """select a.source, a.target, a.kind, a.provenance from graph_edges a
           where a.kind = 'precedes' and a.source = %s and a.target = %s
           union all
           select a.source, a.target, a.kind, a.provenance from graph_edges a
           where a.kind = 'part_of' and a.target = %s and a.source in (%s, %s)
             and (select count(distinct source) from graph_edges where kind = 'part_of'
                  and target = a.target and source in (%s, %s)) = 2""",
        ("activity:" + source, "activity:" + target, "process:" + object_type,
         "activity:" + source, "activity:" + target, "activity:" + source, "activity:" + target),
    ).fetchall()


def compare(cohorts, rates=RATES):
    """Weight stratum rates on shared support, with separate conditional risk sets."""
    result = {}
    for label, (outcome, eligible) in rates.items():
        counts = defaultdict(lambda: [[0, 0], [0, 0]])
        for stratum, exposed, flags in cohorts:
            if eligible is None or eligible in flags:
                tally = counts[stratum][bool(exposed)]
                tally[0] += outcome in flags
                tally[1] += 1
        weight = current = control = variance = exposed_n = control_n = 0
        for (c, nc), (e, ne) in counts.values():
            if not nc or not ne:
                continue
            w = min(ne, nc)
            a, b = e / ne, c / nc
            current += w * a
            control += w * b
            # Smoothed variances avoid false certainty in all-zero or all-one cells.
            ae, bc = (e + .5) / (ne + 1), (c + .5) / (nc + 1)
            variance += w * w * (ae * (1 - ae) / ne + bc * (1 - bc) / nc)
            weight += w
            exposed_n += ne
            control_n += nc
        gap = (current - control) / weight if weight else None
        margin = 1.64 * sqrt(variance) / weight if weight else None
        status = "insufficient"
        if min(exposed_n, control_n) >= 12:
            status = "lower" if gap < -max(.06, margin) else "higher" if gap > max(.06, margin) else "inconclusive"
            if abs(gap) + margin <= .12:
                status = "steady"
        result[label] = {"exposed": current / weight if weight else None,
                         "control": control / weight if weight else None, "gap": gap,
                         "margin": margin, "matched_exposed": exposed_n, "matched_control": control_n,
                         "status": status}
    return result


def matched_cohorts(rows, kind, start, end, scope):
    sessions, stocks = defaultdict(list), defaultdict(list)
    for row in rows:
        attrs = row["attrs"]
        if row["activity"] == "inventory_snapshot":
            stocks[attrs.get("sku_id") or attrs.get("sku")].append(row)
        elif session := attrs.get("session_id"):
            sessions[session].append(row)
    anchor = "cart_added" if kind == "stock" else "search_requested"
    cohorts, samples, signals, segments = [], [], [], set()
    for events in sessions.values():
        entry = next((row for row in events if row["activity"] == anchor and start <= row["occurred_at"] < end), None)
        if not entry:
            continue
        attrs, at = entry["attrs"], entry["occurred_at"]
        if any(attrs.get(name) != value for name, value in scope.items()):
            continue
        if kind == "stock":
            sku = attrs.get("sku_id") or attrs.get("sku")
            snapshots = [row for row in stocks[sku] if row["occurred_at"] <= at]
            if not snapshots:
                continue
            upstream = snapshots[-1]
            units = upstream["attrs"].get("available_units")
            if units is None:
                continue
            exposed = units == 0
            changed = exposed and any(row["attrs"].get("available_units", 0) > 0 for row in snapshots[:-1])
            stratum = (attrs.get("country"), attrs.get("platform"), attrs.get("category"), int(at.timestamp()) // 900)
        else:
            upstream = entry
            arm = attrs.get("experiment_arm")
            if arm not in {"rollout", "holdout"} or not isinstance(attrs.get("response_ms"), (int, float)):
                continue
            exposed = arm == "rollout"
            changed = exposed
            stratum = (attrs.get("country"), attrs.get("platform"), int(at.timestamp()) // 600)
        following = [row for row in events if at <= row["occurred_at"] <= at + GRACE]
        flags = {row["activity"] for row in following if row["activity"] in PATH}
        proposals = {row["id"]: row for row in following if row["activity"] == "feed_impression"}
        order = next((row for row in following if row["activity"] == "order_confirmed"), None)
        if any(order is None or row["occurred_at"] < order["occurred_at"] for row in proposals.values()):
            flags.add("exposure")
        if order:
            order_attrs = order["attrs"]
            impression = proposals.get(order_attrs.get("recommendation_impression_id"))
            if order_attrs.get("order_path") == "direct":
                flags.add("direct")
            elif impression and impression["occurred_at"] < order["occurred_at"]:
                flags.add("assisted")
        cohorts.append((stratum, exposed, flags))
        if changed:
            signals.append(upstream["occurred_at"])
            dimensions = ("country", "platform", "category") if kind == "stock" else ("country", "platform")
            segments.add(tuple((dim, attrs[dim]) for dim in dimensions if attrs.get(dim) is not None))
            if len(samples) < 3:
                samples.append({"upstream": upstream, "eligible": entry, "downstream": sorted(flags)})
    return {"rates": compare(cohorts), "preceded": len(signals), "first_change": min(signals, default=None),
            "segments": [dict(segment) for segment in sorted(segments)], "samples": samples}


def read_cohorts(db, kind, start, end, cutoff, scope, facts=None):
    activities = [*PATH, "search_requested", "feed_impression"]
    if facts:
        rows = [row for activity in activities for row in facts.following[activity]]
    else:
        rows = db.execute(
            """select id, activity, attrs, occurred_at from events where occurred_at >= %s
               and occurred_at < %s and ingested_at <= %s and activity = any(%s)
               order by occurred_at, id""", (start, end + GRACE, cutoff, activities),
        ).fetchall()
    if kind == "stock":
        rows.extend(db.execute(
            """select id, activity, attrs, occurred_at from events where activity = 'inventory_snapshot'
               and occurred_at >= %s and occurred_at < %s and ingested_at <= %s""",
            (start - 2 * HOUR, end, cutoff),
        ).fetchall())
    rows.sort(key=lambda row: (row["occurred_at"], row["id"]))
    return matched_cohorts(rows, kind, start, end, scope)


def supports_link(kind, evidence):
    rates = evidence["rates"]
    targets = ("cart entry", "recommendation exposure") if kind == "search" else ("checkout entry",)
    return evidence["preceded"] > 0 and all(rates[target]["status"] == "lower" for target in targets)
