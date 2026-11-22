"""Build evidence branches from cohort-aligned telemetry."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median

from psycopg.types.json import Jsonb

from eye.cohort_evidence import graph_path, read_cohorts, supports_link
from eye.intervals import GRACE, HOUR, aligned_history
PAYMENT_EVENTS = ("payment_attempted", "payment_succeeded", "payment_timed_out", "payment_declined")
PAYMENT_CONTEXT = (*PAYMENT_EVENTS, "order_confirmed")
SALES_EVENTS = ("search_requested", "feed_impression", "order_confirmed", "payment_attempted", "payment_succeeded")
PAYMENT_SIGNALS = {"payment_attempts", "successful_payments", "payment_success_rate", "payment_timeout_rate", "payment_timeouts",
                   "other_failed_payments", "data_completeness_ratio", "event_ingestion_lag_seconds"}
SALES_SIGNALS = {"orders", "session_conversion_rate", "checkout_completion_rate", "feed_impressions", "feed_clicks",
                 "search_click_rate", "product_view_sessions", "add_to_cart_sessions", "checkout_sessions", "sessions"}
SHOPPER_SCOPE = ("country", "platform", "acquisition_channel")


def hypothesis(title, supports=(), weakens=(), unresolved=(), *, evidence=None):
    branch = {"title": title, "supports": list(supports), "weakens": list(weakens), "unresolved": list(unresolved)}
    return branch, evidence or {}


def traffic_scope(scope, fields=SHOPPER_SCOPE):
    return {key: scope[key] for key in fields if key in scope}


def read_events(db, windows, activities, scope, cutoff=None, *, rolling=False, follow_payment=False):
    if rolling:
        return db.execute(
            """select e.id, e.activity, p.start_at as window_start, e.occurred_at, e.ingested_at, e.attrs
               from unnest(%s::timestamptz[]) p(start_at) join events e
                 on e.occurred_at >= p.start_at and e.occurred_at < p.start_at + %s
                and e.ingested_at <= coalesce(%s, p.start_at + %s)
               where e.activity = any(%s) and e.attrs @> %s
                 and (e.activity <> 'payment_attempted' or e.occurred_at < p.start_at + %s)
               order by e.occurred_at, e.id""",
            (windows, HOUR + GRACE if follow_payment else HOUR, cutoff, HOUR + GRACE,
             list(activities), Jsonb(scope), HOUR),
        ).fetchall()
    return db.execute(
        """select e.id, e.activity, e.window_start, e.occurred_at, e.ingested_at, e.attrs
           from events e join windows w on w.start_at = e.window_start
           where e.window_start = any(%s) and e.activity = any(%s) and e.attrs @> %s
             and e.ingested_at <= coalesce(%s, w.observation_snapshot, w.end_at)
           order by e.occurred_at, e.id""",
        (windows, list(activities), Jsonb(scope), cutoff),
    ).fetchall()


def baselines(db, start, through):
    return [row["start_at"] for row in db.execute(
        """select start_at from windows where start_at <= %s
           order by (extract(hour from start_at) = extract(hour from %s::timestamptz)) desc,
                    start_at desc limit 4""",
        (through, start),
    ).fetchall()]


def payment_cohort(rows, start=None, at_detection=None):
    attempts = {}
    outcomes = defaultdict(list)
    confirmations = defaultdict(list)
    for row in rows:
        identity = row["attrs"].get("payment_id")
        if not identity:
            continue
        if row["activity"] == "payment_attempted":
            if start is None or row["window_start"] == start:
                attempts.setdefault(identity, row)
        elif row["activity"] == "order_confirmed":
            confirmations[identity].append(row)
        else:
            outcomes[identity].append(row)
    counts = Counter()
    samples = {}
    for identity, attempt in attempts.items():
        received = [row for row in outcomes[identity] if row["occurred_at"] >= attempt["occurred_at"]]
        visible = [row for row in received if at_detection is None or row["ingested_at"] <= at_detection]
        success_visible = any(row["activity"] == "payment_succeeded" for row in visible)
        timely_orders = [row for row in confirmations[identity]
                         if row["occurred_at"] >= attempt["occurred_at"]
                         and (at_detection is None or row["ingested_at"] <= at_detection)]
        timeouts = [row for row in visible if row["activity"] == "payment_timed_out"]
        gateways = [row for row in timeouts if row["attrs"].get("response") == "gateway_timeout"]
        late = [row for row in received if not success_visible and row["activity"] == "payment_succeeded" and at_detection is not None
                and row["occurred_at"] <= at_detection < row["ingested_at"]]
        late_authorizations = [row for row in received if row["activity"] == "payment_succeeded"
                               and at_detection is not None and row["occurred_at"] > at_detection]
        counts["attempts"] += 1
        counts["initial_timeouts"] += bool(timeouts)
        counts["initial_gateway_timeouts"] += bool(gateways)
        counts["initial_missing"] += not visible
        counts["orders_while_missing"] += bool(timely_orders) and not visible
        counts["missing_now"] += not received
        counts["successes"] += any(row["activity"] == "payment_succeeded" for row in received)
        counts["initial_declines"] += any(row["activity"] == "payment_declined" for row in visible)
        counts["late_successes"] += bool(late)
        counts["late_authorizations"] += bool(late_authorizations)
        succeeded = any(row["activity"] == "payment_succeeded" for row in received)
        counts["timeouts_without_success"] += bool(timeouts) and not succeeded
        counts["gateways_without_success"] += bool(gateways) and not succeeded
        counts["timed_out_then_succeeded"] += bool(timeouts and late)
        if timeouts and "timeout" not in samples:
            samples["timeout"] = [attempt, (gateways or timeouts)[0]]
        if late and "delayed_receipt" not in samples:
            samples["delayed_receipt"] = [attempt, late[0]]
        if timely_orders and not visible and "order_while_missing" not in samples:
            samples["order_while_missing"] = [attempt, timely_orders[0]]
        if not received and "missing" not in samples:
            samples["missing"] = [attempt]
    return counts, samples


def payment_branches(db, start, scope, at_detection, cutoff, history, *, rolling=False):
    scope = traffic_scope(scope, (*SHOPPER_SCOPE, "payment_provider", "checkout_release_version"))
    windows = [start] if rolling else [row["start_at"] for row in db.execute(
        "select start_at from windows where start_at >= %s and start_at <= %s order by start_at", (start, cutoff)
    ).fetchall()]
    rows = read_events(db, windows, PAYMENT_CONTEXT, scope, cutoff, rolling=rolling, follow_payment=True)
    # Gateway scope only exists on payment telemetry, so infer the affected cohort
    # from receipts when a broader data-completeness alert was the first signal.
    if not scope:
        strata = defaultdict(list)
        for row in rows:
            segment = traffic_scope(row["attrs"], ("country", "platform", "payment_provider"))
            strata[tuple(segment.items())].append(row)
        candidates = [(payment_cohort(segment, start, at_detection)[0], dict(key))
                      for key, segment in strata.items() if key]
        ranked = sorted(candidates, key=lambda item: item[0]["initial_missing"] + item[0]["initial_timeouts"],
                        reverse=True)
        if ranked and (counts := ranked[0][0])["attempts"] >= 10:
            impact = counts["initial_missing"] + counts["initial_timeouts"]
            other_attempts = sum(count["attempts"] for count, _ in ranked[1:])
            other_impact = sum(count["initial_missing"] + count["initial_timeouts"] for count, _ in ranked[1:])
            other_rate = other_impact / other_attempts if other_attempts else 0
            if impact >= 3 and impact / counts["attempts"] >= max(.10 + other_rate, 2 * other_rate):
                scope = ranked[0][1]
                rows = strata[tuple(scope.items())]
    counts, samples = payment_cohort(rows, start, at_detection)
    if not counts["attempts"]:
        return [], False
    historic, _ = payment_cohort(read_events(db, history, PAYMENT_CONTEXT, scope, rolling=rolling, follow_payment=True))
    total = counts["attempts"]
    timeout = counts["initial_timeouts"]
    gateway = counts["initial_gateway_timeouts"]
    late = counts["late_successes"]
    missing = counts["missing_now"]
    expected = counts["initial_missing"]
    baseline_attempts = historic["attempts"]
    baseline_timeouts = historic["initial_gateway_timeouts"]
    elevated = gateway > 0 and (not baseline_attempts or gateway / total > baseline_timeouts / baseline_attempts + .03)
    segment = ", ".join(str(value) for value in scope.values()) or "marketplace-wide traffic"
    gateway_support = []
    gateway_weaken = []
    if elevated:
        gateway_support.append(f"{gateway} of {total} linked attempts in {segment} had an explicit gateway timeout at the observation cutoff.")
        if baseline_attempts:
            gateway_support.append(f"Matched baseline windows recorded {baseline_timeouts} timeouts in {baseline_attempts} attempts.")
        if counts["gateways_without_success"]:
            gateway_support.append(f"{counts['gateways_without_success']} gateway timeouts still have no linked success.")
    elif gateway:
        gateway_weaken.append(f"Only {gateway} of {total} attempts in {segment} had a gateway timeout, in line with matched baseline windows.")
    else:
        gateway_weaken.append(f"No explicit gateway timeout was observed for the {total} linked attempts.")
    if counts["timed_out_then_succeeded"]:
        gateway_weaken.append(f"{counts['timed_out_then_succeeded']} timed-out attempts also received a success later.")
    if late:
        gateway_weaken.append(f"{late} attempts had successful outcomes that were recorded before the cutoff and ingested afterward.")
    uncertainty = [f"{missing} attempts still have no observed terminal outcome."] if missing else []
    if timeout > gateway:
        uncertainty.append(f"{timeout - gateway} timed-out attempts lacked a recorded gateway response code.")
    if elevated:
        uncertainty.append("Gateway timeout events identify the observed outcome. The underlying network or provider cause is unverified.")

    delay_support = []
    delay_weaken = []
    if expected:
        delay_support.append(f"{expected} of {total} attempts in {segment} had no terminal event at the original observation cutoff.")
    if counts["orders_while_missing"]:
        timely = f"{counts['orders_while_missing']} of those attempts already had a linked order confirmation at that cutoff."
        delay_support.append(timely)
        gateway_weaken.append(timely)
    if late:
        delay_support.append(f"{late} received a success for the same payment ID after the cutoff, with the success occurring before it.")
        pair = samples["delayed_receipt"][1]
        delay = (pair["ingested_at"] - pair["occurred_at"]).total_seconds() / 60
        delay_support.append(f"A sampled linked receipt took {delay:.0f} minutes from occurrence to ingestion.")
    elif not missing:
        delay_weaken.append("No successful event for these attempts arrived after the original cutoff.")
    if timeout:
        delay_weaken.append(f"{counts['timeouts_without_success']} attempts recorded explicit timeouts and still have no linked success.")
    if counts["late_authorizations"]:
        delay_weaken.append(f"{counts['late_authorizations']} successes occurred after the cutoff, so late ingestion does not establish their timing.")
    delay_unresolved = [f"The outcomes of {missing} attempts are still missing."] if missing else []
    if late:
        delay_unresolved.append("The events establish ingestion delay. The upstream delivery component responsible is unverified.")
    evidence = {"scope": scope, "at_detection": at_detection, "observed_through": cutoff,
                "attempts": dict(counts), "baseline": dict(historic), "matched_events": samples}
    branches = [
        hypothesis("Gateway timeouts", gateway_support, gateway_weaken, uncertainty, evidence=evidence),
        hypothesis("Delayed success events", delay_support, delay_weaken, delay_unresolved, evidence=evidence),
    ]
    comparison = historic["initial_declines"] / baseline_attempts if baseline_attempts else 0
    if counts["initial_declines"] / total > comparison + .03:
        branches.append(hypothesis("Payment declines",
                                   [f"{counts['initial_declines']} of {total} linked attempts had a decline at the cutoff."],
                                   unresolved=["Decline events alone do not establish why attempts were rejected."],
                                   evidence=evidence))
    return branches, bool(missing)


def search_cohort(rows):
    group = defaultdict(list)
    for row in rows:
        if session := row["attrs"].get("session_id"):
            group[session].append(row)
    cohorts = {}
    for events in group.values():
        search = next((row for row in events if row["activity"] == "search_requested"
                       and isinstance(row["attrs"].get("response_ms"), (int, float))), None)
        if search is None:
            continue
        arm = search["attrs"].get("experiment_arm")
        version = search["attrs"].get("search_release")
        if arm not in {"rollout", "holdout"} or not version:
            continue
        cohort = cohorts.setdefault(arm, {"sessions": 0, "exposed": 0, "before_response": 0, "direct_orders": 0,
                                         "recommendation_orders": 0, "unclassified_orders": 0, "orders": 0,
                                         "payment_attempts": 0, "payment_successes": 0, "latencies": [],
                                         "releases": set(), "samples": []})
        cohort["sessions"] += 1
        cohort["releases"].add(version)
        response_ms = search["attrs"]["response_ms"]
        cohort["latencies"].append(response_ms)
        search_start = search["occurred_at"]
        responded = search_start + timedelta(milliseconds=response_ms)
        impressions = {row["id"]: row for row in events if row["activity"] == "feed_impression"
                       and search_start <= row["occurred_at"]}
        orders = {}
        for row in events:
            if row["activity"] == "order_confirmed" and row["occurred_at"] >= search_start:
                orders.setdefault(row["attrs"].get("order_id") or row["id"], row)
        first_order = min((row["occurred_at"] for row in orders.values()), default=None)
        eligible_impressions = [row for row in impressions.values()
                                if first_order is None or row["occurred_at"] < first_order]
        cohort["exposed"] += bool(eligible_impressions)
        cohort["before_response"] += any(row["occurred_at"] < responded for row in eligible_impressions)
        cohort["orders"] += len(orders)
        sample = [search]
        for order in orders.values():
            attrs = order["attrs"]
            linked = impressions.get(attrs.get("recommendation_impression_id"))
            if attrs.get("order_path") == "recommendation" and linked and linked["occurred_at"] < order["occurred_at"]:
                cohort["recommendation_orders"] += 1
                sample.extend((linked, order))
            elif attrs.get("order_path") == "direct":
                cohort["direct_orders"] += 1
                sample.append(order)
            else:
                cohort["unclassified_orders"] += 1
        attempts = {row["attrs"].get("payment_id") for row in events if row["activity"] == "payment_attempted"
                    and row["occurred_at"] >= search_start} - {None}
        successes = {row["attrs"].get("payment_id") for row in events if row["activity"] == "payment_succeeded"
                     and row["occurred_at"] >= search_start} - {None}
        cohort["payment_attempts"] += len(attempts)
        cohort["payment_successes"] += len(attempts & successes)
        if len(cohort["samples"]) < 4 and (orders or eligible_impressions):
            cohort["samples"].append(sample if orders else [search, eligible_impressions[0]])
    for cohort in cohorts.values():
        cohort["median_response_ms"] = median(cohort.pop("latencies"))
        cohort["releases"] = sorted(cohort["releases"])
    return cohorts


def sales_branches(db, start, scope, at_detection, history, *, rolling=False):
    scope = traffic_scope(scope)
    cohorts = search_cohort(read_events(db, [start], SALES_EVENTS, scope, at_detection, rolling=rolling))
    if not cohorts:
        return []
    rollout, holdout = cohorts.get("rollout"), cohorts.get("holdout")
    if not rollout or not holdout:
        if rollout:
            return [hypothesis("Search rollout and recommendation exposure",
                               [f"Search release {', '.join(rollout['releases'])} served {rollout['sessions']} measured sessions."],
                               unresolved=["No contemporaneous holdout was observed for this traffic cohort."],
                               evidence={"scope": scope, "cohorts": cohorts})]
        return []
    old = search_cohort(read_events(db, history, SALES_EVENTS, scope, rolling=rolling)).get("holdout")
    faster = rollout["median_response_ms"] < holdout["median_response_ms"]
    different_release = rollout["releases"] != holdout["releases"]
    exposed = rollout["exposed"] / rollout["sessions"]
    control_exposed = holdout["exposed"] / holdout["sessions"]
    observed_lower = exposed < control_exposed
    supports, weakens, unresolved = [], [], []
    speed = (f"Median search response was {rollout['median_response_ms']:.0f} ms in the rollout "
             f"and {holdout['median_response_ms']:.0f} ms in the contemporaneous holdout.")
    (supports if faster else weakens).append(speed)
    rate = (f"After search began and before any order, {exposed:.1%} of rollout sessions saw a recommendation "
            f"versus {control_exposed:.1%} in the holdout.")
    (supports if observed_lower else weakens).append(rate)
    before = rollout["before_response"] / rollout["sessions"]
    control_before = holdout["before_response"] / holdout["sessions"]
    timing = f"A recommendation appeared before search responded in {before:.1%} of rollout sessions versus {control_before:.1%} in holdout."
    (supports if before < control_before else weakens).append(timing)
    if different_release:
        supports.append(f"Observed releases were {', '.join(rollout['releases'])} in rollout and {', '.join(holdout['releases'])} in holdout.")
    else:
        weakens.append("Both arms reported the same search release.")
    if old:
        old_exposed = old["exposed"] / old["sessions"]
        if abs(old_exposed - control_exposed) <= .10:
            supports.append(f"Earlier matching holdout exposure was {old_exposed:.1%}, close to the contemporaneous holdout.")
        else:
            weakens.append(f"Earlier matching holdout exposure was {old_exposed:.1%}, showing another exposure change outside rollout.")
    if min(rollout["sessions"], holdout["sessions"]) < 30:
        unresolved.append("At least one experiment arm has fewer than 30 measured search sessions.")
    unresolved.append("The release and timing evidence support the exposure mechanism. Other changes bundled with the release remain possible.")

    def delta(field):
        return 100 * (rollout[field] / rollout["sessions"] - holdout[field] / holdout["sessions"])

    direct, assisted, net = (delta(field) for field in ("direct_orders", "recommendation_orders", "orders"))
    impact = [f"Per 100 searching sessions, direct orders changed by {direct:+.1f} and linked recommendation-assisted orders by {assisted:+.1f}.",
              f"Total orders changed by {net:+.1f} per 100 searching sessions."]
    impact_weakens = []
    if assisted < 0:
        if direct + assisted >= 0:
            impact.append("The direct-order gain offset the loss of linked recommendation-assisted orders.")
        elif direct > 0:
            impact_weakens.append("Direct-order gains only partly offset the loss of linked recommendation-assisted orders.")
        else:
            impact_weakens.append("Direct orders did not offset the loss of linked recommendation-assisted orders.")
    else:
        impact_weakens.append("Linked recommendation-assisted orders did not fall relative to the contemporaneous holdout.")
    attribution_gaps = rollout["unclassified_orders"] + holdout["unclassified_orders"]
    impact_unresolved = ([f"{attribution_gaps} orders lack a verified path and only contribute to total orders."]
                         if attribution_gaps else [])
    impact_unresolved.append("Revenue and profit remain unmeasured in this order-count comparison.")

    alternative_support, alternative_weaken = [], []
    if old:
        reference = old["exposed"] / old["sessions"]
        if control_exposed < reference - .10:
            alternative_support.append("Recommendation exposure also declined in holdout, consistent with a broader delivery change.")
        else:
            alternative_weaken.append("The holdout retained its usual recommendation exposure, weakening a shared delivery disruption.")
    else:
        alternative_weaken.append("Recommendation exposures remained visible in the contemporaneous holdout.")
    payment_support, payment_weaken = [], []
    payment_unresolved = ["Sales outside this matched search cohort remain unexplained by this comparison."]
    if rollout["payment_attempts"] and holdout["payment_attempts"]:
        roll_rate = rollout["payment_successes"] / rollout["payment_attempts"]
        hold_rate = holdout["payment_successes"] / holdout["payment_attempts"]
        message = f"Linked payment success was {roll_rate:.1%} in rollout and {hold_rate:.1%} in holdout."
        (payment_support if roll_rate < hold_rate - .10 else payment_weaken).append(message)
    else:
        payment_unresolved.append("At least one arm has no linked payment attempts.")
    evidence = {"scope": scope, "cohorts": cohorts, "prior_holdout": old,
                "orders_per_100_delta": {"direct": direct, "recommendation": assisted, "total": net}}
    return [
        hypothesis("Faster search rollout reduced recommendation exposure", supports, weakens, unresolved, evidence=evidence),
        hypothesis("Direct purchases offset recommendation-assisted losses", impact, impact_weakens, impact_unresolved,
                   evidence=evidence),
        hypothesis("A shared recommendation delivery disruption", alternative_support, alternative_weaken,
                   ["Delivery logs and stock telemetry would help check causes that affect only the rollout."], evidence=evidence),
        hypothesis("A downstream payment problem", payment_support, payment_weaken, payment_unresolved, evidence=evidence),
    ]


def upstream_branches(db, incident, start, at_detection, history, *, rolling=False):
    """Check discovered predecessors against session-cohort observations."""
    scope = traffic_scope(incident["scope"])
    transition = incident["report"].get("break", {})
    roots = ["activity:" + activity for activity in transition.get("activities", [])[:1]]
    if not roots:
        roots = [row["target"] for row in db.execute(
            "select target from graph_edges where source = %s and kind = 'measures' and target like 'activity:%%'",
            (f"metric:{incident['metric_id']}",),
        ).fetchall()]
    if not roots:
        return []
    upstream = db.execute(
        """with recursive earlier(node, depth, path) as (
            select unnest(%s::text[]), 0, %s::text[]
            union all
            select g.source, e.depth + 1, e.path || g.source
            from earlier e join graph_edges g on g.target = e.node and g.kind = 'precedes'
            where e.depth < 3 and not g.source = any(e.path)
        )
        select substring(node from 10) as activity, min(depth) as depth
        from earlier where depth > 0 and node like 'activity:%%'
        group by node order by depth, activity limit 12""",
        (roots, roots),
    ).fetchall()
    activities = [row["activity"] for row in upstream if row["activity"] != "session_started"]
    if not activities:
        return []
    anchors = [node.removeprefix("activity:") for node in roots]

    def summarize(rows):
        sessions = {row["attrs"].get("session_id"): row["occurred_at"] for row in rows
                    if row["activity"] == "session_started" and row["attrs"].get("session_id")}
        reached = {}
        for row in rows:
            if row["activity"] in anchors:
                reached.setdefault(row["attrs"].get("session_id"), row["occurred_at"])
        earlier, samples = defaultdict(set), defaultdict(list)
        for row in rows:
            session, activity = row["attrs"].get("session_id"), row["activity"]
            at = row["occurred_at"]
            if activity in activities and session in sessions and sessions[session] <= at <= reached.get(session, at):
                earlier[activity].add(session)
                if len(samples[activity]) < 2:
                    samples[activity].append(row)
        return len(sessions), {key: len(value) for key, value in earlier.items()}, samples

    wanted = ["session_started", *anchors, *activities]
    total, current, samples = summarize(read_events(db, [start], wanted, scope, at_detection, rolling=rolling))
    historic, prior, _ = summarize(read_events(db, history, wanted, scope, rolling=rolling))
    if total < 20 or historic < 50:
        return [hypothesis("Earlier workflow disruption", unresolved=[
            "Too few linked sessions to compare the detected stage with its discovered predecessors."
        ], evidence={"scope": scope, "sessions": total, "baseline_sessions": historic})]
    changes = [(prior.get(activity, 0) / historic - current.get(activity, 0) / total, activity)
               for activity in activities]
    results = []
    for gap, activity in sorted(changes, key=lambda change: abs(change[0]), reverse=True)[:3]:
        if abs(gap) < .10:
            break
        now, baseline = current.get(activity, 0) / total, prior.get(activity, 0) / historic
        label = activity.replace("_", " ")
        results.append(hypothesis(f"Earlier change in {label}",
                                  [f"The discovered workflow connects {label} to the measured stage.",
                                   f"{now:.1%} of matched sessions had this earlier activity versus {baseline:.1%} in baseline windows."],
                                  unresolved=["The observed sequence supports an upstream connection. A common cause remains possible."],
                                  evidence={"scope": scope, "activity": activity, "observed": now, "expected": baseline,
                                            "samples": samples[activity]}))
    if results:
        return results
    return [hypothesis("Earlier workflow disruption",
                       weakens=["The sampled predecessor activities showed no comparable shift across matched sessions."],
                       unresolved=["Uninstrumented changes upstream of the first measured break remain possible."],
                       evidence={"scope": scope, "sessions": total, "baseline_sessions": historic,
                                 "current": current, "baseline": prior})]


def receipt_branch(db, scope, start, cutoff, event_types):
    source = scope["source_id"]
    rows = db.execute(
        """with expected as (
               select event_type, sum(expected) as n from source_manifests where source_id=%s
               and minute_at >= %s and minute_at < %s group by event_type
           ), received as (
               select activity, count(*) as n from events where occurred_at >= %s and occurred_at < %s
               and ingested_at <= %s and attrs->>'source_id'=%s group by activity
           ) select event_type, expected.n as expected, coalesce(received.n,0) as received
             from expected left join received on activity=event_type where event_type=any(%s)
             order by event_type""",
        (source, start, start + HOUR, start, start + HOUR, cutoff, source, event_types),
    ).fetchall()
    gaps = [row for row in rows if row["received"] < row["expected"]]
    return hypothesis("Shared source receipt loss",
                      [f"{row['event_type']}: {row['received']} of {row['expected']} expected receipts from {source}."
                       for row in gaps],
                      unresolved=["Missing telemetry cannot distinguish missing delivery from business outcomes.",
                                  "The shared source supports grouping. The precise delivery failure mechanism is unverified."],
                      evidence={"scope": scope, "manifest_comparison": rows, "cutoff": cutoff})


def matched_branch(db, incident, start, cutoff, kind):
    path = graph_path(db, kind)
    if not path:
        return None
    limit = start + HOUR + timedelta(minutes=30)
    end = min(limit, max(start + HOUR, cutoff - GRACE))
    scope = incident["scope"]
    evidence = read_cohorts(db, kind, start, end, cutoff, scope)
    if not evidence["preceded"] and kind == "search":
        return None
    shared = incident["report"].get("shared_explanation", {})
    checks = shared.get("completion_checks", [])
    evidence.update(scope=scope, graph_path=path, end=end, cutoff=cutoff,
                    completion_checks=checks, revisit=end < limit)
    supported, weakened, unresolved = [], [], []
    if evidence["first_change"]:
        at = evidence["first_change"].strftime("%H:%M:%S UTC")
        prefix = "A SKU changed from positive stock to zero before it was added to matching carts" if kind == "stock" else \
            "Search rollout requests preceded recommendation exposure and ordering opportunities"
        supported.append(f"{prefix}. The earliest linked upstream reading was at {at}.")
    targets = ("checkout entry",) if kind == "stock" else ("recommendation exposure", "cart entry", "orders per shopper")
    for target in targets:
        rate = evidence["rates"][target]
        if rate["status"] == "insufficient":
            unresolved.append(f"Too few exposed and comparable unexposed shoppers to resolve {target}.")
            continue
        caption = (f"Matched {target} was {rate['exposed']:.1%} in exposed traffic and {rate['control']:.1%} "
                   f"in controls ({rate['matched_exposed']} and {rate['matched_control']} eligible).")
        destination = supported if rate["status"] == "lower" else unresolved if rate["status"] == "inconclusive" else weakened
        destination.append(caption)
    if kind == "search":
        rollout, holdout = evidence["rollout"], evidence["holdout"]
        if rollout["median_ms"] is not None and holdout["median_ms"] is not None:
            caption = (f"Matched rollout ({', '.join(rollout['releases']) or 'unrecorded release'}) had median response "
                       f"{rollout['median_ms']:.0f} ms versus {holdout['median_ms']:.0f} ms in the holdout "
                       f"({', '.join(holdout['releases']) or 'unrecorded release'}).")
            (supported if rollout["median_ms"] < holdout["median_ms"] else weakened).append(caption)
        before = evidence["rates"]["exposure before response"]
        if before["status"] == "lower":
            supported.append(f"Recommendation exposure before the response fell from {before['control']:.1%} in "
                             f"holdout to {before['exposed']:.1%} in rollout.")
        direct = evidence["rates"]["direct orders per shopper"]
        assisted = evidence["rates"]["assisted orders per shopper"]
        if direct["gap"] is not None and assisted["gap"] is not None:
            supported.append(f"Per 100 matched searchers, direct orders changed by {100 * direct['gap']:+.1f} "
                             f"and linked assisted orders by {100 * assisted['gap']:+.1f}.")
            if assisted["gap"] < 0 and direct["gap"] + assisted["gap"] < 0:
                supported.append("Direct purchases did not fully offset the linked assisted-order loss.")
    paid = evidence["rates"]["payment success"]
    if paid["status"] in {"steady", "lower", "higher"}:
        caption = f"Conditional payment success was {paid['exposed']:.1%} versus {paid['control']:.1%}."
        (weakened if paid["status"] == "lower" else supported).append(caption)
    elif steady := [row for row in checks if row["id"] in {"payment_attempt", "payment", "order"} and row["status"] == "steady"]:
        supported.append("Among shoppers who reached them, these rates stayed near their own historical baselines: "
                         + ", ".join(row["label"] for row in steady) + ".")
    else:
        unresolved.append("The linked samples do not establish whether downstream conditional success held.")
    unresolved.append("Exposure is matched by customer segment and time. Other concurrent changes remain possible.")
    evidence["status"] = "supported" if supports_link(kind, evidence) else "provisional"
    title = "Availability preceded a checkout loss" if kind == "stock" else "Matched rollout, recommendation exposure, and sales"
    return hypothesis(title, supported, weakened, unresolved, evidence=evidence)


def build_branches(db, incident, cutoff, baseline):
    start = incident["opened_at"]
    window = incident["report"].get("observation_window")
    at_detection = datetime.fromisoformat(window["observed_at"]) if window else db.execute(
        "select coalesce(observation_snapshot, end_at) as cutoff from windows where start_at = %s", (start,),
    ).fetchone()["cutoff"]
    cutoff = max(cutoff or at_detection, at_detection)
    history = aligned_history(start, baseline) if window else baselines(db, start, baseline)
    metric = incident["metric_id"]
    branches, watch_receipts = [], False
    scope = incident["scope"]
    event_type = scope.get("event_type")
    shared = incident["report"].get("shared_explanation", {})
    kind = shared.get("kind")
    if kind == "source":
        branches.append(receipt_branch(db, scope, start, at_detection, shared["event_types"]))
    if metric in PAYMENT_SIGNALS and scope.get("source_id") in {None, "payments"} and (not event_type or event_type in PAYMENT_EVENTS):
        branches, watch_receipts = payment_branches(db, start, incident["scope"], at_detection, cutoff, history,
                                                   rolling=bool(window))
    if window and kind in {"stock", "search"}:
        if branch := matched_branch(db, incident, start, cutoff, kind):
            branches.append(branch)
        for row in db.execute(
            """select decision from incident_relationships where parent_id=%s and observed_at <= %s
               order by incident_id""", (incident["id"], cutoff),
        ).fetchall():
            decision = row["decision"]
            caption = decision["reason"]
            status = decision["status"]
            supports = [caption] if status == "linked" else []
            if "upstream_explained" in decision:
                supports.append(f"Linked cohorts account for an estimated {decision['upstream_explained']:.1f} of "
                                f"{decision['scope_volume_loss']:.1f} missing {decision['metric_id'].replace('_', ' ')}.")
                supports.append(f"Scoped completion was {decision['checked_completion_rate']:.1%} versus "
                                f"{decision['baseline_completion_rate']:.1%} expected."
                                if decision["checked_completion_rate"] is not None else "The comparison rate was unavailable.")
            branches.append(hypothesis(f"Downstream incident #{decision['incident_id']}", supports,
                                       [caption] if status == "independent" else [],
                                       [caption] if status == "provisional" else [], evidence=decision))
    elif metric in SALES_SIGNALS:
        branches.extend(sales_branches(db, start, incident["scope"], at_detection, history, rolling=bool(window)))
    if not branches:
        branches.extend(upstream_branches(db, incident, start, at_detection, history, rolling=bool(window)))
    if not branches:
        branches.append(hypothesis("Scope or telemetry changed",
                                   unresolved=["There is insufficient linked event telemetry to compare competing explanations."],
                                   evidence={"scope": incident["scope"], "metric": metric}))
    return branches, watch_receipts, cutoff
