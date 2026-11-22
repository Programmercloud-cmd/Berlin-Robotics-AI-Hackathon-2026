"""Incident lifecycle: one incident per problem, tracked until it resolves.

  * anomalies that belong to an open incident (same first-cause chain, compatible segment, close in time)
    are attached to it instead of opening a new one;
  * the most impactful unexplained anomaly opens a new incident and starts an investigation;
  * every tick the first cause is re-checked in its segment: alerted -> monitoring -> resolved after
    3 normal complete hours; a recurrence reopens the same incident;
  * the fingerprint is first_cause|segment (not the hypothesis, which can change mid-investigation).
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from sentinel.bus import EventBus
from sentinel.contracts import Anomaly, Incident, seg_key

from .react import Investigator, fmt_seg

ALERT_MIN_EUR_PER_H, RESOLVE_HOURS, ATTACH_HOURS, MIN_INVESTIGATE_EUR_PER_H = 50.0, 3, 8, 25.0
LEARN_HOURS = 24      # how long a learned "normal" pattern corrects the baseline


def parse_seg(k: str) -> dict:
    return dict(p.split("=", 1) for p in k.split("|")) if k else {}


def compatible(a: dict, b: dict) -> bool:
    """The segments overlap: no shared dimension with different values (total overlaps everything)."""
    return all(b[k] == v for k, v in a.items() if k in b)


class IncidentManager:
    def __init__(self, graph, monitor, investigator: Investigator, bus: EventBus):
        self.g, self.mon, self.inv, self.bus = graph, monitor, investigator, bus
        self.incidents: dict[str, Incident] = {}
        self.cases: dict[str, object] = {}
        self.related: dict[str, set[str]] = {}
        self.normal_run: dict[str, int] = {}
        self.last_seen: dict[str, pd.Timestamp] = {}
        self.alerted: set[str] = set()

    # ------------------------------------------------------------------ anomalies -> incidents
    def handle(self, anomalies: list[dict], now) -> list[str]:
        now = pd.Timestamp(now)
        changed = []
        todo = sorted(anomalies, key=lambda a: -abs(a.get("impact_eur_per_h") or 0))
        while todo:
            a = todo.pop(0)
            inc = self._match(a, now)
            if inc:
                self._attach(inc, a, now)
                changed.append(inc.id)
                continue
            imp = abs(a.get("impact_eur_per_h") or 0.0)
            if 0 < imp < MIN_INVESTIGATE_EUR_PER_H:   # has a euro path but too small to spend an investigation on
                self.bus.publish("WATCH", "incidents", {"metric": a["metric"], "by": [a["segment"] or "total"],
                    "reasons": [f"minor anomaly ({imp:.0f} EUR/h < {MIN_INVESTIGATE_EUR_PER_H:.0f}): watched, not investigated"]})
                continue
            inc = self._open(a, now)
            case = self.inv.investigate(inc, a, now)
            self.cases[inc.id] = case
            self.related[inc.id] = self._related_set(case)
            self._after_conclusion(inc, now)
            changed.append(inc.id)
            # anomalies of this tick explained by the new incident
            rest = []
            for b in todo:
                if self._fits(inc, b):
                    self._attach(inc, b, now, quiet=True)
                else:
                    rest.append(b)
            todo = rest
        return sorted(set(changed))

    def _related_set(self, case) -> set[str]:
        fc = case.inc.first_cause
        rel = set(case.chain) | {fc} | set(self.g.downstream(fc, types=("formula", "causal")))
        for h in case.hyps:
            c = case.hyp_meta.get(h.id, {}).get("cause")
            if c and h.status != "rejected":
                rel.add(c)
        # volume inputs of downstream outputs also move with the first cause (payment costs, discounts with GMV)
        for m in list(rel):
            rel |= {e.src for e in self.g.inputs(m) if self.g.nodes[e.src].kind in ("count", "sum")}
        return rel

    def _fits(self, inc: Incident, a: dict) -> bool:
        """Attach an anomaly only if this incident explains it: related metric, overlapping segment, and the
        incident's segment carries most of the anomaly's deviation (so an explained campaign incident cannot
        swallow a payment fault that merely overlaps it)."""
        aseg, iseg = parse_seg(a["segment"]), parse_seg(inc.segment)
        if a["metric"] not in self.related.get(inc.id, set()) or not compatible(aseg, iseg):
            return False
        # a minor incident without a supported cause cannot explain a much larger anomaly
        case = self.cases.get(inc.id)
        own = abs(case.notes.get("impact", {}).get("contribution_eur_per_h", 0.0)) if case is not None else 0.0
        if inc.label in ("unexplained", "hypothesis") and inc.id not in self.alerted and \
                abs(a.get("impact_eur_per_h") or 0.0) > max(2 * own, ALERT_MIN_EUR_PER_H):
            return False
        if all(aseg.get(k) == v for k, v in iseg.items()):
            return True                      # the anomaly lies inside the incident's segment
        inter = {**aseg, **iseg}
        if not self.inv.tools.supports(a["metric"], inter):
            return False
        ts = pd.Timestamp(a["ts"])
        outer = self.inv.tools.window_dev(a["metric"], aseg, ts, ts + timedelta(hours=1))
        inner = self.inv.tools.window_dev(a["metric"], inter, ts, ts + timedelta(hours=1))

        def dev(w):
            if not np.isfinite(w.get("actual", np.nan)):
                return 0.0
            d = w["actual"] - w["expected"]
            return d * w.get("volume", 1.0) if "volume" in w else d
        do = dev(outer)
        share = dev(inner) / do if do else 0.0
        # the incident's slice must carry the deviation out of proportion to its size: a slice with a quarter
        # of the volume that merely moves like everything else does not explain the anomaly
        vo = outer.get("volume", outer.get("expected", 0.0)) or 0.0
        vi = inner.get("volume", inner.get("expected", 0.0)) or 0.0
        vol_share = vi / vo if vo else 1.0
        return share >= 0.5 and share >= min(1.0, 1.5 * vol_share)

    def _match(self, a: dict, now) -> Incident | None:
        aseg = parse_seg(a["segment"])

        def closeness(inc):   # 2: anomaly inside the incident's segment, 1: shares a key=value, 0: merely compatible
            iseg = parse_seg(inc.segment)
            if iseg and all(aseg.get(k) == v for k, v in iseg.items()):
                return 2
            return 1 if any(aseg.get(k) == v for k, v in iseg.items()) else 0

        live = [inc for inc in self.incidents.values() if inc.state != "resolved"
                and now - self.last_seen.get(inc.id, now) <= timedelta(hours=ATTACH_HOURS) and self._fits(inc, a)]
        if live:      # several incidents may fit: the one whose segment best covers the anomaly wins
            return max(live, key=lambda i: (closeness(i), i.updated_at))
        for inc in sorted(self.incidents.values(), key=lambda i: i.updated_at, reverse=True):
            if inc.state == "resolved" and self._fits(inc, a) and a["metric"] == inc.first_cause and \
                    parse_seg(a["segment"]) == parse_seg(inc.segment) and now - inc.updated_at <= timedelta(hours=24):
                inc.state = "reopened"
                self.bus.publish("INCIDENT_UPDATED", "incidents", {"id": inc.id, "state": "reopened",
                                                                   "why": "the first cause deviates again"}, inc.id)
                return inc
        return None

    def _open(self, a: dict, now) -> Incident:
        iid = f"INC-{len(self.incidents) + 1:03d}"
        inc = Incident(id=iid, fingerprint="", state="detected", opened_at=now, updated_at=now,
                       anomalies=[self._anom(a)], segment=a["segment"])
        self.incidents[iid] = inc
        self.last_seen[iid] = now
        self.bus.publish("INCIDENT_OPENED", "incidents", {"id": iid, "trigger": a}, iid)
        inc.state = "investigating"
        return inc

    def _escalate(self, inc: Incident, now, cause: str):
        """An incident first seen through a harmless symptom (e.g. CTR up) is re-judged when harmful symptoms
        of the same root cause join it: the incident is alerted on its net business harm."""
        if inc.id in self.alerted or inc.label not in ("supported_cause", "hypothesis", "unexplained"):
            return
        case = self.cases.get(inc.id)
        if case is None or case.notes.get("impact", {}).get("contribution_eur_per_h", 0.0) <= 0:
            return       # only incidents first judged favourable; harmful ones were already judged on their impact
        harm = min(0.0, min(a.impact_eur_per_h for a in inc.anomalies))
        if harm <= -ALERT_MIN_EUR_PER_H:
            inc.state = "alerted"
            self.alerted.add(inc.id)
            worst = min(inc.anomalies, key=lambda a: a.impact_eur_per_h)
            note = (f"\n\n**Escalated** at {now}: {cause} brought business harm into this incident "
                    f"({harm:+,.0f} EUR/h; largest: {worst.metric} [{worst.segment or 'total'}]).")
            inc.report_md += note
            self.bus.publish("FINAL_REPORT", "incidents", {"id": inc.id, "kind": "alert", "label": inc.label,
                                                           "first_cause": inc.first_cause, "segment": inc.segment,
                                                           "impact_eur_per_h": abs(harm), "escalated": True,
                                                           "report_md": inc.report_md}, inc.id)

    def _attach(self, inc: Incident, a: dict, now, quiet: bool = False):
        inc.anomalies.append(self._anom(a))
        self._escalate(inc, now, f"symptom {a['metric']} [{a['segment'] or 'total'}]")
        inc.updated_at = now
        self.last_seen[inc.id] = now
        self.normal_run[inc.id] = 0
        if not quiet:
            self.bus.publish("INCIDENT_UPDATED", "incidents", {"id": inc.id, "attached": f"{a['metric']} [{a['segment'] or 'total'}]",
                                                               "why": "same first-cause chain and compatible segment; "
                                                                      "not reported as a new incident"}, inc.id)

    def _root_event(self, inc_id: str):
        case = self.cases.get(inc_id)
        if case is None:
            return None
        evh = [h for h in case.hyps if h.status != "rejected" and h.posterior >= 0.5
               and case.hyp_meta.get(h.id, {}).get("event") is not None]
        if not evh:
            return None
        best = max(evh, key=lambda h: h.posterior)
        return case.hyp_meta[best.id]["event"].id

    def _learn_normal(self, inc: Incident, now, reason: str):
        """Write patterns an investigation confirmed as normal back into the monitor's baseline."""
        for a in inc.anomalies:
            if a.expected and np.isfinite(a.actual) and a.expected > 0:
                self.mon.learn(a.metric, parse_seg(a.segment), a.actual / a.expected, start=a.ts,
                               until=now + timedelta(hours=LEARN_HOURS), reason=f"{inc.id}: {reason}", incident=inc.id)

    def feedback(self, incident_id: str, kind: str, now=None):
        """Operator feedback: 'false_alarm' or 'planned'. The pattern becomes normal for the monitor."""
        inc = self.incidents[incident_id]
        now = pd.Timestamp(now) if now is not None else pd.Timestamp(inc.updated_at)
        self._learn_normal(inc, now, f"operator feedback: {kind}")
        inc.label, inc.state, inc.updated_at = "explained", "resolved", now
        self.bus.publish("INCIDENT_UPDATED", "incidents", {"id": incident_id, "state": "resolved", "label": "explained",
                                                           "why": f"operator feedback: {kind}; learned as normal"},
                         incident_id)

    def _after_conclusion(self, inc: Incident, now):
        inc.fingerprint = f"{inc.first_cause}|{inc.segment}"
        root = self._root_event(inc.id)
        same_root = [i for i in self.incidents.values() if i.id != inc.id and i.state != "resolved" and root
                     and self._root_event(i.id) == root and compatible(parse_seg(i.segment), parse_seg(inc.segment))]
        if same_root:   # different symptoms of one change (same release, overlapping segment): one incident
            keep = same_root[0]
            if keep.first_cause in self.g.downstream(inc.first_cause):     # the new one is further upstream
                keep.first_cause, keep.segment, keep.report_md = inc.first_cause, inc.segment, inc.report_md
                keep.hypotheses = inc.hypotheses
                keep.fingerprint = inc.fingerprint
                self.cases[keep.id] = self.cases[inc.id]
            keep.anomalies += inc.anomalies
            self.related[keep.id] |= self.related.get(inc.id, set())
            inc.state = "resolved"
            self.bus.publish("INCIDENT_UPDATED", "incidents", {"id": inc.id, "state": "merged", "merged_into": keep.id,
                "why": f"same root cause ({root}) and overlapping segment: {inc.first_cause} is another symptom"}, inc.id)
            self._escalate(keep, now, f"merged {inc.id} ({inc.first_cause} [{inc.segment or 'total'}])")
            self.bus.publish("INCIDENT_UPDATED", "incidents", {"id": keep.id, "state": keep.state,
                "label": keep.label, "why": f"absorbed {inc.id}; first cause {keep.first_cause}",
                "report_md": keep.report_md}, keep.id)
            return
        dup = [i for i in self.incidents.values() if i.id != inc.id and i.fingerprint == inc.fingerprint
               and i.state != "resolved"]
        if dup:   # the same problem found again from another symptom: merge, never report twice
            keep = dup[0]
            keep.anomalies += inc.anomalies
            inc.state = "resolved"
            self.related[keep.id] |= self.related.get(inc.id, set())
            self.bus.publish("INCIDENT_UPDATED", "incidents", {"id": inc.id, "state": "merged", "merged_into": keep.id,
                                                               "why": f"same fingerprint {inc.fingerprint}"}, inc.id)
            return
        signed = self.cases[inc.id].notes.get("impact", {}).get("contribution_eur_per_h", 0.0)
        impact = abs(signed)
        # alert on business harm only: a favourable change (e.g. a traffic jump) is recorded, not paged
        unconfirmed = self.cases[inc.id].notes.get("unconfirmed", False)
        if inc.label in ("supported_cause", "hypothesis", "unexplained") and signed <= -ALERT_MIN_EUR_PER_H                 and not unconfirmed:
            inc.state = "alerted"
            self.alerted.add(inc.id)
            self.bus.publish("FINAL_REPORT", "investigator", {"id": inc.id, "kind": "alert", "label": inc.label,
                                                              "first_cause": inc.first_cause, "segment": inc.segment,
                                                              "impact_eur_per_h": impact, "report_md": inc.report_md}, inc.id)
        else:
            inc.state = "monitoring"
            if inc.label == "explained" or (signed > 0 and inc.label != "data_issue"):
                self._learn_normal(inc, now, "explained by a planned change" if inc.label == "explained"
                                   else "favourable change")
            why = {"explained": "explained by a planned change: no alert",
                   "data_issue": "data issue: held until the data is complete"}.get(inc.label,
                   "not confirmed by any formula input (amplified noise): watched, no alert" if unconfirmed else
                   f"favourable change (+{impact:,.0f} EUR/h): recorded, no alert" if signed > 0 else
                   f"impact {impact:,.0f} EUR/h below the alert threshold")
            self.bus.publish("INCIDENT_UPDATED", "incidents", {"id": inc.id, "state": "monitoring", "label": inc.label,
                                                               "why": why, "report_md": inc.report_md}, inc.id)

    # ------------------------------------------------------------------ follow-up every tick
    def follow_up(self, now):
        now = pd.Timestamp(now)
        for inc in self.incidents.values():
            if inc.state in ("resolved", "investigating", "detected") or not inc.first_cause:
                continue
            seg = parse_seg(inc.segment)
            h = self.inv.tools.hourly(inc.first_cause, seg, now - timedelta(hours=1), now)
            if h.empty:
                continue
            z = float(h.z.iloc[-1])
            if abs(z) < 3:
                self.normal_run[inc.id] = self.normal_run.get(inc.id, 0) + 1
                if inc.state in ("alerted", "reopened"):
                    inc.state = "monitoring"
                    self.bus.publish("INCIDENT_UPDATED", "incidents", {"id": inc.id, "state": "monitoring",
                        "why": f"{inc.first_cause} [{fmt_seg(seg)}] back within the expected band (z={z:+.1f})"}, inc.id)
                if self.normal_run[inc.id] >= RESOLVE_HOURS:
                    inc.state, inc.updated_at = "resolved", now
                    case = self.cases.get(inc.id)
                    dur = (now - pd.Timestamp(case.notes.get("onset", inc.opened_at))) if case else None
                    if case is not None:   # final numbers over the whole incident, not just the first hour
                        case.end = now - timedelta(hours=RESOLVE_HOURS)
                        imp = self.inv.impact(case)
                        case.notes["impact"] = imp
                        inc.impact_eur_total = imp.get("contribution_eur_total", 0.0)
                        best = max((h for h in case.hyps if h.status != "rejected"), key=lambda h: h.posterior, default=None)
                        from .report import render_report
                        inc.report_md = render_report(self.g, case, best, imp)
                    self.bus.publish("INCIDENT_RESOLVED", "incidents", {"id": inc.id, "label": inc.label,
                        "why": f"{RESOLVE_HOURS} consecutive normal complete hours", "duration": str(dur)}, inc.id)
                    if inc.id in self.alerted:   # only incidents that paged someone get a resolution report
                        extra = self._recovery_evidence(inc, case, now)
                        self.bus.publish("FINAL_REPORT", "incidents", {"id": inc.id, "kind": "resolved", "label": inc.label,
                            "first_cause": inc.first_cause, "segment": inc.segment,
                            "impact_eur_total": inc.impact_eur_total,
                            "report_md": inc.report_md + f"\n\n### Resolution\n**Resolved** at {now} after {dur}: "
                                                         f"{inc.first_cause} back to normal for {RESOLVE_HOURS} hours." + extra},
                            inc.id)
            else:
                self.normal_run[inc.id] = 0
                self.last_seen[inc.id] = now

    def _recovery_evidence(self, inc: Incident, case, now) -> str:
        """Did the recovery coincide with a rollback of the suspected service? (strengthens, does not prove)"""
        if case is None:
            return ""
        best = max((h for h in case.hyps if h.status != "rejected"), key=lambda h: h.posterior, default=None)
        ev = case.hyp_meta.get(best.id, {}).get("event") if best else None
        if ev is None or not ev.service:
            return ""
        later = [e for e in self.inv.data.events(pd.Timestamp(ev.ts) + timedelta(hours=1), now)
                 if e.service == ev.service]
        if not later:
            return "\n- No rollback of the suspected service was recorded; the recovery is not attributed."
        e = later[0]
        self.bus.publish("HYPOTHESIS", "incidents", {"id": best.id, "statement": best.statement,
            "prediction": "recovery coincides with a rollback of the same service", "result": "confirmed",
            "why": f"{e.description} at {e.ts:%H:%M}"}, inc.id)
        return (f"\n- Recovery followed **{e.service} '{e.description}'** at {e.ts:%m-%d %H:%M}: consistent with the "
                f"release being the cause (supporting evidence, still not an experiment).")

    @staticmethod
    def _anom(a: dict) -> Anomaly:
        return Anomaly(metric=a["metric"], segment=a["segment"], ts=pd.Timestamp(a["ts"]).to_pydatetime(),
                       actual=float(a["actual"]), expected=float(a["expected"]), z=float(np.clip(a["z"], -1e6, 1e6)),
                       detector="cusum" if a["detector"] == "cusum" else "spike",
                       impact_eur_per_h=float(a.get("impact_eur_per_h") or 0.0))
