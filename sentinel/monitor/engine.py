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

v2 additions (each can be switched off for ablation via `features`):
  scan        multi-dimensional scan: every pair of dimensions of the key metrics is tested cell by cell
              against its same-hour history, with Benjamini-Hochberg FDR control over all cells of the tick,
              so a DE x iOS failure is seen while the total only wobbles
  riskloc     detection and localisation are separated: an aggregate anomaly is handed to the investigator
              together with RiskLoc's candidate root-cause sets (LOCALISATION_CLUE)
  process     anomalies on process metrics (PM4Py conformance / performance, outcome-branch shares) are
              raised as PROCESS_DEVIATION early warnings
  learning    patterns that investigations confirmed as normal (explained / favourable / operator feedback)
              are written back as baseline corrections with an expiry (BASELINE_LEARNED)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

import numpy as np
import pandas as pd

from sentinel.brain.graph import BrainGraph
from sentinel.bus import EventBus
from sentinel.contracts import seg_key

from itertools import combinations

from scipy import stats

from .baseline import SeriesBaseline

SPIKE_Z, CUSUM_K, CUSUM_H, RECOVER_HOURS, LOOKBACK_H = 4.5, 1.0, 8.0, 3, 6
MIN_HISTORY_HOURS = 12             # fewer complete history hours: no reliable noise estimate, do not judge
PLAN_END_HOURS = 2                 # consecutive hours back at the unplanned baseline that end a planned change
CAMPAIGN_DEFAULT_HOURS = 24        # Event has no end time in the contract: campaigns are assumed to last a day
KEY_METRICS = ["contribution", "gmv", "orders", "conversion_rate", "payment_success_rate", "sessions"]
SCAN_FDR_Q, SCAN_PHI, SCAN_MIN_REL = 1e-3, 2.0, 0.10
DEFAULT_FEATURES = {"scan": True, "riskloc": True, "process": True, "learning": True,
                    "bfs": True,          # the investigator's pruned graph search (v3); off = greedy v2 walk
                    "calibrate": True}    # data profile + calibration, planned-effect end, slow harm (v4)


def seg_from_key(key: str) -> dict:
    return dict(p.split("=", 1) for p in key.split("|")) if key else {}


class MonitorEngine:
    def __init__(self, graph: BrainGraph, data, bus: EventBus, history_start, history_end,
                 features: dict | None = None):
        self.g, self.data, self.bus = graph, data, bus
        self.features = {**DEFAULT_FEATURES, **(features or {})}
        self.learned: list[dict] = []
        self._forecaster = None
        self.h0, self.h1 = pd.Timestamp(history_start), pd.Timestamp(history_end)
        self.baselines: dict[tuple[str, str], SeriesBaseline] = {}
        self.evaluated: set[tuple[str, str, pd.Timestamp]] = set()
        self.pending_dq: set[tuple[str, pd.Timestamp]] = set()
        self.state: dict[tuple[str, str], dict] = defaultdict(lambda: {"anomalous": False, "normal_run": 0,
                                                                        "cpos": 0.0, "cneg": 0.0})
        self.series: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.innocent_seen: set[tuple[str, str, str]] = set()
        self.tick_no = 0
        self.calibration: dict = {"phi": {}, "target_metrics": set(), "common_shock": {}}   # set by DataProfile
        self._plan_end: dict[str, pd.Timestamp] = {}

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
        key = (pd.Timestamp(ts), pd.Timestamp(getattr(self.data, "now", ts)))
        cache = self.__dict__.setdefault("_planned_cache", {})
        if key not in cache:
            evs = self.data.events(ts - timedelta(hours=CAMPAIGN_DEFAULT_HOURS), ts + timedelta(hours=1))
            cache[key] = [e for e in evs if e.planned and e.expected_effect and
                          pd.Timestamp(e.ts) <= ts < pd.Timestamp(e.ts) + timedelta(hours=CAMPAIGN_DEFAULT_HOURS)
                          and (not self.features["calibrate"] or ts < (self.planned_end(e) or pd.Timestamp.max))]
        return cache[key]

    def planned_end(self, ev) -> pd.Timestamp | None:
        """The contract's events carry no end time. A planned change is over once the scoped volume it was
        expected to move is back at its unplanned baseline for PLAN_END_HOURS consecutive complete hours
        (closer to the baseline than to the planned level, in log terms). Found once, then fixed."""
        if ev.id in self._plan_end:
            return self._plan_end[ev.id]
        now = pd.Timestamp(getattr(self.data, "now", self.h1))
        key = ("plan_end_checked", ev.id, now)
        if key in self.__dict__.setdefault("_plan_checks", set()):
            return None
        self._plan_checks.add(key)
        eff = next(((m, f) for m, f in ev.expected_effect.items() if m in self.g.nodes and f and f != 1), None)
        if eff is None:
            return None
        m, f = eff
        scope = {k: v for k, v in (ev.scope or {}).items() if k in self.g.nodes[m].dimensions}
        t0 = pd.Timestamp(ev.ts).floor("h") + timedelta(hours=2)      # allow the change to ramp up
        if now - t0 < timedelta(hours=PLAN_END_HOURS):
            return None
        df = self.data.metric(m, t0, now, filters=scope)
        df = df[df.complete].dropna(subset=["value"])
        b, run = self.baseline(m, scope), []
        for r in df.itertuples():
            base = b.expected(r.ts)[0]
            if base <= 0:
                continue
            lr = np.log(max(r.value, 1e-9) / base)
            run = run + [pd.Timestamp(r.ts)] if abs(lr) < abs(lr - np.log(f)) else []
            if len(run) >= PLAN_END_HOURS:
                self._plan_end[ev.id] = run[0]
                self.bus.publish("PLANNED_EFFECT_ENDED", "monitor", {
                    "event": ev.id, "description": ev.description, "metric": m, "scope": scope,
                    "ended_at": str(run[0]), "expected_factor": f,
                    "why": f"{m} [{seg_key(scope) or 'total'}] back at its unplanned baseline for "
                           f"{PLAN_END_HOURS} hours from {run[0]:%m-%d %H:%M}"})
                return run[0]
        return None

    def planned_factor(self, metric: str, segment: dict, ts) -> tuple[float, list[str]]:
        """Expected-volume multiplier from planned events. Traffic effects (sessions) scale every volume
        metric in scope; rates are expected unchanged."""
        node = self.g.nodes[metric]
        f_total, why = 1.0, []
        if self.features["learning"]:
            for p in self.learned:
                if p["metric"] == metric and p["from"] <= pd.Timestamp(ts) < p["until"] and \
                        all(segment.get(k) == v for k, v in p["segment"].items()):
                    f_total *= p["factor"]
                    why.append(f"learned: {p['reason']} x{p['factor']:.2f}")
        if node.kind not in ("count", "sum"):
            return f_total, why
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
        if self.features["scan"]:
            anomalies += self._scan(now)
        for a in anomalies:
            node = self.g.nodes[a["metric"]]
            if self.features["process"] and (node.owner.startswith("pm4py") or a["metric"].endswith("_share")
                                             and node.owner.startswith("pm4py")):
                a["detector"] = "process"
                self.bus.publish("PROCESS_DEVIATION", "monitor", {
                    "metric": a["metric"], "segment": a["segment"], "hour": a["ts"], "actual": a["actual"],
                    "expected": a["expected"], "z": a["z"],
                    "text": "process signal: the lifecycle deviates from its discovered behaviour"})
            if self.features["riskloc"]:
                self._clues(a)
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

    def early_tick(self, window_start) -> list[dict]:
        """Early warning on a rolling one-hour window (sources that publish them, e.g. Eye every five
        minutes). Each watched series is compared with the baseline of the local hour the window mostly
        covers, with the same small-sample threshold as the hourly spike detector. A hit is published as
        EARLY_WARNING (and remembered per series and hour); the investigation still runs on the complete
        hour, so an early warning adds speed without adding unexplained pages."""
        if not hasattr(self.data, "rolling_metric"):
            return []
        ws = pd.Timestamp(window_start)
        hour = (ws + timedelta(minutes=30)).floor("h")
        out = []
        for m, gb, _ in self.watch_plan(hour + timedelta(hours=1)):
            if m in self.g.profile.get("silent_metrics", ()):
                continue
            try:
                df = self.data.rolling_metric(m, ws, group_by=gb)
            except ValueError:
                continue
            if df.empty:
                continue
            node = self.g.nodes[m]
            groups = df.groupby(gb[0]) if gb else [(None, df)]
            for member, part in groups:
                seg = {gb[0]: str(member)} if gb else {}
                for row in part.itertuples():
                    if not row.complete or row.value != row.value:
                        continue
                    wkey = ("early", m, seg_key(seg), hour)
                    if wkey in self.evaluated:
                        continue
                    b = self.baseline(m, seg)
                    if b.n_hist < MIN_HISTORY_HOURS:
                        continue
                    factor, _ = self.planned_factor(m, seg, hour)
                    s = b.score(hour, row.value, row.denominator, factor)
                    support = row.denominator >= 100 if node.kind in ("ratio", "gauge") else s["expected"] >= 20
                    if support and abs(s["z"]) >= self.spike_threshold(b.n_hist):
                        self.evaluated.add(wkey)
                        w = {"metric": m, "segment": seg_key(seg), "window_start": str(ws), "hour": str(hour),
                             "actual": row.value, "expected": s["expected"], "z": s["z"],
                             "direction": "up" if s["z"] > 0 else "down", "good_direction": node.good_direction,
                             "impact_eur_per_h": self.impact_eur(m, seg, hour, row.value, s["expected"])}
                        self.bus.publish("EARLY_WARNING", "monitor", w)
                        out.append(w)
        return out

    @staticmethod
    def spike_threshold(n_hist: int) -> float:
        """SPIKE_Z under a normal noise model; with little history the noise scale is itself uncertain, so
        the threshold is the Student-t quantile (n - 1 df) with the same tail probability. 56 days of history
        give ~4.5; 16 hours give ~7.5."""
        cache = MonitorEngine.__dict__.get("_thr_cache")
        if cache is None:
            cache = MonitorEngine._thr_cache = {}
        if n_hist not in cache:
            cache[n_hist] = max(SPIKE_Z, float(stats.t.isf(stats.norm.sf(SPIKE_Z), df=max(n_hist - 1, 1))))
        return cache[n_hist]

    def _judge(self, m: str, seg: dict, ts, row) -> dict | None:
        node = self.g.nodes[m]
        b = self.baseline(m, seg)
        if b.n_hist < MIN_HISTORY_HOURS or np.isnan(row.value) or m in self.g.profile.get("silent_metrics", ()):
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
        spike = support and abs(s["z"]) >= self.spike_threshold(b.n_hist)
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

    # ================================================================ v2: multi-dimensional scan
    @property
    def forecaster(self):
        if self._forecaster is None:
            from sentinel.riskloc import LeafForecaster
            self._forecaster = LeafForecaster(self.data, self.g, self.h0, self.h1)
        return self._forecaster

    def scan_metrics(self) -> list[str]:
        keys = self.g.profile.get("key_metrics", KEY_METRICS)
        return [m for m in keys if m in self.g.nodes and self.g.nodes[m].kind in ("count", "ratio")
                and len(self.g.nodes[m].dimensions) >= 2]

    def _scan(self, now) -> list[dict]:
        """Test every cell of every dimension pair of the key metrics; BH-FDR over all tests of this tick."""
        from sentinel.brain.validate import local_profile_key
        start = now - timedelta(hours=LOOKBACK_H)
        tests = []
        for m in self.scan_metrics():
            node = self.g.nodes[m]
            for pair in combinations(node.dimensions, 2):
                hist = self.forecaster._fit(m, pair)
                cur = self.data.metric(m, start, now, group_by=list(pair))
                cur = cur[cur.complete].dropna(subset=["value"])
                if cur.empty:
                    continue
                cur = cur.assign(k=local_profile_key(cur.ts).values)
                for d in pair:
                    cur[d] = cur[d].astype(str)
                    hist[d] = hist[d].astype(str)
                mm = cur.merge(hist, on=list(pair) + ["k"], how="inner")
                for r in mm.itertuples():
                    seg = {pair[0]: getattr(r, pair[0]), pair[1]: getattr(r, pair[1])}
                    key = (m, "scan:" + seg_key(seg), pd.Timestamp(r.ts))
                    if key in self.evaluated or r.n < 3:
                        continue
                    self.evaluated.add(key)
                    factor, _ = self.planned_factor(m, seg, pd.Timestamp(r.ts))   # planned + learned
                    sd = r.sd if r.sd == r.sd else 0.0                             # cell's own history spread
                    phi = max(SCAN_PHI, self.calibration["phi"].get(m, SCAN_PHI))  # pooled (calibration)
                    if node.kind == "ratio":
                        if not r.den or r.denominator < 50:
                            continue
                        p = min(max(r.num / r.den * factor, 1e-4), 1 - 1e-4)
                        exp, act = p, r.value
                        z = (act - p) / max(sd, np.sqrt(phi * p * (1 - p) / r.denominator))
                    else:
                        if r.val < 20:
                            continue
                        exp, act = r.val * factor, r.value
                        z = (act - exp) / max(sd * factor, np.sqrt(phi * exp))
                    rel = act / exp - 1 if exp else 0.0
                    tests.append((float(2 * stats.norm.sf(abs(z))), z, rel, m, seg, pd.Timestamp(r.ts), act, exp))
        if not tests:
            return []
        tests.sort(key=lambda t: t[0])
        n, cutoff = len(tests), -1
        for i, t in enumerate(tests):                      # Benjamini-Hochberg
            if t[0] <= SCAN_FDR_Q * (i + 1) / n:
                cutoff = i
        out = []
        for i, (p, z, rel, m, seg, ts, act, exp) in enumerate(tests):
            skey = (m, "scan:" + seg_key(seg))
            st = self.state[skey]
            hit = i <= cutoff and abs(rel) >= SCAN_MIN_REL
            if hit and not st["anomalous"]:
                st["anomalous"], st["normal_run"] = True, 0
                a = {"metric": m, "segment": seg_key(seg), "ts": str(ts), "actual": act, "expected": exp,
                     "low": None, "high": None, "z": float(z), "detector": "scan",
                     "direction": "up" if z > 0 else "down", "good_direction": self.g.nodes[m].good_direction,
                     "planned_adjustment": [], "p_value": p, "tests_this_tick": n,
                     "impact_eur_per_h": self.impact_eur(m, seg, ts, act, exp)}
                self.bus.publish("ANOMALY_DETECTED", "monitor", a)
                out.append(a)
            elif not hit and st["anomalous"]:
                st["normal_run"] += 1
                if st["normal_run"] >= RECOVER_HOURS:
                    st["anomalous"] = False
        self.bus.publish("SCAN", "monitor", {"cells_tested": n, "significant": cutoff + 1, "new_anomalies": len(out),
                                             "fdr_q": SCAN_FDR_Q})
        return out

    # ================================================================ v2: localisation clues (RiskLoc)
    def _clues(self, a: dict):
        from sentinel.riskloc import riskloc
        node = self.g.nodes[a["metric"]]
        seg = seg_from_key(a["segment"])
        dims = [d for d in node.dimensions if d not in seg][:4]
        if node.kind not in ("count", "ratio", "sum") or len(dims) < 2 or len(seg) > 1:
            return
        ts = pd.Timestamp(a["ts"])
        leaves = self.forecaster.leaves(a["metric"], list(seg) + dims, ts, ts + timedelta(hours=1))
        for k, v in seg.items():
            leaves = leaves[leaves[k].astype(str) == v]
        res = riskloc(leaves, dims, phi=self.calibration["phi"].get(a["metric"], 1.0))
        causes = [{**c, "segment": {**seg, **c["segment"]}} for c in res.get("causes", [])]
        a["clues"] = causes
        self.bus.publish("LOCALISATION_CLUE", "monitor", {
            "metric": a["metric"], "segment": a["segment"], "hour": a["ts"], "method": "riskloc",
            "leaves": res.get("leaves"), "abnormal_leaves": res.get("abnormal_leaves"), "causes": causes})

    # ================================================================ v2: learning from investigations
    def learn(self, metric: str, segment: dict, factor: float, start, until, reason: str, incident: str | None = None):
        if not self.features["learning"] or not np.isfinite(factor) or factor <= 0:
            return
        p = {"metric": metric, "segment": dict(segment), "factor": float(factor), "from": pd.Timestamp(start),
             "until": pd.Timestamp(until), "reason": reason, "incident": incident}
        self.learned.append(p)
        for k in [k for k in self.state if k[0] == metric]:
            self.state[k]["anomalous"] = False                # re-judge the series under the new expectation
        self.bus.publish("BASELINE_LEARNED", "monitor", {**p, "from": str(p["from"]), "until": str(p["until"])},
                         incident)

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
