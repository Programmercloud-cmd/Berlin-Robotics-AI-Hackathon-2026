"""1. Monitor Engine — the eyes.

Each tick (one simulated hour):
  attention   choose what to look at: every metric at total, one rotating dimension per metric,
              plus the dimensions named by recent events (a release on platform=android -> look by platform)
  quality     an hour whose source data is < 95% complete is held back (DATA_ISSUE), not judged
  baseline    robust seasonal expectation in local time (monitor/baseline.py)
  innocent    planned events with an expected effect (campaign: sessions x1.4) scale the expected
              volume in their scope; a change that only the plan explains is INNOCENT_CHANGE, not an alert
  detect      spike (|z| >= 4.5 with enough support) or CUSUM (sustained shift)
  alert       series that turn anomalous emit ANOMALY_DETECTED with a euro impact estimate;
              series that stay normal for 3 complete hours emit SERIES_RECOVERED
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

import numpy as np
import pandas as pd

from sentinel.brain.graph import BrainGraph
from sentinel.bus import EventBus
from sentinel.contracts import seg_key

from .baseline import SeriesBaseline

SPIKE_Z, CUSUM_K, CUSUM_H, RECOVER_HOURS, LOOKBACK_H = 4.5, 1.0, 8.0, 3, 6
CAMPAIGN_DEFAULT_HOURS = 24        # Event has no end time in the contract: campaigns are assumed to last a day
KEY_METRICS = ["contribution", "gmv", "orders", "conversion_rate", "payment_success_rate", "sessions"]


def seg_from_key(key: str) -> dict:
    return dict(p.split("=", 1) for p in key.split("|")) if key else {}


class MonitorEngine:
    def __init__(self, graph: BrainGraph, data, bus: EventBus, history_start, history_end):
        self.g, self.data, self.bus = graph, data, bus
        self.h0, self.h1 = pd.Timestamp(history_start), pd.Timestamp(history_end)
        self.baselines: dict[tuple[str, str], SeriesBaseline] = {}
        self.evaluated: set[tuple[str, str, pd.Timestamp]] = set()
        self.pending_dq: set[tuple[str, pd.Timestamp]] = set()
        self.state: dict[tuple[str, str], dict] = defaultdict(lambda: {"anomalous": False, "normal_run": 0,
                                                                        "cpos": 0.0, "cneg": 0.0})
        self.series: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.innocent_seen: set[tuple[str, str, str]] = set()
        self.tick_no = 0

    # ================================================================ baselines
    def fit(self, metrics: list[str] | None = None):
        n = 0
        for m in metrics or list(self.g.nodes):
            node = self.g.nodes[m]
            for dim in [None] + list(node.dimensions):
                gb = [dim] if dim else []
                df = self.data.metric(m, self.h0, self.h1, group_by=gb)
                groups = df.groupby(dim) if dim else [(None, df)]
                for member, part in groups:
                    seg = {dim: str(member)} if dim else {}
                    self.baselines[(m, seg_key(seg))] = SeriesBaseline(node.kind, node.unit, part)
                    n += 1
        self.bus.publish("SYSTEM", "monitor", {"message": f"fitted {n} seasonal baselines (local hour x day type)",
                                               "history": [str(self.h0), str(self.h1)]})

    def baseline(self, metric: str, segment: dict) -> SeriesBaseline:
        key = (metric, seg_key(segment))
        if key not in self.baselines:
            node = self.g.nodes[metric]
            df = self.data.metric(metric, self.h0, self.h1, filters=segment)
            self.baselines[key] = SeriesBaseline(node.kind, node.unit, df)
        return self.baselines[key]

    # ================================================================ planned events (innocent changes)
    def _active_planned(self, ts) -> list:
        evs = self.data.events(ts - timedelta(hours=CAMPAIGN_DEFAULT_HOURS), ts + timedelta(hours=1))
        return [e for e in evs if e.planned and e.expected_effect and
                pd.Timestamp(e.ts) <= ts < pd.Timestamp(e.ts) + timedelta(hours=CAMPAIGN_DEFAULT_HOURS)]

    def planned_factor(self, metric: str, segment: dict, ts) -> tuple[float, list[str]]:
        """Expected-volume multiplier from planned events. Traffic effects (sessions) scale every volume
        metric in scope; rates are expected unchanged."""
        node = self.g.nodes[metric]
        if node.kind not in ("count", "sum"):
            return 1.0, []
        f_total, why = 1.0, []
        for ev in self._active_planned(ts):
            f = ev.expected_effect.get(metric)
            if f is None:
                for src, fx in ev.expected_effect.items():   # volume rule: only metrics fed by the same traffic
                    if src in self.g.nodes and set(self.g.tables_of(metric)) & self._coupled_tables(src):
                        f = fx
                        break
            if f is None:
                continue
            scope = ev.scope or {}
            if any(k in segment and segment[k] != v for k, v in scope.items()):
                continue                                        # segment outside the event scope
            if all(segment.get(k) == v for k, v in scope.items()):
                eff = f                                         # segment inside the scope
            else:                                               # broader segment: share-weighted
                proxy = metric if all(k in node.dimensions for k in scope) else "sessions"
                inner = {**{k: v for k, v in segment.items() if k in self.g.nodes[proxy].dimensions}, **scope}
                outer = {k: v for k, v in segment.items() if k in self.g.nodes[proxy].dimensions}
                share = self.baseline(proxy, inner).expected(ts)[0] / max(self.baseline(proxy, outer).expected(ts)[0], 1e-9)
                eff = 1 + float(np.clip(share, 0, 1)) * (f - 1)
            f_total *= eff
            why.append(f"{ev.description or ev.type} ({ev.id}) x{eff:.2f}")
        return f_total, why

    def _coupled_tables(self, metric: str) -> set[str]:
        """Tables whose volumes move with `metric`'s table: linked by a ratio that divides one by the other
        (fact_commerce orders / fact_traffic sessions). Ads and inventory tables are not traffic-coupled."""
        if not hasattr(self, "_table_links"):
            links: dict[str, set[str]] = defaultdict(set)
            for n in self.g.nodes.values():
                ts = set(self.g.tables_of(n.id)) if (n.value_sql or n.numerator_sql) else set()
                for a in ts:
                    links[a] |= ts
            self._table_links = links
        seen, todo = set(), list(self.g.tables_of(metric))
        while todo:
            t = todo.pop()
            if t not in seen:
                seen.add(t)
                todo += list(self._table_links.get(t, ()))
        return seen

    # ================================================================ attention
    def watch_plan(self, ts) -> list[tuple[str, list[str], list[str]]]:
        plan: dict[tuple[str, tuple], list[str]] = defaultdict(list)
        keys = self.g.profile.get("key_metrics", KEY_METRICS)
        for i, m in enumerate(self.g.nodes):
            plan[(m, ())].append("impact" if m in keys else "coverage")
            dims = self.g.nodes[m].dimensions
            if dims:
                plan[(m, (dims[(self.tick_no + i) % len(dims)],))].append("rotation")
        for ev in self.data.events(ts - timedelta(hours=LOOKBACK_H), ts):
            if not ev.scope:
                continue
            for m, node in self.g.nodes.items():
                linked = ev.service and ev.service in self.g.services(m)
                if linked or m in keys:
                    for d in ev.scope:
                        if d in node.dimensions:
                            plan[(m, (d,))].append(f"recent {ev.type}: {ev.description}")
        return [(m, list(gb), reasons) for (m, gb), reasons in plan.items()]

    # ================================================================ tick
    def tick(self, now) -> list[dict]:
        now = pd.Timestamp(now)
        self.tick_no += 1
        start = now - timedelta(hours=LOOKBACK_H)
        plan = self.watch_plan(now)
        anomalies, evaluated, held = [], 0, 0
        for ev in self.data.events(now - timedelta(hours=1), now):
            self.bus.publish("SYSTEM", "monitor", {"message": f"new event: {ev.type} {ev.service or ''} "
                                                   f"{ev.description}", "event": ev.model_dump(mode="json")})
        for m, gb, reasons in plan:
            if any(r.startswith("recent") for r in reasons):
                self.bus.publish("WATCH", "monitor", {"metric": m, "by": gb, "reasons": reasons})
            df = self.data.metric(m, start, now, group_by=gb)
            if df.empty:
                continue
            groups = df.groupby(gb[0]) if gb else [(None, df)]
            for member, part in groups:
                seg = {gb[0]: str(member)} if gb else {}
                for row in part.itertuples():
                    ts = pd.Timestamp(row.ts)
                    key = (m, seg_key(seg), ts)
                    if key in self.evaluated:
                        continue
                    if not row.complete:
                        held += 1
                        for t in self.g.tables_of(m):
                            c = self.data.completeness(t, ts)
                            if c < 0.95 and (t, ts) not in self.pending_dq:
                                self.pending_dq.add((t, ts))
                                self.bus.publish("DATA_ISSUE", "monitor", {
                                    "table": t, "hour": str(ts), "completeness": round(c, 3),
                                    "action": "held back: not judged until the partition is complete"})
                        continue
                    self.evaluated.add(key)
                    evaluated += 1
                    a = self._judge(m, seg, ts, row)
                    if a:
                        anomalies.append(a)
        for (t, ts) in list(self.pending_dq):
            if self.data.completeness(t, ts) >= 0.95:
                self.pending_dq.discard((t, ts))
                self.bus.publish("DATA_ISSUE", "monitor", {"table": t, "hour": str(ts), "completeness": 1.0,
                                                           "action": "partition now complete; re-evaluated"})
        reasons = defaultdict(int)
        for _, _, rs in plan:
            for r in rs:
                reasons[r.split(":")[0]] += 1
        self.bus.publish("MONITOR_TICK", "monitor", {"watched_series": len(plan), "hours_evaluated": evaluated,
                                                     "held_incomplete": held, "anomalies": len(anomalies),
                                                     "reasons": dict(reasons)})
        return anomalies

    def _judge(self, m: str, seg: dict, ts, row) -> dict | None:
        node = self.g.nodes[m]
        b = self.baseline(m, seg)
        if b.n_hist < 50 or np.isnan(row.value):
            return None
        factor, why = self.planned_factor(m, seg, ts)
        s = b.score(ts, row.value, row.denominator, factor)
        raw = b.score(ts, row.value, row.denominator, 1.0) if factor != 1.0 else s
        skey = (m, seg_key(seg))
        self.series[skey].append({"ts": str(ts), "actual": row.value, "expected": s["expected"],
                                  "low": s["low"], "high": s["high"], "z": s["z"]})
        support = row.denominator >= 100 if node.kind in ("ratio", "gauge") else s["expected"] >= 20
        st = self.state[skey]
        z = float(np.clip(s["z"], -8, 8))
        st["cpos"] = max(0.0, st["cpos"] + z - CUSUM_K)
        st["cneg"] = max(0.0, st["cneg"] - z - CUSUM_K)
        spike = support and abs(s["z"]) >= SPIKE_Z
        drift = support and max(st["cpos"], st["cneg"]) >= CUSUM_H
        if factor != 1.0 and abs(raw["z"]) >= SPIKE_Z and not (spike or drift):
            k = (m, seg_key(seg), why[0])
            if k not in self.innocent_seen:
                self.innocent_seen.add(k)
                self.bus.publish("INNOCENT_CHANGE", "monitor", {
                    "metric": m, "segment": seg_key(seg), "hour": str(ts), "actual": row.value,
                    "expected_without_plan": raw["expected"], "expected_with_plan": s["expected"],
                    "z_without_plan": raw["z"], "z_with_plan": s["z"], "explained_by": why})
        if spike or drift:
            st["normal_run"] = 0
            if not st["anomalous"]:
                st["anomalous"] = True
                st["cpos"] = st["cneg"] = 0.0
                a = {"metric": m, "segment": seg_key(seg), "ts": str(ts), "actual": row.value,
                     "expected": s["expected"], "low": s["low"], "high": s["high"], "z": s["z"],
                     "detector": "spike" if spike else "cusum", "direction": "up" if s["z"] > 0 else "down",
                     "good_direction": node.good_direction, "planned_adjustment": why,
                     "impact_eur_per_h": self.impact_eur(m, seg, ts, row.value, s["expected"])}
                self.bus.publish("ANOMALY_DETECTED", "monitor", a)
                return a
        else:
            if st["anomalous"]:
                st["normal_run"] += 1
                if st["normal_run"] >= RECOVER_HOURS:
                    st["anomalous"] = False
                    self.bus.publish("SERIES_RECOVERED", "monitor", {"metric": m, "segment": seg_key(seg),
                                                                     "hour": str(ts)})
        return None

    # ================================================================ euro impact
    def impact_eur(self, metric: str, seg: dict, ts, actual: float, expected: float) -> float:
        """Propagate a relative change through the formula graph to contribution (EUR per hour)."""
        rel = actual / expected - 1 if expected else 0.0
        if not np.isfinite(rel) or rel == 0:
            return 0.0
        share = 1.0
        if seg:  # translate a segment change into a total-level change by the segment's weight
            tot = self.baseline(metric, {})
            part = self.baseline(metric, seg)
            w_seg = part.expected(ts)[1] if self.g.nodes[metric].kind in ("ratio", "gauge") else part.expected(ts)[0]
            w_tot = tot.expected(ts)[1] if self.g.nodes[metric].kind in ("ratio", "gauge") else tot.expected(ts)[0]
            share = float(np.clip(w_seg / max(w_tot, 1e-9), 0, 1))
        return float(self.propagate(metric, rel * share, ts).get("money_eur", 0.0))

    def propagate(self, metric: str, rel: float, ts) -> dict:
        """Numerically push a relative change of `metric` up every formula to contribution, using expected
        (baseline) values of all inputs at this hour. Works for any expression (products, ratios, sums)."""
        from sentinel.brain.formula import Formula
        changes = {metric: rel}
        frontier = [metric]
        while frontier:
            cur = frontier.pop()
            for e in self.g.outputs(cur):
                out = e.dst
                f = Formula.parse(self.g.formulas[out])
                base = {v: self.baseline(v, {}).expected(ts)[0] for v in f.variables}
                shocked = {v: base[v] * (1 + changes.get(v, 0.0)) for v in f.variables}
                b0 = f.evaluate(base)
                if b0:
                    changes[out] = f.evaluate(shocked) / b0 - 1
                    frontier.append(out)
        res = {"relative_changes": changes}
        money = self.g.profile.get("money_metric", "contribution")
        if money in changes and money in self.g.nodes:
            res["money_eur"] = (changes[money] * self.baseline(money, {}).expected(ts)[0]
                                * self.g.profile.get("money_scale", 1.0))
        return res
