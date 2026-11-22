"""Propose shared explanations using scope, overlapping observations and linked evidence."""

from collections import defaultdict
from datetime import datetime

from psycopg.types.json import Jsonb

from eye.cohort_evidence import graph_path, read_cohorts, supports_link
from eye.downstream_impact import PROJECTIONS, assess, build_paths
from eye.intervals import HOUR
from eye.transitions import ALERT_STEPS, POSITIONS, compatible

COMPLETENESS = "data_completeness_ratio"
STOCK = {"available_units", "stockout_sku_share"}
VOLUME = {"add_to_cart_sessions", "checkout_sessions", "payment_attempts", "successful_payments", "orders",
          "session_conversion_rate"}


def scope_strength(left, right):
    shared = left.keys() & right.keys()
    return (not left or not right or bool(shared)) and compatible(left, right)


def active(incidents, kind, scope):
    return next((row for row in incidents if row["report"].get("shared_explanation", {}).get("kind") == kind
                 and row["scope"] == scope), None)


def conflicting_step(row, workflow, *, except_step=None):
    stage = ALERT_STEPS.get(row["metric_id"], -1)
    return any(item["completion"]["status"] == "degraded" and item["completion"]["id"] != except_step
               and POSITIONS[item["completion"]["id"]] <= stage
               and scope_strength(item["completion"]["scope"], row["scope"]) for item in workflow)


def source_plans(alerts, incidents, facts):
    sources = defaultdict(list)
    for row in alerts:
        if row["metric_id"] == COMPLETENESS and row["scope"].get("source_id"):
            sources[row["scope"]["source_id"]].append(row)
    event_sources = defaultdict(set)
    for name, rows in sources.items():
        for row in rows:
            if event := row["scope"].get("event_type"):
                event_sources[event].add(name)
    for name, rows in sources.items():
        scope = {"source_id": name}
        existing = active(incidents, "source", scope)
        types = {row["scope"]["event_type"] for row in rows if row["scope"].get("event_type")}
        if len(types) < 2 and not existing:
            continue
        members = sorted(types | set(existing["report"]["shared_explanation"]["event_types"] if existing else ()))
        matching = rows + [row for row in alerts if row["metric_id"] == COMPLETENESS
                           and set(row["scope"]) == {"event_type"}
                           and event_sources[row["scope"]["event_type"]] == {name}]
        if facts:
            deficits = {key: max(0, count - facts.received[key]) for key, count in facts.manifest.items()}
            total = sum(deficits.values())
            if total and sum(count for (source, _), count in deficits.items() if source == name) >= .8 * total:
                matching.extend(row for row in alerts if row["metric_id"] == COMPLETENESS and not row["scope"])
        yield {"kind": "source", "scope": scope, "metric_id": COMPLETENESS, "incident": existing,
               "alerts": matching, "event_types": members, "confidence": "supported",
               "summary": f"Several event types had missing receipts from {name} in overlapping windows."}


def cohort_plan(db, kind, start, cutoff, alerts, incidents, workflow, facts):
    stock = kind == "stock"
    roots = [row for row in alerts if row["metric_id"] in STOCK] if stock else []
    volumes = [row for row in alerts if row["metric_id"] in VOLUME and row["observed"] < row["expected"]]
    existing = next((row for row in incidents if row["report"].get("shared_explanation", {}).get("kind") == kind), None)
    if not roots and (not volumes or (stock and not existing)):
        return None
    if not stock and not facts.following["search_requested"]:
        return None
    if not stock and not any(row["attrs"].get("experiment_arm") == "rollout"
                             for row in facts.following["search_requested"]) and not existing:
        return None
    path = graph_path(db, kind)
    if not path:
        return None
    if existing:
        scope = existing["scope"]
    else:
        signals = roots or volumes
        scope = min(signals, key=lambda row: len(row["scope"]))["scope"]
    roots = [row for row in roots if scope_strength(scope, row["scope"])]
    evidence = read_cohorts(db, kind, start, start + HOUR, cutoff, scope, facts)
    supported = supports_link(kind, evidence)
    last_support = existing["report"]["shared_explanation"].get("last_support") if existing else None
    overlaps_support = last_support and start - datetime.fromisoformat(last_support) < HOUR
    segments = evidence["segments"] if supported else existing["report"]["shared_explanation"].get("segments", []) if overlaps_support else []
    linked = [row for row in volumes if scope_strength(scope, row["scope"])
              and any(scope_strength(segment, row["scope"]) for segment in segments)
              and not conflicting_step(row, workflow, except_step="checkout" if stock else None)] if supported or overlaps_support else []
    if stock:
        linked = [row for row in linked if ALERT_STEPS[row["metric_id"]] >= POSITIONS["checkout"]]
    attached = roots + linked
    if not attached:
        return None
    anchor = min(roots or linked, key=lambda row: (len(row["scope"]), -row["score"]))
    return {"kind": kind, "metric_id": existing["metric_id"] if existing else anchor["metric_id"] if stock else "session_conversion_rate",
            "scope": scope, "incident": existing, "alerts": attached, "evidence": evidence, "graph_path": path,
            "segments": segments,
            "confidence": "supported" if supported else "provisional", "last_support": start.isoformat() if supported else last_support,
            "summary": ("Availability readings share an observed inventory dependency." if stock else
                        "Search rollout cohorts share a loss of recommendation exposure and cart entry."),
            "completion_checks": [group["completion"] for group in workflow
                                  if scope_strength(scope, group["completion"]["scope"])]}


def revisit_groups(db, start, cutoff, readings, facts):
    """Reassess overlapping evidence edges while both incidents still describe this episode."""
    incidents = db.execute(
        """select i.*, r.parent_id, r.status as relationship_status from incidents i
           left join incident_relationships r on r.incident_id=i.id
           where opened_at >= %s and opened_at <= %s order by opened_at, id""", (start - HOUR, start),
    ).fetchall()
    for parent in incidents:
        shared = parent["report"].get("shared_explanation", {})
        kind = shared.get("kind")
        if kind not in {"stock", "search"} or parent["recovered_at"]:
            continue
        children = [row for row in incidents if row["id"] != parent["id"] and not row["recovered_at"]
                    and parent["opened_at"] <= row["opened_at"] and row["metric_id"] in PROJECTIONS
                    and not row["report"].get("shared_explanation") and compatible(parent["scope"], row["scope"])
                    and row["parent_id"] in (None, parent["id"]) and row["relationship_status"] != "independent"]
        if not children:
            continue
        journeys = []
        evidence = read_cohorts(db, kind, start, start + HOUR, cutoff, parent["scope"], facts, journeys=journeys)
        if not evidence["preceded"]:
            continue
        supported = supports_link(kind, evidence)
        paths = build_paths(db, journeys)
        for child in children:
            decision = assess(paths, kind, child, readings)
            status = decision["status"]
            previous = child["relationship_status"]
            if status == "linked" and not supported:
                status = "provisional"
                decision.update(status=status, reason="The measured upstream change still has provisional cohort support.")
            if previous == "linked" and status != "independent":
                continue
            if previous is None and status == "independent":
                continue
            decision.update(parent_id=parent["id"], incident_id=child["id"], window_start=start.isoformat(),
                            observed_at=cutoff.isoformat(), upstream=kind)
            saved = db.execute(
                """insert into incident_relationships values (%s,%s,%s,%s,%s) on conflict (incident_id)
                   do update set status=excluded.status, observed_at=excluded.observed_at, decision=excluded.decision
                   where incident_relationships.parent_id=excluded.parent_id""",
                (child["id"], parent["id"], status, cutoff, Jsonb(decision)),
            )
            if saved.rowcount and status != previous:
                db.execute("insert into evidence(incident_id, window_start, kind, payload) values (%s,%s,'incident grouping',%s)",
                           (child["id"], start, Jsonb(decision)))
                if status in {"linked", "independent"}:
                    db.execute("""update incidents set report=report || '{"deep_investigation":"pending"}'
                                  where id=%s""", (parent["id"],))
