"""3. Investigator — ReAct brain.

Receives an alert, then loops Thought -> Action (tool) -> Observation, every step published on the bus
and stored as numbered evidence (E1, E2, ...). The policy is deterministic and explainable:

  data_check   is the data complete? (an incomplete source ends the case as a data issue)
  traverse     (v3) pruned BFS over the metric graph, upstream from the alerting metric: every visited
               node is decomposed into its formula inputs and screened against its causal parents;
               neighbours inside their noise band (|z| < 3) are pruned with their whole branch, only
               anomalous neighbours carrying part of the change are expanded. The first-cause chain is read
               deterministically off the decompositions; other anomalous branches are secondary candidates.
               (v2: greedy walk along the single largest input, feature flag bfs=False)
  localise     drill down dimension by dimension while the change concentrates (lift)
  control      Welch t-test: is the change specific to the segment, siblings normal?
  events       releases / campaigns near the onset whose scope and service fit -> hypotheses;
               events whose scope contradicts the segment are rejected with the reason
  causal       causal parents of the first cause: did they move (+ Granger)?
  predict      each hypothesis predicts timing and confinement; confirmations update its posterior
  dead end     no hypothesis reaches 0.8 or its scope cannot be verified -> ask the Meta-Agent to
               extend the graph, then re-test with the new dimensions / metrics
  conclude     supported_cause / hypothesis / explained / data_issue / unexplained, impact in EUR

Evidence anchoring (v3): every tool call writes a structured evidence record (tool, parameters, window,
entity, signal type, statistics, verdict); every prediction a hypothesis makes is judged against cited
record ids and contributes a log-likelihood ratio. Normalisation, scoring and ranking are deterministic
code; the report cites record ids instead of narrating.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from sentinel.bus import EventBus
from sentinel.contracts import Evidence, EvidenceLink, Hypothesis, seg_key

from .tools import Toolbox

LR_CONFIRM, LR_REFUTE = 3.0, 0.3


def odds_update(p: float, lr: float) -> float:
    o = p / (1 - p) * lr
    return float(o / (1 + o))


def fmt_seg(seg: dict) -> str:
    return seg_key(seg) or "total"


UNCONFIRMED_Z = 3.0      # an input must move this much for a formula output's change to count
SCOPED_RELEASE_LOOKBACK_H = 12  # how far back a release scoped exactly to the affected slice stays a suspect
Z_PRUNE = 3.0            # BFS: a neighbour inside its own noise band (|z| < 3) is pruned with its whole branch
SHARE_EXPAND = 0.2       # BFS: an anomalous formula input is expanded only if it carries >= 20% of the change
MAX_WALK_NODES = 12      # BFS budget (nodes decomposed per investigation)

STEP_OF = {"data_check": "data_check", "decompose": "decompose", "localise": "localise", "control": "localise",
           "events": "events", "causal": "predict", "predict": "predict", "meta": "frontier",
           "traverse": "traverse", "prune": "traverse"}
SIGNAL_OF = {"data_check": "data_quality", "decompose": "decomposition", "localise": "drilldown",
             "control": "control_test", "events": "event_match", "causal": "causal_test", "predict": "process_mix",
             "meta": "meta", "traverse": "prune", "prune": "prune"}
_STAT_KEYS = ("z", "p", "t", "rel", "share", "lift", "dev_share", "vol_share", "focus_rel", "siblings_rel", "focus_z",
              "granger_p", "output_rel", "residual_share", "risk", "explanatory_power")


def _jsonable(x):
    """Numbers, strings, lists and dicts only (evidence records are persisted as JSON)."""
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return None if not np.isfinite(x) else round(float(x), 6)
    if x is None or isinstance(x, str):
        return x
    return str(x)


def _stats_of(result) -> dict:
    """The test statistics of a tool result, extracted deterministically (top-level, top member, top part)."""
    out = {}
    if isinstance(result, dict):
        for k in _STAT_KEYS:
            if isinstance(result.get(k), (int, float, np.floating)) and not isinstance(result.get(k), bool):
                out[k] = result[k]
        if isinstance(result.get("top"), dict):
            out.update({f"top_{k}": v for k, v in result["top"].items() if k in _STAT_KEYS})
            out["top_member"] = result["top"].get("member")
        if result.get("parts"):
            p0 = result["parts"][0]
            out.update({"top_input": p0["input"], "top_share": p0["share"], "top_z": p0["z"]})
    return out


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

    # ================================================================ ReAct plumbing + evidence anchoring
    def record(self, case: Case, stage: str, tool: str, query: str, text: str, numbers: dict, *,
               signal: str = "", params: dict | None = None, entity: str | None = None, focus: str | None = None,
               stats: dict | None = None, verdict: str = "", window=None, derived: bool = True) -> Evidence:
        """Write one structured evidence record (deterministic; no free narration) and publish it."""
        self._ev_counter += 1
        w = window or (case.start, case.end)
        ev = Evidence(id=f"E{self._ev_counter}", incident_id=case.inc.id,
                      step=STEP_OF.get(stage, "frontier"), query=query, result=_jsonable(numbers),
                      interpretation=text, tool=tool, params=_jsonable(params or {}),
                      window=[str(pd.Timestamp(w[0])), str(pd.Timestamp(w[1]))],
                      entity=entity or f"{focus or case.focus}[{fmt_seg(case.seg)}]",
                      signal=signal or SIGNAL_OF.get(stage, stage), stats=_jsonable(stats or {}), verdict=verdict)
        case.evidence.append(ev)
        case.inc.evidence_ids.append(ev.id)
        self.bus.publish("OBSERVATION", "investigator", {
            "stage": stage, "evidence_id": ev.id, "text": text, "numbers": ev.result, "focus": focus or case.focus,
            "segment": fmt_seg(case.seg), "tool": tool, "query": query, "params": ev.params, "window": ev.window, "entity": ev.entity,
            "signal": ev.signal, "stats": ev.stats, "verdict": verdict,
            "derived": derived}, case.inc.id)      # derived: written from earlier results, no tool call of its own
        return ev

    def step(self, case: Case, stage: str, thought: str, action: str, fn, interpret, *, signal: str = "",
             params: dict | None = None, focus: str | None = None, verdict=None, window=None) -> object:
        inc_id = case.inc.id
        f = focus or case.focus
        self.bus.publish("THOUGHT", "investigator", {"stage": stage, "text": thought, "focus": f,
                                                     "segment": fmt_seg(case.seg)}, inc_id)
        self.bus.publish("ACTION", "investigator", {"stage": stage, "tool": action, "focus": f,
                                                    "segment": fmt_seg(case.seg)}, inc_id)
        result = fn()
        text, numbers = interpret(result)
        self.record(case, stage, action.split("(")[0], action, text, numbers, signal=signal,
                    params={"segment": dict(case.seg), **(params or {})}, focus=f, stats=_stats_of(result),
                    verdict=verdict(result) if callable(verdict) else (verdict or ""), window=window, derived=False)
        return result

    @staticmethod
    def last_ev(case: Case) -> list[str]:
        return [case.evidence[-1].id] if case.evidence else []

    def add_hyp(self, case: Case, statement: str, source: str, prior: float, meta: dict) -> Hypothesis:
        h = Hypothesis(id=f"H{len(case.hyps) + 1}", statement=statement, source=source, predictions=[],
                       prior=round(prior, 3), posterior=round(prior, 3))
        case.hyps.append(h)
        case.hyp_meta[h.id] = meta
        self.bus.publish("HYPOTHESIS", "investigator", {"id": h.id, "statement": statement, "source": source,
                                                        "prior": prior, "status": "open"}, case.inc.id)
        return h

    def judge(self, case: Case, h: Hypothesis, prediction: str, ok: bool | None, why: str, ev: list[str]):
        """Test one prediction of a hypothesis against the evidence records `ev` (their ids are cited)."""
        res = "confirmed" if ok else "refuted" if ok is False else "untestable"
        cite = f" [{', '.join(ev)}]" if ev else ""
        h.predictions.append(f"{prediction} -> {res}: {why}{cite}")
        lr = LR_CONFIRM if ok is True else LR_REFUTE if ok is False else 1.0
        if ok is not None:
            h.posterior = round(odds_update(h.posterior, lr), 3)
        h.evidence.append(EvidenceLink(prediction=prediction, result=res, evidence_ids=list(ev),
                                       log_lr=round(float(np.log(lr)), 3)))
        self.bus.publish("HYPOTHESIS", "investigator", {"id": h.id, "statement": h.statement, "prediction": prediction,
                                                        "result": res, "why": why, "evidence_ids": list(ev),
                                                        "posterior": h.posterior}, case.inc.id)

    # ================================================================ the investigation
    def investigate(self, inc, anomaly: dict, now) -> Case:
        now = pd.Timestamp(now)
        a_ts = pd.Timestamp(anomaly["ts"])
        case = Case(inc, anomaly, a_ts - timedelta(hours=2), now)
        if anomaly.get("frame"):
            case.notes["frame"] = anomaly["frame"]
            case.notes["claims"] = list(anomaly.get("claims") or [])
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
        if self.bfs:
            self.traverse(case, onset, now)
        else:
            self._greedy_decompose(case, onset, now)
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
                          lambda r, key=key: self._control_text(case, key, r), params={"key": key},
                          verdict=lambda r: ("confined" if r.get("confined") else "not_confined")
                          if r.get("possible") else "untestable")
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
    @property
    def bfs(self) -> bool:
        return bool(getattr(self.mon, "features", {}).get("bfs", True))

    def _flag_unconfirmed(self, case: Case, metric: str, dec: dict):
        # a formula output can look extreme when it is a small difference of large sums (contribution);
        # if no input moved outside its own noise, the "change" is amplified noise, not a problem
        if dec["parts"] and max(abs(x["z"]) for x in dec["parts"]) < UNCONFIRMED_Z:
            case.notes["unconfirmed"] = True
            self.bus.publish("THOUGHT", "investigator", {"stage": "decompose", "text":
                f"No input of {metric} moved outside its own noise (all |z| < {UNCONFIRMED_Z}): the output's "
                "deviation is amplified noise of a difference. Watch it; do not page."}, case.inc.id)

    def _decompose(self, case: Case, metric: str, onset, now, thought: str) -> dict:
        return self.step(case, "decompose", thought, f"decompose({metric}, {fmt_seg(case.seg)}, {onset}..{now})",
                         lambda: self.tools.decompose(metric, case.seg, onset, now),
                         lambda r: self._decomp_text(metric, r), focus=metric, params={"metric": metric},
                         verdict=lambda r: "decomposed" if r.get("possible") else "leaf")

    def _greedy_decompose(self, case: Case, onset, now):
        """v2 policy: follow the single largest formula input while it carries the change."""
        for depth in range(6):
            dec = self._decompose(case, case.focus, onset, now,
                                  f"Which formula input of {case.focus} explains its change in [{fmt_seg(case.seg)}]?")
            if not dec.get("possible"):
                break
            if depth == 0:
                self._flag_unconfirmed(case, case.focus, dec)
            top = dec["parts"][0]
            if top["share"] >= 0.5 and abs(top["z"]) >= 3:
                case.focus = top["input"]
                case.chain.append(top["input"])
                continue
            break

    def traverse(self, case: Case, onset, now):
        """v3 policy: pruned breadth-first search over the metric graph, upstream from the alerting metric.

        Every visited node is decomposed into its formula inputs and screened against its causal parents.
        Neighbours inside their own noise band (|z| < Z_PRUNE) are pruned together with their whole branch
        and recorded as evidence; only anomalous neighbours that carry part of the change are expanded.
        The first-cause chain is then read deterministically off the collected decompositions (largest
        input carrying >= 50% of the change, |z| >= 3), and every other anomalous branch is kept as a
        secondary candidate (compound incidents). No step is chosen by a language model."""
        from collections import deque
        walk = {"visited": [], "anomalous": [case.entry], "pruned": [], "edges": [], "secondary": []}
        decomp: dict[str, dict] = {}
        seen, q = {case.entry}, deque([(case.entry, 0)])
        while q and len(walk["visited"]) < MAX_WALK_NODES:
            node, depth = q.popleft()
            walk["visited"].append(node)
            dec = self._decompose(case, node, onset, now,
                                  f"BFS depth {depth}: which formula inputs of {node} moved in [{fmt_seg(case.seg)}]?")
            if dec.get("possible"):
                decomp[node] = dec
                if node == case.entry:
                    self._flag_unconfirmed(case, node, dec)
                normal = []
                for p in dec["parts"]:
                    if abs(p["z"]) < Z_PRUNE:
                        normal.append((p["input"], p["z"], "formula"))
                    elif abs(p["share"]) >= SHARE_EXPAND and p["input"] not in seen and depth + 1 < 6:
                        seen.add(p["input"])
                        q.append((p["input"], depth + 1))
                        walk["anomalous"].append(p["input"])
                        walk["edges"].append([p["input"], node, "formula"])
                self._prune_record(case, node, normal, walk)
            # causal parents: screen cheaply, expand only the anomalous ones
            parents = [e for e in self.g.causes(node) if e.src not in seen]
            if parents:
                screened, normal = [], []
                for e in parents:
                    cseg = {k: v for k, v in case.seg.items() if k in self.g.nodes[e.src].dimensions}
                    w = self.tools.window_dev(e.src, cseg, onset, now)
                    screened.append((e.src, w.get("z", 0.0), w.get("rel", np.nan)))
                    if abs(w.get("z", 0.0)) < Z_PRUNE:
                        normal.append((e.src, w.get("z", 0.0), "causal"))
                    elif depth + 1 < 6:
                        seen.add(e.src)
                        q.append((e.src, depth + 1))
                        walk["anomalous"].append(e.src)
                        walk["edges"].append([e.src, node, "causal"])
                self.record(case, "traverse", "screen_causal_parents",
                            f"window_dev([{', '.join(s for s, _, _ in screened)}], {fmt_seg(case.seg)}, {onset}..{now})",
                            f"causal parents of {node}: " + "; ".join(
                                f"{s} {r:+.1%} (z={z:+.1f}) {'anomalous -> expand' if abs(z) >= Z_PRUNE else 'normal -> prune'}"
                                for s, z, r in screened),
                            {s: round(float(z), 2) for s, z, _ in screened}, signal="prune", focus=node,
                            params={"node": node, "parents": [s for s, _, _ in screened]},
                            stats={"anomalous": sum(abs(z) >= Z_PRUNE for _, z, _ in screened),
                                   "pruned": len(normal)},
                            verdict="expanded" if len(normal) < len(screened) else "all_pruned",
                            window=(onset, now))
                walk["pruned"] += [s for s, _, _ in normal]
        # first-cause chain read off the decompositions (deterministic)
        while True:
            dec = decomp.get(case.focus)
            if dec is None and len(case.chain) > 1 and len(case.chain) < 7:
                dec = self._decompose(case, case.focus, onset, now,       # beyond the BFS budget: finish the chain
                                      f"Which formula input of {case.focus} explains its change?")
            if not dec or not dec.get("possible"):
                break
            top = dec["parts"][0]
            if top["share"] >= 0.5 and abs(top["z"]) >= 3 and top["input"] not in case.chain:
                case.focus = top["input"]
                case.chain.append(top["input"])
                continue
            break
        walk["secondary"] = [m for m in walk["anomalous"] if m not in case.chain]
        case.notes["walk"] = walk
        self.bus.publish("GRAPH_WALK", "investigator", {
            "entry": case.entry, "segment": fmt_seg(case.seg), "visited": walk["visited"], "pruned": walk["pruned"],
            "anomalous": walk["anomalous"], "edges": walk["edges"], "chain": case.chain,
            "secondary": walk["secondary"]}, case.inc.id)

    def _prune_record(self, case: Case, node: str, normal: list, walk: dict):
        if not normal:
            return
        walk["pruned"] += [m for m, _, _ in normal]
        self.record(case, "prune", "prune", f"prune(inputs of {node}, |z| < {Z_PRUNE})",
                    f"pruned {len(normal)} normal input(s) of {node}: " +
                    ", ".join(f"{m} (z={z:+.1f})" for m, z, _ in normal) + "; their branches are not explored",
                    {m: round(float(z), 2) for m, z, _ in normal}, signal="prune", focus=node,
                    params={"node": node, "threshold_z": Z_PRUNE}, stats={"pruned": len(normal)}, verdict="normal")

    def localise(self, case: Case, start, end):
        for _ in range(3):
            dims = [d for d in self.g.nodes[case.focus].dimensions if d not in case.seg]
            best = None
            for d in dims:
                r = self.step(case, "localise", f"Where is the change of {case.focus} concentrated? Split by {d}.",
                              f"drilldown({case.focus}, {fmt_seg(case.seg)}, by={d})",
                              lambda d=d: self.tools.drilldown(case.focus, case.seg, d, start, end),
                              lambda r: self._drill_text(r), params={"by": d},
                              verdict=lambda r: ("concentrated" if r.get("top") and abs(r["top"]["z"]) >= 3 and (
                                  (r["top"]["dev_share"] >= 0.6 and r["top"]["lift"] >= 1.5) or
                                  (r["top"]["dev_share"] >= 0.8 and all(abs(m["z"]) < 2 for m in r["members"]
                                                                       if m["member"] != r["top"]["member"])))
                                                 else "spread"))
                top = r.get("top")
                others_normal = top is not None and all(abs(m["z"]) < 2 for m in r.get("members", [])
                                                        if m["member"] != top["member"])
                if top and abs(top["z"]) >= 3 and ((top["dev_share"] >= 0.6 and top["lift"] >= 1.5)
                                                   or (top["dev_share"] >= 0.8 and others_normal)):
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
                        lambda: self._candidate_events(onset, case.seg),
                        lambda r: ((f"{len(r)} events: " + "; ".join(f"{e.ts:%H:%M} {e.type} {e.service or ''} "
                                                                       f"'{e.description}' scope={fmt_seg(e.scope)}" for e in r))
                                   if r else "no events near the onset", {"events": len(r)}),
                        params={"from": str(onset - timedelta(hours=3)), "to": str(onset + timedelta(hours=1))},
                        verdict=lambda r: f"{len(r)} candidate(s)")
        ev_events = self.last_ev(case)
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
                    ended = self.mon.planned_end(e) if self.mon.features.get("calibrate", True) else None
                    running = 0 <= dt and (onset < ended if ended is not None else dt <= 24)
                    self.judge(case, h, "onset falls inside the running campaign window", running,
                               f"campaign started {e.ts:%m-%d %H:%M}, onset {onset:%m-%d %H:%M}"
                               + (f", campaign effect ended {ended:%m-%d %H:%M} (measured)" if ended is not None
                                  else ""), ev_events)
                elif 3 < dt <= SCOPED_RELEASE_LOOKBACK_H and e.scope:
                    self.judge(case, h, "release scoped to the affected slice precedes a slow onset", True,
                               f"release {e.ts:%m-%d %H:%M}, onset {onset:%m-%d %H:%M} ({dt:.0f} h later); "
                               f"scope {fmt_seg(e.scope)} = affected slice", ev_events)
                else:
                    self.judge(case, h, "onset coincides with the event (<= 1h)", abs(dt) <= 1.0,
                               f"onset {onset:%H:%M}, event {e.ts:%H:%M}", ev_events)
            if h.status == "rejected":
                continue
            # confinement: scope keys must be verified by localisation
            unverifiable = [k for k in e.scope if k not in case.seg and k not in self.g.nodes[case.focus].dimensions]
            for k, v in e.scope.items():
                if any(p.startswith(f"change concentrated in {k}=") for p in h.predictions):
                    continue
                if k in case.seg and case.seg[k] == v:
                    ctl = self.step(case, "control", f"{h.id}: is the change confined to {k}={v}, as the event's scope "
                                    "predicts?", f"compare_control({case.focus}, {fmt_seg(case.seg)}, vs other {k})",
                                    lambda k=k: self.tools.compare_control(case.focus, case.seg, k, start, end),
                                    lambda r, k=k: self._control_text(case, k, r), params={"key": k},
                                    verdict=lambda r: "confined" if r.get("confined") else "not_confined")
                    self.judge(case, h, f"change concentrated in {k}={v}, other {k} values normal",
                               bool(ctl.get("confined")),
                               f"{ctl.get('focus_rel', float('nan')):+.1%} vs other {k} "
                               f"{ctl.get('siblings_rel', float('nan')):+.1%}", self.last_ev(case))
                elif k not in case.seg and k in self.g.nodes[case.focus].dimensions:
                    dd = self.step(case, "localise", f"{h.id}: does the change concentrate in the event's scope {k}={v}?",
                                   f"drilldown({case.focus}, {fmt_seg(case.seg)}, by={k})",
                                   lambda k=k: self.tools.drilldown(case.focus, case.seg, k, start, end),
                                   lambda r: self._drill_text(r), params={"by": k})
                    me = next((m for m in dd["members"] if m["member"] == v), None)
                    ok = bool(me and me["lift"] >= 1.3 and me["dev_share"] >= 0.4)
                    self.judge(case, h, f"change concentrated in {k}={v} (event scope)", ok,
                               (f"{v} carries {me['dev_share']:.0%} of the change with {me['vol_share']:.0%} of volume"
                                if me else "member not found"), self.last_ev(case))
            if e.planned and e.scope and not any(p.startswith("change concentrated in") for p in h.predictions) \
                    and not any(p.startswith("scope of the planned change") for p in h.predictions):
                # the focus cannot be split by the plan's scope: check it on the metrics that compose the focus
                proxy = next((v.src for v in self.g.inputs(case.focus) if all(
                    k in self.g.nodes[v.src].dimensions for k in list(e.scope) + list(case.seg))), None)
                if proxy:
                    k, v = next(iter(e.scope.items()))
                    dd = self.step(case, "localise", f"{h.id}: {case.focus} cannot be split by {k}; check the planned "
                                   f"scope on its input {proxy}.", f"drilldown({proxy}, {fmt_seg(case.seg)}, by={k})",
                                   lambda proxy=proxy, k=k: self.tools.drilldown(proxy, case.seg, k, start, end),
                                   lambda r: self._drill_text(r), params={"by": k, "metric": proxy}, focus=proxy)
                    me = next((m for m in dd["members"] if m["member"] == v), None)
                    ok = bool(me and me["lift"] >= 1.3 and me["dev_share"] >= 0.4)
                    self.judge(case, h, f"scope of the planned change checked on input {proxy}: concentrated in {k}={v}",
                               ok, (f"{v} carries {me['dev_share']:.0%} of {proxy}'s change with {me['vol_share']:.0%} "
                                    f"of volume") if me else "member not found", self.last_ev(case))
                else:
                    ev = self.record(case, "events", "graph.dimensions", f"dimensions({case.focus} and its inputs)",
                                     f"{case.focus} and its formula inputs have no dimension {sorted(e.scope)}",
                                     {"missing": len(e.scope)}, signal="graph_structure",
                                     params={"metric": case.focus, "scope": e.scope}, verdict="unverifiable")
                    self.judge(case, h, "scope of the planned change can be checked on this metric", False,
                               f"{case.focus} and its inputs cannot be split by {sorted(e.scope)}: timing alone "
                               "does not explain it", [ev.id] + ev_events)
            if unverifiable and not rerun:
                if any(k not in self.g.nodes[case.focus].dimensions for k in unverifiable):
                    case.dead_end_reasons.append(
                        f"cannot verify scope {fmt_seg({k: e.scope[k] for k in unverifiable})} of '{e.description}': "
                        f"{case.focus} has no dimension {unverifiable}")
            if e.planned and e.expected_effect:
                h.statement += " (planned; expected effect " + ", ".join(f"{k} x{v}" for k, v in e.expected_effect.items()) + ")"
            mech = e.service in linked if e.service else False
            if mech and not any(p.startswith("mechanism") for p in h.predictions):
                ev = self.record(case, "events", "graph.services", f"services({case.focus} and its causes)",
                                 f"{e.service} owns a metric on the path to {case.focus} (context edges)",
                                 {"linked": 1}, signal="graph_structure",
                                 params={"service": e.service, "metric": case.focus}, verdict="linked")
                self.judge(case, h, "mechanism: the released service is linked to the first cause in the graph", True,
                           f"{e.service} -> {case.focus} via context edges", [ev.id])
        # causal parents of the first cause; with BFS, parents inside their noise band are pruned before any
        # hypothesis is formed (hypotheses only on anomalous branches)
        pruned = []
        for edge in self.g.causes(case.focus):
            if any(case.hyp_meta[h.id].get("cause") == edge.src for h in case.hyps):
                continue
            if self.bfs:
                cseg = {k: v for k, v in case.seg.items() if k in self.g.nodes[edge.src].dimensions}
                z = self.tools.window_dev(edge.src, cseg, start, end).get("z", 0.0)
                if abs(z) < Z_PRUNE:
                    pruned.append((edge.src, z, "causal"))
                    continue
            r = self.step(case, "causal", f"Causal parent {edge.src} ({edge.sign}, conf {edge.confidence:.2f}): did it move first?",
                          f"causal_check({edge.src} -> {case.focus}, {fmt_seg(case.seg)})",
                          lambda edge=edge: self.tools.causal_check(edge.src, case.focus, edge.sign, case.seg, start, end,
                                                                    focus_dev["rel"]),
                          lambda r: (f"{r['cause']} [{r['segment'] or 'total'}] {r['rel']:+.1%} (z={r['z']:+.1f}); "
                                     f"{'consistent with' if r['consistent'] else 'does not explain'} the change"
                                     + (f"; Granger p={r['granger_p']:.2g}" if 'granger_p' in r else ""),
                                     {k: r[k] for k in ("rel", "z") if k in r}),
                          params={"cause": edge.src, "effect": case.focus, "sign": edge.sign},
                          verdict=lambda r: "consistent" if r["consistent"] else "inconsistent")
            ev_c = self.last_ev(case)
            h = self.add_hyp(case, f"{edge.src} moved and drove {case.focus} ({edge.sign})", "causal_edge",
                             min(0.5, edge.confidence * 0.6), {"cause": edge.src, "ev": ev_c})
            self.judge(case, h, f"{edge.src} moved in the same segment with the expected sign", r["consistent"],
                       f"{r['rel']:+.1%}, z={r['z']:+.1f}", ev_c)
            if r.get("granger_p") is not None and r["consistent"]:
                self.judge(case, h, "Granger precedence", r["granger_p"] < 0.01, f"p={r['granger_p']:.2g}", ev_c)
            if not r["consistent"]:
                h.status = "rejected"
        self._test_claims(case, start, end)
        if pruned:
            self._prune_record(case, f"{case.focus} (causal parents)", pruned, case.notes.setdefault(
                "walk", {"visited": [], "anomalous": [], "pruned": [], "edges": [], "secondary": []}))
        # mechanism chain: an event whose service owns a cause that did move -> event -> cause -> first cause
        moved = [case.hyp_meta[h.id]["cause"] for h in case.hyps
                 if case.hyp_meta[h.id].get("cause") and h.status != "rejected" and h.posterior >= 0.5]
        for h in case.hyps:
            e = case.hyp_meta[h.id].get("event")
            if e is None or h.status == "rejected" or not e.service:
                continue
            for c in moved:
                if e.service in self.g.services(c) and not any(p.startswith("mechanism chain") for p in h.predictions):
                    cause_h = next(x for x in case.hyps if case.hyp_meta[x.id].get("cause") == c)
                    self.judge(case, h, f"mechanism chain {e.service} -> {c} -> {case.focus}", True,
                               f"{c} belongs to {e.service} and moved with the expected sign at the onset",
                               case.hyp_meta[cause_h.id].get("ev", []))

    def _test_claims(self, case: Case, start, end):
        """Causes the operator's text claims, tested as hypotheses on the data. A claim the telemetry cannot
        identify stays unresolved at its prior; the text itself is never evidence."""
        for c in case.notes.get("claims") or []:
            if c.get("tested"):
                continue
            c["tested"] = True
            h = self.add_hyp(case, f"operator premise: {c['phrase']}", "premise", 0.2, {"claim": c})
            d, v = c.get("dimension"), c.get("value")
            if d and v and d in self.g.nodes[case.focus].dimensions:
                parent = {k: x for k, x in case.seg.items() if k != d}
                dd = self.step(case, "localise", f"{h.id}: does the change concentrate in {d}={v}, as the text claims?",
                               f"drilldown({case.focus}, {fmt_seg(parent)}, by={d})",
                               lambda: self.tools.drilldown(case.focus, parent, d, start, end),
                               lambda r: self._drill_text(r), params={"by": d, "claim": c["phrase"]})
                me = next((m for m in dd["members"] if str(m["member"]) == str(v)), None)
                ok = bool(me and me["lift"] >= 1.3 and me["dev_share"] >= 0.4 and abs(me["z"]) >= 3)
                self.judge(case, h, f"change concentrated in {d}={v}", ok,
                           (f"{v} carries {me['dev_share']:.0%} of the change with {me['vol_share']:.0%} of volume"
                            if me else f"{d}={v} not present"), self.last_ev(case))
                continue
            if c.get("kind") == "release":
                evs = self._candidate_events(pd.Timestamp(case.notes.get("onset", start)), case.seg)
                words = {w for w in c["phrase"].lower().replace("-", " ").split() if len(w) > 3}
                hit = [e for e in evs if e.type == "release" and words & set(
                    f"{e.service or ''} {e.description}".lower().replace("-", " ").split())]
                ev = self.record(case, "events", "match_claim", f"events near onset vs claim '{c['phrase']}'",
                                 (f"recorded release(s) matching the claim: " + "; ".join(
                                     f"{e.ts:%H:%M} {e.service} '{e.description}'" for e in hit)) if hit else
                                 "no recorded or measured release matches the claim", {"matches": len(hit)},
                                 signal="event_match", params={"claim": c["phrase"]},
                                 verdict="match" if hit else "no_match")
                self.judge(case, h, "a release matching the claim was recorded near the onset", bool(hit),
                           f"{len(hit)} matching release(s)", [ev.id])
                continue
            ev = self.record(case, "events", "claim_identifiable", f"catalog dimensions vs claim '{c['phrase']}'",
                             f"no metric, dimension or event in the telemetry identifies '{c['phrase']}': "
                             "left unresolved", {"identifiable": 0}, signal="graph_structure",
                             params={"claim": c["phrase"]}, verdict="unverifiable")
            self.judge(case, h, "the telemetry can identify the claimed cause", None,
                       "no dimension or event identifies it; the premise stays a hypothesis", [ev.id])

    def mechanism(self, case: Case, start, end):
        """Which concrete events changed inside the segment? (e.g. payment_timed_out share 2% -> 63%).
        This is the observed failure mechanism, not yet its underlying cause."""
        acts = self.data.activities_for(case.focus)
        if len(acts) < 2:
            return
        denominators = {"payment_attempted", "session_started", "search_requested", "feed_impression"}
        calc = getattr(self.data, "catalog", {}).get(case.focus, {}).get("calculation", {})
        if calc.get("denominator_activity"):
            denominators.add(calc["denominator_activity"])   # e.g. checkout_started for checkout completion
        outcomes = [a for a in acts if a not in denominators] or acts
        mix = self.step(case, "predict", f"Which events behind {case.focus} changed inside {fmt_seg(case.seg)}?",
                        f"activity_mix({outcomes}, {fmt_seg(case.seg)}, now vs same hours on 7 previous days)",
                        lambda: self.data.activity_mix(start, end, case.seg, outcomes),
                        lambda r: ("; ".join(f"{x['activity']} {x['now']} vs {x['baseline']:.0f} usual" for x in r),
                                   {x["activity"]: x["now"] for x in r}), signal="process_mix")
        ev_mix = self.last_ev(case)
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
                   f"{share_base:.1%} -> {share_now:.1%} ({top['now']} events)", ev_mix + self.last_ev(case))
        # outside the segment the mix must be normal, else it is not specific to this segment
        if case.seg:
            allm = self.step(case, "predict", f"Is the {top['activity']} share normal outside {fmt_seg(case.seg)}?",
                             f"activity_mix({outcomes}, total, now vs same hours on 7 previous days)",
                             lambda: self.data.activity_mix(start, end, {}, outcomes),
                             lambda r: ("; ".join(f"{x['activity']} {x['now']} vs {x['baseline']:.0f} usual" for x in r),
                                        {x["activity"]: x["now"] for x in r}), signal="process_mix")
            on = sum(x["now"] for x in allm) - tn
            ob = sum(x["baseline"] for x in allm) - tb
            a_all = next(x for x in allm if x["activity"] == top["activity"])
            if on > 20 and ob > 0:
                out_now, out_base = (a_all["now"] - top["now"]) / on, (a_all["baseline"] - top["baseline"]) / ob
                self.judge(case, h, f"{top['activity']} share normal outside {fmt_seg(case.seg)}",
                           abs(out_now - out_base) < abs(delta) / 3, f"{out_base:.1%} -> {out_now:.1%} elsewhere",
                           self.last_ev(case))

    # ================================================================ conclusion
    def conclude(self, case: Case, label: str | None = None) -> Case:
        inc = case.inc
        by_id = {e.id: e for e in case.evidence}
        for h in case.hyps:
            if h.status != "rejected":
                h.status = "supported" if h.posterior >= 0.8 else "open"
            # deterministic hybrid score: the posterior, weighted by how many independent kinds of evidence
            # confirmed the hypothesis (timing alone < timing + scope + mechanism)
            kinds = {by_id[i].signal for link in h.evidence if link.result == "confirmed"
                     for i in link.evidence_ids if i in by_id}
            h.score = round(h.posterior * (0.85 + 0.05 * min(3, len(kinds))), 4) if h.status != "rejected" else 0.0
        # ranking: posterior first (the labels' thresholds), the hybrid score breaks ties
        # an untested operator premise is listed, never presented as the leading explanation
        cands = [h for h in case.hyps if h.status != "rejected" and not (
            h.source == "premise" and not any(link.result == "confirmed" for link in h.evidence))]
        best = max(cands, key=lambda h: (h.posterior, h.score), default=None)
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
        money_total, money_from = d_gmv * margin, "gmv_margin"
        # attribute only what the first cause explains: push its own deviation through the formula graph
        # (the segment's GMV can also move for other reasons, e.g. a concurrent incident)
        f = self.tools.window_dev(case.focus, case.seg, case.start, case.end)
        if np.isfinite(f.get("actual", np.nan)) and f.get("expected") and case.focus in self.g.nodes:
            per_h = self.mon.impact_eur(case.focus, case.seg, ts_mid, f["actual"], f["expected"])
            if per_h or money_id in self.mon.propagate(case.focus, 0.01, ts_mid)["relative_changes"]:
                money_total, money_from = per_h * hours, "propagation"
        targets = getattr(self.mon, "calibration", {}).get("target_metrics", set())
        near = {case.focus, *case.chain} | {e.src for e in self.g.causes(case.focus)}
        for t in sorted(targets & near):
            tseg = {k: v for k, v in case.seg.items() if k in self.g.nodes[t].dimensions}
            tw = self.tools.window_dev(t, tseg, case.start, case.end)
            if np.isfinite(tw.get("actual", np.nan)) and abs(tw.get("z", 0.0)) >= 3 and tw.get("volume"):
                if self.mon.features.get("gate", True) and tw["actual"] < tw["expected"]:
                    # underspend only matters when there was budget to spend: at night the plan is tiny
                    typical = self.mon.baseline(t, tseg).overall_den
                    if typical and tw["volume"] / max(hours, 1) < 0.5 * typical:
                        out["target_context"] = (f"{t} below plan, but planned volume {tw['volume'] / max(hours, 1):,.0f}"
                                                 f"/h is under half its usual {typical:,.0f}/h: not harm")
                        continue
                harm = abs(tw["actual"] - tw["expected"]) * tw["volume"]          # e.g. EUR over / under plan
                out["target_harm"] = {"metric": t, "segment": fmt_seg(tseg), "eur_total": harm,
                                      "eur_per_h": harm / hours, "actual": tw["actual"], "expected": tw["expected"]}
                money_total = min(money_total, -harm)       # alert on it even if the marketplace earns more
                break
        out.update({"hours": hours, "gmv_delta_eur": d_gmv, "money_label": prof.get("money_label", "contribution"),
                    "orders_delta": (o["actual"] - o["expected"]) if np.isfinite(o.get("actual", np.nan)) else 0.0,
                    "margin": margin, "money_from": money_from, "contribution_eur_total": money_total,
                    "contribution_eur_per_h": money_total / hours})
        return out

    def _candidate_events(self, onset, seg: dict | None = None):
        """Events near the onset, campaigns still running, and earlier releases scoped exactly to the
        affected slice: a harm that builds up slowly (a model that degrades conversion over hours) is
        detected long after its release, which is still the prime suspect when its scope matches."""
        near = self.data.events(onset - timedelta(hours=3), onset + timedelta(hours=2))
        look = SCOPED_RELEASE_LOOKBACK_H if self.mon.features.get("calibrate", True) else 24
        earlier = self.data.events(onset - timedelta(hours=look), onset - timedelta(hours=3))
        running = [e for e in earlier if e.planned and e.type == "campaign"
                   and onset - pd.Timestamp(e.ts) <= timedelta(hours=24)]
        scoped = [e for e in earlier if not e.planned and e.type in ("release", "config") and e.scope and seg
                  and all(seg.get(k) == v for k, v in e.scope.items())
                  and self.mon.features.get("calibrate", True)]
        return running + scoped + near

    @staticmethod
    def _control_text(case, key, r):
        if not r.get("possible"):
            return "not testable", {}
        verdict = "specific to this segment" if r["confined"] else "not specific"
        if r["confined"] and r.get("significant") is False:
            verdict = "larger here than in the other values, but not statistically significant across hours"
        txt = (f"{key}={case.seg[key]}: {r['focus_rel']:+.1%} (z={r['focus_z']:+.1f}) vs other {key} values "
               f"{r['siblings_rel']:+.1%} -> {verdict}")
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
