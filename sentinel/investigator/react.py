"""3. Investigator — ReAct brain.

Receives an alert, then loops Thought -> Action (tool) -> Observation, every step published on the bus
and stored as numbered evidence (E1, E2, ...). The policy is deterministic and explainable:

  data_check   is the data complete? (an incomplete source ends the case as a data issue)
  decompose    walk formula edges upstream while one input carries the change -> first cause;
               everything above it is a downstream symptom
  localise     drill down dimension by dimension while the change concentrates (lift)
  control      Welch t-test: is the change specific to the segment, siblings normal?
  events       releases / campaigns near the onset whose scope and service fit -> hypotheses;
               events whose scope contradicts the segment are rejected with the reason
  causal       causal parents of the first cause: did they move (+ Granger)?
  predict      each hypothesis predicts timing and confinement; confirmations update its posterior
  dead end     no hypothesis reaches 0.8 or its scope cannot be verified -> ask the Meta-Agent to
               extend the graph, then re-test with the new dimensions / metrics
  conclude     supported_cause / hypothesis / explained / data_issue / unexplained, impact in EUR
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from sentinel.bus import EventBus
from sentinel.contracts import Evidence, Hypothesis, seg_key

from .tools import Toolbox

LR_CONFIRM, LR_REFUTE = 3.0, 0.3


def odds_update(p: float, lr: float) -> float:
    o = p / (1 - p) * lr
    return float(o / (1 + o))


def fmt_seg(seg: dict) -> str:
    return seg_key(seg) or "total"


UNCONFIRMED_Z = 3.0      # an input must move this much for a formula output's change to count

class Case:
    """Working memory of one investigation."""

    def __init__(self, inc, anomaly: dict, start, end):
        self.inc = inc
        self.entry = anomaly["metric"]
        self.seg = dict(p.split("=", 1) for p in anomaly["segment"].split("|")) if anomaly["segment"] else {}
        self.start, self.end = pd.Timestamp(start), pd.Timestamp(end)
        self.focus = self.entry
        self.chain: list[str] = [self.entry]       # entry -> ... -> first cause
        self.hyps: list[Hypothesis] = []
        self.hyp_meta: dict[str, dict] = {}
        self.evidence: list[Evidence] = []
        self.notes: dict = {}
        self.dead_end_reasons: list[str] = []


class Investigator:
    def __init__(self, graph, data, monitor, bus: EventBus, meta=None, max_steps: int = 60):
        self.g, self.data, self.mon, self.bus, self.meta = graph, data, monitor, bus, meta
        self.tools = Toolbox(graph, data, monitor)
        self.max_steps = max_steps
        self._ev_counter = 0

    # ================================================================ ReAct plumbing
    def step(self, case: Case, stage: str, thought: str, action: str, fn, interpret) -> object:
        inc_id = case.inc.id
        self.bus.publish("THOUGHT", "investigator", {"stage": stage, "text": thought, "focus": case.focus,
                                                     "segment": fmt_seg(case.seg)}, inc_id)
        self.bus.publish("ACTION", "investigator", {"stage": stage, "tool": action, "focus": case.focus,
                                                    "segment": fmt_seg(case.seg)}, inc_id)
        result = fn()
        text, numbers = interpret(result)
        self._ev_counter += 1
        ev = Evidence(id=f"E{self._ev_counter}", incident_id=inc_id,
                      step={"data_check": "data_check", "decompose": "decompose", "localise": "localise",
                            "control": "localise", "events": "events", "causal": "predict", "predict": "predict",
                            "meta": "frontier"}.get(stage, "frontier"),
                      query=action, result=numbers, interpretation=text)
        case.evidence.append(ev)
        case.inc.evidence_ids.append(ev.id)
        self.bus.publish("OBSERVATION", "investigator", {"stage": stage, "evidence_id": ev.id, "text": text,
                                                         "numbers": numbers, "focus": case.focus,
                                                         "segment": fmt_seg(case.seg)}, inc_id)
        return result

    def add_hyp(self, case: Case, statement: str, source: str, prior: float, meta: dict) -> Hypothesis:
        h = Hypothesis(id=f"H{len(case.hyps) + 1}", statement=statement, source=source, predictions=[],
                       prior=round(prior, 3), posterior=round(prior, 3))
        case.hyps.append(h)
        case.hyp_meta[h.id] = meta
        self.bus.publish("HYPOTHESIS", "investigator", {"id": h.id, "statement": statement, "source": source,
                                                        "prior": prior, "status": "open"}, case.inc.id)
        return h

    def judge(self, case: Case, h: Hypothesis, prediction: str, ok: bool | None, why: str):
        h.predictions.append(f"{prediction} -> {'confirmed' if ok else 'refuted' if ok is False else 'untestable'}: {why}")
        if ok is True:
            h.posterior = round(odds_update(h.posterior, LR_CONFIRM), 3)
        elif ok is False:
            h.posterior = round(odds_update(h.posterior, LR_REFUTE), 3)
        self.bus.publish("HYPOTHESIS", "investigator", {"id": h.id, "statement": h.statement, "prediction": prediction,
                                                        "result": "confirmed" if ok else "refuted" if ok is False else "untestable",
                                                        "why": why, "posterior": h.posterior}, case.inc.id)

    # ================================================================ the investigation
    def investigate(self, inc, anomaly: dict, now) -> Case:
        now = pd.Timestamp(now)
        a_ts = pd.Timestamp(anomaly["ts"])
        case = Case(inc, anomaly, a_ts - timedelta(hours=2), now)
        self.bus.publish("THOUGHT", "investigator", {"stage": "start", "text":
            f"Alert on {anomaly['metric']} [{fmt_seg(case.seg)}] at {anomaly['ts']}: actual {anomaly['actual']:.4g} vs "
            f"expected {anomaly['expected']:.4g} (z={anomaly['z']:+.1f}). Start an investigation."}, inc.id)

        # ---- 1. data check
        dq = self.step(case, "data_check", "Before interpreting the change: is the data behind the alert complete?",
                       f"data_check({case.entry}, {a_ts}..{a_ts + timedelta(hours=1)})",
                       lambda: self.tools.data_check(case.entry, a_ts, a_ts + timedelta(hours=1)),
                       lambda r: (("all source partitions complete" if all(not v["incomplete_hours"] for v in r.values())
                                   else "incomplete partitions: " + ", ".join(f"{t} {v['incomplete_hours']}" for t, v in r.items()
                                                                            if v["incomplete_hours"])),
                                  {t: v["min"] for t, v in r.items()}))
        if any(v["min"] < 0.95 for v in dq.values()) and all(v["incomplete_hours"] for v in dq.values()):
            return self.conclude(case, label="data_issue")

        # ---- 1b. RiskLoc clues from the monitor: start from the strongest candidate set
        clues = anomaly.get("clues") or []
        if clues:
            dims = self.g.nodes[case.entry].dimensions
            top = next((c for c in clues if all(k in dims for k in c["segment"])), None)
            self.step(case, "localise",
                      "The monitor attached RiskLoc candidate root-cause sets: start from the strongest one instead "
                      "of walking the whole graph from the aggregate.",
                      f"riskloc({case.entry}, {anomaly['ts']})", lambda: clues,
                      lambda r: ("; ".join(f"{fmt_seg(c['segment'])} (risk {c['risk']:.2f}, explains "
                                           f"{c['explanatory_power']:.0%}, z={c.get('z', 0):.1f})" for c in r),
                                 {fmt_seg(c["segment"]): c["explanatory_power"] for c in r}))
            case.notes["riskloc"] = clues
            if top:
                case.seg = dict(top["segment"])
                self.bus.publish("THOUGHT", "investigator", {"stage": "localise", "text":
                    f"Seed the investigation with {fmt_seg(case.seg)}; the next steps verify it (decomposition, "
                    f"drill-down, negative controls) rather than trust it."}, inc.id)

        # ---- 2. onset + decomposition down the formula graph
        onset = self.tools.onset(case.entry, case.seg, a_ts)
        case.start = onset
        case.notes["onset"] = str(onset)
        depth = 0
        while depth < 6:
            depth += 1
            dec = self.step(case, "decompose",
                            f"Which formula input of {case.focus} explains its change in [{fmt_seg(case.seg)}]?",
                            f"decompose({case.focus}, {fmt_seg(case.seg)}, {onset}..{now})",
                            lambda: self.tools.decompose(case.focus, case.seg, onset, now),
                            lambda r: self._decomp_text(case.focus, r))
            if not dec.get("possible"):
                break
            if depth == 1 and dec["parts"] and max(abs(x["z"]) for x in dec["parts"]) < UNCONFIRMED_Z:
                # a formula output can look extreme when it is a small difference of large sums (contribution);
                # if no input moved outside its own noise, the "change" is amplified noise, not a problem
                case.notes["unconfirmed"] = True
                self.bus.publish("THOUGHT", "investigator", {"stage": "decompose", "text":
                    f"No input of {case.focus} moved outside its own noise (all |z| < {UNCONFIRMED_Z}): the output's "
                    "deviation is amplified noise of a difference. Watch it; do not page."}, inc.id)
            top = dec["parts"][0]
            if top["share"] >= 0.5 and abs(top["z"]) >= 3:
                case.focus = top["input"]
                case.chain.append(top["input"])
                continue
            break
        case.inc.first_cause = case.focus
        self.bus.publish("THOUGHT", "investigator", {"stage": "decompose", "text":
            f"First cause candidate: {case.focus}. Downstream symptoms: {' <- '.join(reversed(case.chain[:-1])) or 'none'}."},
            inc.id)

        # ---- 3. localisation
        self.localise(case, onset, now)

        # ---- 4. control: keep only the segment keys the change is specific to (generalise), and use the
        #         siblings as a negative control
        for key in list(case.seg):
            r = self.step(case, "control", f"Is the change specific to {key}={case.seg[key]}, with the other {key} values normal?",
                          f"compare_control({case.focus}, {fmt_seg(case.seg)}, vs other {key})",
                          lambda key=key: self.tools.compare_control(case.focus, case.seg, key, onset, now),
                          lambda r, key=key: self._control_text(case, key, r))
            if r.get("possible") and not r["confined"]:
                case.seg.pop(key)
                self.bus.publish("THOUGHT", "investigator", {"stage": "control", "text":
                    f"The other {key} values move too ({r['siblings_rel']:+.1%}): {key} is not part of the cause. "
                    f"Generalise the segment to {fmt_seg(case.seg) if case.seg else 'the total'}."}, inc.id)
                if not case.seg:     # the alert's slice was a wrong guess: localise again from the total
                    self.localise(case, onset, now)
                    break
        case.inc.segment = seg_key(case.seg)

        # ---- re-estimate the onset on the final (cleaner) segment before matching events
        new_onset = self.tools.onset(case.focus, case.seg, a_ts)
        if new_onset != onset:
            self.bus.publish("THOUGHT", "investigator", {"stage": "localise", "text":
                f"On the localised segment the change starts at {new_onset:%m-%d %H:%M} (was {onset:%m-%d %H:%M} on the "
                f"alert's noisier slice)."}, inc.id)
            onset = new_onset
            case.start = onset
            case.notes["onset"] = str(onset)

        # ---- 5. hypotheses from events, 6. from causal edges, 7. predictions
        self.hypothesise(case, onset, now)

        # ---- 7b. observed failure mechanism from event-level data (OCEL sources only)
        if hasattr(self.data, "activity_mix"):
            self.mechanism(case, onset, now)

        # ---- 8. dead end -> meta-agent
        best = max((h.posterior for h in case.hyps if h.status != "rejected"), default=0.0)
        if self.meta is not None and (best < 0.8 or case.dead_end_reasons):
            reasons = case.dead_end_reasons or [f"best hypothesis posterior {best:.2f} < 0.8"]
            self.bus.publish("DEAD_END", "investigator", {"focus": case.focus, "segment": fmt_seg(case.seg),
                                                          "reasons": reasons}, inc.id)
            muts = self.meta.explore(case.focus, case.seg, onset, now, reasons, incident_id=inc.id)
            if muts:
                self.step(case, "meta", "The graph was extended; re-run localisation and hypothesis tests with the new knowledge.",
                          "meta_agent.explore()", lambda: muts,
                          lambda r: ("; ".join(m["summary"] for m in r), {"mutations": len(r)}))
                case.dead_end_reasons = []
                self.localise(case, onset, now)
                self.hypothesise(case, onset, now, rerun=True)
        return self.conclude(case)

    # ================================================================ stages
    def localise(self, case: Case, start, end):
        for _ in range(3):
            dims = [d for d in self.g.nodes[case.focus].dimensions if d not in case.seg]
            best = None
            for d in dims:
                r = self.step(case, "localise", f"Where is the change of {case.focus} concentrated? Split by {d}.",
                              f"drilldown({case.focus}, {fmt_seg(case.seg)}, by={d})",
                              lambda d=d: self.tools.drilldown(case.focus, case.seg, d, start, end),
                              lambda r: self._drill_text(r))
                top = r.get("top")
                if top and top["dev_share"] >= 0.6 and top["lift"] >= 1.5 and abs(top["z"]) >= 3:
                    if best is None or top["lift"] > best[1]["lift"]:
                        best = (d, top)
            if not best:
                break
            d, top = best
            case.seg[d] = top["member"]
            self.bus.publish("THOUGHT", "investigator", {"stage": "localise", "text":
                f"Narrow to {d}={top['member']}: it carries {top['dev_share']:.0%} of the deviation with "
                f"{top['vol_share']:.0%} of the volume (lift {top['lift']:.1f}x). Segment is now {fmt_seg(case.seg)}."},
                case.inc.id)
        case.inc.segment = seg_key(case.seg)

    def hypothesise(self, case: Case, start, end, rerun: bool = False):
        focus_dev = self.tools.window_dev(case.focus, case.seg, start, end)
        onset = pd.Timestamp(case.notes.get("onset", start))
        linked = set(self.g.services(case.focus))
        for m in case.chain:
            linked |= set(self.g.services(m))
        for m in self.g.upstream(case.focus, types=("causal",)):
            linked |= set(self.g.services(m))
        # events near the onset
        evs = self.step(case, "events", "Did anything change (release, config, campaign) around the onset?",
                        f"events({onset - timedelta(hours=3)}..{onset + timedelta(hours=1)})",
                        lambda: self._candidate_events(onset),
                        lambda r: ((f"{len(r)} events: " + "; ".join(f"{e.ts:%H:%M} {e.type} {e.service or ''} "
                                                                       f"'{e.description}' scope={fmt_seg(e.scope)}" for e in r))
                                   if r else "no events near the onset", {"events": len(r)}))
        known = {m.get("event_id") for m in case.hyp_meta.values()}
        for e in evs:
            if e.id in known and not rerun:
                continue
            clash = [k for k, v in e.scope.items() if k in case.seg and case.seg[k] != v]
            if e.id in known:
                h = next(h for h in case.hyps if case.hyp_meta[h.id].get("event_id") == e.id)
            else:
                stmt = f"{e.type} '{e.description}' ({e.service or 'no service'}) at {e.ts:%H:%M} caused the change"
                is_linked = e.service in linked if e.service else False
                prior = 0.35 if (is_linked or e.planned) else 0.12
                h = self.add_hyp(case, stmt, "event", prior, {"event_id": e.id, "event": e})
                if clash:
                    h.status, h.posterior = "rejected", 0.0
                    self.bus.publish("HYPOTHESIS", "investigator", {"id": h.id, "statement": stmt, "status": "rejected",
                        "why": f"event scope {fmt_seg(e.scope)} contradicts the affected segment {fmt_seg(case.seg)}"},
                        case.inc.id)
                    continue
                dt = (onset - pd.Timestamp(e.ts)).total_seconds() / 3600
                if e.planned and e.type == "campaign":
                    self.judge(case, h, "onset falls inside the running campaign window", 0 <= dt <= 24,
                               f"campaign started {e.ts:%m-%d %H:%M}, onset {onset:%m-%d %H:%M}")
                else:
                    self.judge(case, h, "onset coincides with the event (<= 1h)", abs(dt) <= 1.0,
                               f"onset {onset:%H:%M}, event {e.ts:%H:%M}")
            if h.status == "rejected":
                continue
            # confinement: scope keys must be verified by localisation
            unverifiable = [k for k in e.scope if k not in case.seg and k not in self.g.nodes[case.focus].dimensions]
            for k, v in e.scope.items():
                if any(p.startswith(f"change concentrated in {k}=") for p in h.predictions):
                    continue
                if k in case.seg and case.seg[k] == v:
                    ctl = self.tools.compare_control(case.focus, case.seg, k, start, end)
                    self.judge(case, h, f"change concentrated in {k}={v}, other {k} values normal",
                               bool(ctl.get("confined")),
                               f"{ctl.get('focus_rel', float('nan')):+.1%} vs other {k} {ctl.get('siblings_rel', float('nan')):+.1%}")
                elif k not in case.seg and k in self.g.nodes[case.focus].dimensions:
                    dd = self.tools.drilldown(case.focus, case.seg, k, start, end)
                    me = next((m for m in dd["members"] if m["member"] == v), None)
                    ok = bool(me and me["lift"] >= 1.3 and me["dev_share"] >= 0.4)
                    self.judge(case, h, f"change concentrated in {k}={v} (event scope)", ok,
                               (f"{v} carries {me['dev_share']:.0%} of the change with {me['vol_share']:.0%} of volume"
                                if me else "member not found"))
            if e.planned and e.scope and not any(p.startswith("change concentrated in") for p in h.predictions) \
                    and not any(p.startswith("scope of the planned change") for p in h.predictions):
                # the focus cannot be split by the plan's scope: check it on the metrics that compose the focus
                proxy = next((v.src for v in self.g.inputs(case.focus) if all(
                    k in self.g.nodes[v.src].dimensions for k in list(e.scope) + list(case.seg))), None)
                if proxy:
                    k, v = next(iter(e.scope.items()))
                    dd = self.tools.drilldown(proxy, case.seg, k, start, end)
                    me = next((m for m in dd["members"] if m["member"] == v), None)
                    ok = bool(me and me["lift"] >= 1.3 and me["dev_share"] >= 0.4)
                    self.judge(case, h, f"scope of the planned change checked on input {proxy}: concentrated in {k}={v}",
                               ok, (f"{v} carries {me['dev_share']:.0%} of {proxy}'s change with {me['vol_share']:.0%} "
                                    f"of volume") if me else "member not found")
                else:
                    self.judge(case, h, "scope of the planned change can be checked on this metric", False,
                               f"{case.focus} and its inputs cannot be split by {sorted(e.scope)}: timing alone "
                               "does not explain it")
            if unverifiable and not rerun:
                if any(k not in self.g.nodes[case.focus].dimensions for k in unverifiable):
                    case.dead_end_reasons.append(
                        f"cannot verify scope {fmt_seg({k: e.scope[k] for k in unverifiable})} of '{e.description}': "
                        f"{case.focus} has no dimension {unverifiable}")
            if e.planned and e.expected_effect:
                h.statement += " (planned; expected effect " + ", ".join(f"{k} x{v}" for k, v in e.expected_effect.items()) + ")"
            mech = e.service in linked if e.service else False
            if mech and not any(p.startswith("mechanism") for p in h.predictions):
                self.judge(case, h, "mechanism: the released service is linked to the first cause in the graph", True,
                           f"{e.service} -> {case.focus} via context edges")
        # causal parents of the first cause
        for edge in self.g.causes(case.focus):
            if any(case.hyp_meta[h.id].get("cause") == edge.src for h in case.hyps):
                continue
            r = self.step(case, "causal", f"Causal parent {edge.src} ({edge.sign}, conf {edge.confidence:.2f}): did it move first?",
                          f"causal_check({edge.src} -> {case.focus}, {fmt_seg(case.seg)})",
                          lambda edge=edge: self.tools.causal_check(edge.src, case.focus, edge.sign, case.seg, start, end,
                                                                    focus_dev["rel"]),
                          lambda r: (f"{r['cause']} [{r['segment'] or 'total'}] {r['rel']:+.1%} (z={r['z']:+.1f}); "
                                     f"{'consistent with' if r['consistent'] else 'does not explain'} the change"
                                     + (f"; Granger p={r['granger_p']:.2g}" if 'granger_p' in r else ""),
                                     {k: r[k] for k in ("rel", "z") if k in r}))
            h = self.add_hyp(case, f"{edge.src} moved and drove {case.focus} ({edge.sign})", "causal_edge",
                             min(0.5, edge.confidence * 0.6), {"cause": edge.src})
            self.judge(case, h, f"{edge.src} moved in the same segment with the expected sign", r["consistent"],
                       f"{r['rel']:+.1%}, z={r['z']:+.1f}")
            if r.get("granger_p") is not None and r["consistent"]:
                self.judge(case, h, "Granger precedence", r["granger_p"] < 0.01, f"p={r['granger_p']:.2g}")
            if not r["consistent"]:
                h.status = "rejected"
        # mechanism chain: an event whose service owns a cause that did move -> event -> cause -> first cause
        moved = [case.hyp_meta[h.id]["cause"] for h in case.hyps
                 if case.hyp_meta[h.id].get("cause") and h.status != "rejected" and h.posterior >= 0.5]
        for h in case.hyps:
            e = case.hyp_meta[h.id].get("event")
            if e is None or h.status == "rejected" or not e.service:
                continue
            for c in moved:
                if e.service in self.g.services(c) and not any(p.startswith("mechanism chain") for p in h.predictions):
                    self.judge(case, h, f"mechanism chain {e.service} -> {c} -> {case.focus}", True,
                               f"{c} belongs to {e.service} and moved with the expected sign at the onset")

    def mechanism(self, case: Case, start, end):
        """Which concrete events changed inside the segment? (e.g. payment_timed_out share 2% -> 63%).
        This is the observed failure mechanism, not yet its underlying cause."""
        acts = self.data.activities_for(case.focus)
        if len(acts) < 2:
            return
        denominators = {"payment_attempted", "session_started", "search_requested", "feed_impression"}
        outcomes = [a for a in acts if a not in denominators] or acts
        mix = self.step(case, "predict", f"Which events behind {case.focus} changed inside {fmt_seg(case.seg)}?",
                        f"activity_mix({outcomes}, {fmt_seg(case.seg)}, now vs same hours on 7 previous days)",
                        lambda: self.data.activity_mix(start, end, case.seg, outcomes),
                        lambda r: ("; ".join(f"{x['activity']} {x['now']} vs {x['baseline']:.0f} usual" for x in r),
                                   {x["activity"]: x["now"] for x in r}))
        tn, tb = sum(x["now"] for x in mix), sum(x["baseline"] for x in mix)
        if tn < 20 or tb <= 0:
            return
        shifts = [(x["now"] / tn - x["baseline"] / tb, x) for x in mix]
        # the mechanism is what appears MORE (timeouts, failures); a success share falling by the same amount
        # is its mirror image. Fall back to the largest drop only when nothing rose.
        rising = [sh for sh in shifts if sh[0] >= 0.05]
        delta, top = max(rising, key=lambda s: s[0]) if rising else max(shifts, key=lambda s: abs(s[0]))
        share_now, share_base = top["now"] / tn, top["baseline"] / tb
        if abs(delta) < 0.05:
            return
        samples = self.data.supporting_events(start, end, case.seg, top["activity"], limit=3)
        attrs = samples[0]["attrs"] if samples else {}
        detail = ", ".join(f"{k}={v}" for k, v in attrs.items() if k in ("response", "http_status", "result_count"))
        self.step(case, "predict", f"Show concrete {top['activity']} events from the affected segment.",
                  f"supporting_events({top['activity']}, {fmt_seg(case.seg)}, limit=3)",
                  lambda: samples,
                  lambda r: ((f"events {', '.join(s['id'] for s in r)}" + (f" ({detail})" if detail else ""))
                             if r else "no sample events", {"samples": len(r)}))
        verb = "rose" if delta > 0 else "fell"
        h = self.add_hyp(case, f"observed failure mechanism: {top['activity']} {verb} from {share_base:.1%} to "
                               f"{share_now:.1%} of {case.focus} outcomes in {fmt_seg(case.seg)}"
                               + (f" ({detail})" if detail else ""), "event", 0.4,
                         {"mechanism": top["activity"], "samples": samples})
        self.judge(case, h, f"{top['activity']} share changed inside the segment", True,
                   f"{share_base:.1%} -> {share_now:.1%} ({top['now']} events)")
        # outside the segment the mix must be normal, else it is not specific to this segment
        if case.seg:
            allm = self.data.activity_mix(start, end, {}, outcomes)
            on = sum(x["now"] for x in allm) - tn
            ob = sum(x["baseline"] for x in allm) - tb
            a_all = next(x for x in allm if x["activity"] == top["activity"])
            if on > 20 and ob > 0:
                out_now, out_base = (a_all["now"] - top["now"]) / on, (a_all["baseline"] - top["baseline"]) / ob
                self.judge(case, h, f"{top['activity']} share normal outside {fmt_seg(case.seg)}",
                           abs(out_now - out_base) < abs(delta) / 3, f"{out_base:.1%} -> {out_now:.1%} elsewhere")

    # ================================================================ conclusion
    def conclude(self, case: Case, label: str | None = None) -> Case:
        inc = case.inc
        for h in case.hyps:
            if h.status != "rejected":
                h.status = "supported" if h.posterior >= 0.8 else "open"
        best = max((h for h in case.hyps if h.status != "rejected"), key=lambda h: h.posterior, default=None)
        if label is None:
            ev = case.hyp_meta.get(best.id, {}).get("event") if best else None
            if best and ev is not None and ev.planned and best.posterior >= 0.5:
                label = "explained"
            elif best and best.posterior >= 0.8:
                label = "supported_cause"
            elif best and best.posterior >= 0.5:
                label = "hypothesis"
            else:
                label = "unexplained"
        inc.label = label
        inc.hypotheses = case.hyps
        impact = self.impact(case)
        case.notes["impact"] = impact
        inc.impact_eur_total = impact.get("contribution_eur_total", 0.0)
        from .report import render_report
        inc.report_md = render_report(self.g, case, best, impact)
        self.bus.publish("THOUGHT", "investigator", {"stage": "conclude", "text":
            f"Conclusion: {label}. First cause {inc.first_cause} in [{fmt_seg(case.seg)}]"
            + (f"; leading hypothesis {best.id} ({best.posterior:.2f}): {best.statement}" if best else "")
            + f". Impact {impact.get('contribution_eur_per_h', 0):+,.0f} EUR/h."}, inc.id)
        return case

    def impact(self, case: Case) -> dict:
        """Euro impact on contribution: GMV deviation in the segment x contribution margin."""
        out = {"hours": 0}
        prof = self.g.profile
        gmv_id, orders_id, money_id = prof["gmv_metric"], prof["orders_metric"], prof["money_metric"]
        scale = prof.get("money_scale", 1.0)
        if gmv_id not in self.g.nodes or orders_id not in self.g.nodes:
            return out
        seg = {k: v for k, v in case.seg.items() if k in self.g.nodes[gmv_id].dimensions}
        w = self.tools.window_dev(gmv_id, seg, case.start, case.end)
        oseg = {k: v for k, v in case.seg.items() if k in self.g.nodes[orders_id].dimensions}
        o = self.tools.window_dev(orders_id, oseg, case.start, case.end)
        hours = max(w.get("hours", 0), 1)
        ts_mid = case.start + (case.end - case.start) / 2
        money_base = self.mon.baseline(money_id, {}).expected(ts_mid)[0] if money_id in self.g.nodes else 0.0
        margin = money_base / max(self.mon.baseline(gmv_id, {}).expected(ts_mid)[0], 1e-9)
        d_gmv = ((w["actual"] - w["expected"]) * scale) if np.isfinite(w.get("actual", np.nan)) else 0.0
        money_total = d_gmv * margin
        # attribute only what the first cause explains: push its own deviation through the formula graph
        # (the segment's GMV can also move for other reasons, e.g. a concurrent incident)
        f = self.tools.window_dev(case.focus, case.seg, case.start, case.end)
        if np.isfinite(f.get("actual", np.nan)) and f.get("expected") and case.focus in self.g.nodes:
            per_h = self.mon.impact_eur(case.focus, case.seg, ts_mid, f["actual"], f["expected"])
            if per_h or money_id in self.mon.propagate(case.focus, 0.01, ts_mid)["relative_changes"]:
                money_total = per_h * hours
        out.update({"hours": hours, "gmv_delta_eur": d_gmv, "money_label": prof.get("money_label", "contribution"),
                    "orders_delta": (o["actual"] - o["expected"]) if np.isfinite(o.get("actual", np.nan)) else 0.0,
                    "margin": margin, "contribution_eur_total": money_total,
                    "contribution_eur_per_h": money_total / hours})
        return out

    def _candidate_events(self, onset):
        near = self.data.events(onset - timedelta(hours=3), onset + timedelta(hours=2))
        running = [e for e in self.data.events(onset - timedelta(hours=24), onset - timedelta(hours=3))
                   if e.planned and e.type == "campaign"]
        return running + near

    @staticmethod
    def _control_text(case, key, r):
        if not r.get("possible"):
            return "not testable", {}
        txt = (f"{key}={case.seg[key]}: {r['focus_rel']:+.1%} (z={r['focus_z']:+.1f}) vs other {key} values "
               f"{r['siblings_rel']:+.1%} -> {'specific to this segment' if r['confined'] else 'not specific'}")
        if "p" in r:
            txt += f" (Welch t={r['t']:.1f}, p={r['p']:.1e})"
        return txt, {k: r[k] for k in ("focus_rel", "siblings_rel", "focus_z") if k in r}

    # ================================================================ text helpers
    @staticmethod
    def _decomp_text(metric, r):
        if not r.get("possible"):
            return r["reason"], {}
        parts = ", ".join(f"{p['input']} {p['rel']:+.1%} (share {p['share']:.0%}, z={p['z']:+.1f})" for p in r["parts"])
        return f"{metric} {r['output_rel']:+.1%} = {r['expr']}: {parts}", \
            {p["input"]: round(p["share"], 3) for p in r["parts"]}

    @staticmethod
    def _drill_text(r):
        if not r.get("members"):
            return "no data", {}
        ms = r["members"][:4]
        return (f"by {r['dim']}: " + "; ".join(f"{m['member']} {m['rel']:+.1%} carries {m['dev_share']:.0%} of the change "
                                              f"with {m['vol_share']:.0%} of volume (z={m['z']:+.1f})" for m in ms),
                {m["member"]: round(m["dev_share"], 3) for m in ms})
