"""Apply later grouping evidence to a current report without changing its snapshots."""

from datetime import datetime, timezone


def assemble_report(report, incident_id, decisions):
    conclusions, branches, uncertainties, causes = [], [], [], []
    superseded = set()
    for decision in decisions or ():
        child, parent = decision["incident_id"], decision["parent_id"]
        incoming = incident_id == child
        status = decision["status"]
        if status != "linked" and not incoming:
            continue
        at = datetime.fromisoformat(decision["observed_at"]).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        stock_checkout = decision["upstream"] == "stock" and decision["metric_id"] == "checkout_sessions"
        upstream = {"stock": "Stockouts", "attempt_volume": "Fewer payment attempts"}.get(decision["upstream"],
                                                                                       "The linked upstream change")
        metric = "checkout starts" if stock_checkout else decision["metric_id"].replace("_", " ")
        title = f"Upstream link to incident #{parent}" if incoming else f"Downstream incident #{child}"
        superseded.add(title)
        supports, weakens, unresolved = [], [], []
        if status == "linked":
            estimate, loss = decision["upstream_explained"], decision["scope_volume_loss"]
            conclusion = (f"Eye confirmed the link between incidents #{parent} and #{child} at {at}. "
                          f"{upstream} accounted for an estimated {estimate:.1f} of {loss:.1f} missing {metric}.")
            supports.append(conclusion)
            cohort = decision["linked_cohort"]
            if stock_checkout:
                supports.append(f"Checkout started for {cohort['exposed_completed']} of {cohort['exposed_eligible']} carts "
                                f"with recorded stockouts and {cohort['control_completed']} of {cohort['control_eligible']} stocked carts.")
            if decision["checked_completion_rate"] is not None:
                population = "Stocked carts" if stock_checkout else "Scoped attempts"
                supports.append(f"{population} completed at {decision['checked_completion_rate']:.1%} "
                                f"versus {decision['baseline_completion_rate']:.1%} historically.")
            remainder = decision["unexplained"]
            unresolved.append(f"An estimated {remainder:.1f} missing {metric} remain unexplained, within sampling uncertainty.")
            if stock_checkout:
                unresolved.append("The telemetry does not establish what depleted inventory or rule out other contributing changes.")
            if incoming:
                causes.append(f"{upstream} accounted for the measured shortfall within sampling uncertainty.")
        elif status == "provisional":
            conclusion = f"The upstream link to incident #{parent} remains provisional as of {at}. {decision['reason']}"
            unresolved.append(conclusion)
        else:
            conclusion = f"Eye kept this incident separate from incident #{parent} at {at}. {decision['reason']}"
            weakens.append(conclusion)
        conclusions.append(conclusion)
        uncertainties.extend(unresolved)
        branches.append({"title": title, "supports": supports, "weakens": weakens, "unresolved": unresolved})

    if not conclusions:
        return report
    current = {**report, "conclusion": conclusions,
               "evidence_branches": branches + [branch for branch in report.get("evidence_branches", ())
                                                if branch["title"] not in superseded]}
    if causes:
        current["likely_causes"] = causes
    if uncertainties:
        earlier = report.get("uncertainty")
        if earlier and not any(branch["supports"] for branch in branches):
            uncertainties.append(earlier)
        current["uncertainty"] = uncertainties
    return current
