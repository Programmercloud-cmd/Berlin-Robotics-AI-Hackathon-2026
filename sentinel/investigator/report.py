"""Final root-cause report (markdown), built only from the investigation's evidence records.

Every conclusion cites evidence ids (E12): each id is a structured record (tool, parameters, window, entity,
signal type, statistics, verdict) written by deterministic code right after the tool call, so any sentence
in the report can be audited back to the query that produced it."""
from __future__ import annotations

from sentinel.contracts import seg_key

LABEL_TEXT = {
    "supported_cause": "Supported cause",
    "hypothesis": "Likely cause (hypothesis, not proven)",
    "explained": "Explained by a planned change",
    "data_issue": "Data issue, not a business change",
    "unexplained": "Unexplained",
}


def render_report(g, case, best, impact: dict) -> str:
    inc = case.inc
    seg = seg_key(case.seg) or "all traffic"
    first = g.nodes[inc.first_cause].name if inc.first_cause in g.nodes else inc.first_cause
    lines = [f"## {inc.id} · {LABEL_TEXT.get(inc.label, inc.label)}",
             "",
             f"**What happened:** {first} changed in **{seg}** from {case.notes.get('onset', case.start)}."]
    if len(case.chain) > 1:
        sym = " → ".join(g.nodes[m].name for m in reversed(case.chain[:-1]))
        lines.append(f"**Symptoms downstream:** {sym} (explained by the first cause through the formula graph).")
    if best is not None:
        cites = sorted({i for link in best.evidence if link.result == "confirmed" for i in link.evidence_ids},
                       key=lambda x: int(x[1:]) if x[1:].isdigit() else 0)
        lines.append(f"**Leading explanation ({best.posterior:.0%}):** {best.statement}."
                     + (f" Supported by {', '.join(cites)}." if cites else ""))
    if impact.get("hours"):
        lab = impact.get("money_label", "contribution")
        lines.append(f"**Business impact:** {impact['orders_delta']:+,.0f} orders, GMV {impact['gmv_delta_eur']:+,.0f} EUR, "
                     f"{lab} {impact['contribution_eur_total']:+,.0f} EUR over {impact['hours']} h "
                     f"({impact['contribution_eur_per_h']:+,.0f} EUR/h; {lab} is {impact['margin']:.1%} of GMV).")
    walk = case.notes.get("walk") or {}
    if walk.get("visited"):
        lines += ["", "### Graph walk (pruned BFS)"]
        lines.append(f"- visited {len(walk['visited'])} node(s): {', '.join(walk['visited'])}")
        if walk.get("pruned"):
            lines.append(f"- pruned as normal (|z| < 3, branch not explored): {', '.join(dict.fromkeys(walk['pruned']))}")
        lines.append(f"- first-cause chain: {' <- '.join(case.chain)}")
        if walk.get("secondary"):
            lines.append(f"- other anomalous branches (secondary candidates): {', '.join(walk['secondary'])}")
    lines += ["", "### Evidence"]
    for e in case.evidence:
        tag = f"{e.step}" + (f"/{e.signal}" if e.signal and e.signal != e.step else "")
        verdict = f" -> {e.verdict}" if e.verdict else ""
        lines.append(f"- **{e.id}** [{tag}] {e.interpretation}{verdict}")
    tested = [h for h in case.hyps]
    if tested:
        lines += ["", "### Hypotheses tested"]
        for h in tested:
            lines.append(f"- **{h.id}** ({h.source}, prior {h.prior:.2f} → {h.posterior:.2f}, score {h.score:.2f}, "
                         f"{h.status}): {h.statement}")
            for p, link in zip(h.predictions, h.evidence or [None] * len(h.predictions)):
                lines.append(f"  - {p}" + (f" (log-LR {link.log_lr:+.2f})" if link is not None else ""))
    lines += ["", "### What remains uncertain"]
    mech = case.hyp_meta.get(best.id, {}).get("mechanism") if best is not None else None
    if mech:
        samples = case.hyp_meta[best.id].get("samples") or []
        if samples:
            lines.insert(lines.index("### Evidence"), "**Observed events:** " + "; ".join(
                f"`{s['id']}` {s['activity']} at {s['at'][11:16]}"
                + (" (" + ", ".join(f"{k}={v}" for k, v in s["attrs"].items()) + ")" if s["attrs"] else "")
                for s in samples) + chr(10))
        lines.append(f"- The data shows the failure mechanism ({mech} in this segment). Why it happens, for example "
                     "a provider incident or a release, is not visible in the observations: no release or "
                     "configuration change was recorded.")
    elif inc.label == "supported_cause":
        lines.append("- Attribution to the event rests on timing, scope and mechanism; it is not an experiment. "
                     "A rollback that restores the metric would strengthen it.")
    elif inc.label == "hypothesis":
        lines.append("- The leading explanation fits the evidence but did not reach 0.8; alternatives are listed above.")
    elif inc.label == "unexplained":
        lines.append("- No tested explanation fits. The change is real and localised as above; human follow-up needed.")
    elif inc.label == "explained":
        lines.append("- The change matches a planned event's scope; residual effects inside the scope are listed in the evidence.")
    else:
        lines.append("- Values will be re-checked once the partition is complete.")
    return "\n".join(lines)
