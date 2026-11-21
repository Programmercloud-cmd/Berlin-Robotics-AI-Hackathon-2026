"""Investigator tools. Each tool is one bounded, explainable query over Data + baselines.

  window_dev      actual vs expected over a window, for any metric and segment
  data_check      completeness of the metric's source tables
  decompose       attribute an output's change to its formula inputs (one-at-a-time replacement on
                  window aggregates; exact for products/ratios, also valid for sums)
  drilldown       split a change across the members of a dimension: share of the deviation vs share
                  of volume (lift); for ratios the rate effect (w * dp) is used
  compare_control Welch t-test of hourly deviations in the segment vs its siblings (negative control)
  causal_check    did a hypothesised cause move with the right sign? + Granger test on residuals
  onset           first hour of the change
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
from scipy import stats

from sentinel.brain.formula import Formula
from sentinel.contracts import seg_key


class Toolbox:
    def __init__(self, graph, data, monitor):
        self.g, self.data, self.mon = graph, data, monitor

    def supports(self, metric: str, seg: dict) -> bool:
        return all(k in self.g.nodes[metric].dimensions for k in seg)

    # ------------------------------------------------------------------ basics
    def hourly(self, metric: str, seg: dict, start, end) -> pd.DataFrame:
        df = self.data.metric(metric, start, end, filters=seg)
        df = df[df.complete].dropna(subset=["value"]).copy()
        b = self.mon.baseline(metric, seg)
        rows = []
        for r in df.itertuples():
            f = self.mon.planned_factor(metric, seg, pd.Timestamp(r.ts))[0]
            s = b.score(r.ts, r.value, r.denominator, f)
            rows.append({"ts": pd.Timestamp(r.ts), "actual": r.value, "expected": s["expected"], "z": s["z"],
                         "numerator": r.numerator, "denominator": r.denominator})
        out = pd.DataFrame(rows)
        if not out.empty:
            out["rel"] = out.actual / out.expected - 1
        return out

    def window_dev(self, metric: str, seg: dict, start, end) -> dict:
        df = self.data.metric(metric, start, end, filters=seg)
        comp = df[df.complete]
        b = self.mon.baseline(metric, seg)
        w = b.window(comp, factor_fn=lambda t: self.mon.planned_factor(metric, seg, pd.Timestamp(t))[0])
        w.update({"metric": metric, "segment": seg_key(seg), "incomplete_hours": int((~df.complete).sum())})
        if self.g.nodes[metric].kind in ("ratio", "gauge"):
            w["volume"] = float(comp.denominator.sum())
        return w

    def data_check(self, metric: str, start, end) -> dict:
        out = {}
        for t in self.g.tables_of(metric):
            hrs = pd.date_range(pd.Timestamp(start), pd.Timestamp(end) - timedelta(hours=1), freq="h")
            c = {str(h): round(self.data.completeness(t, h), 3) for h in hrs}
            out[t] = {"min": min(c.values()) if c else 1.0, "incomplete_hours": [h for h, v in c.items() if v < 0.95]}
        return out

    def onset(self, metric: str, seg: dict, around, lookback_h: int = 24) -> pd.Timestamp:
        h = self.hourly(metric, seg, pd.Timestamp(around) - timedelta(hours=lookback_h), pd.Timestamp(around) + timedelta(hours=1))
        if h.empty:
            return pd.Timestamp(around)
        h = h.sort_values("ts").reset_index(drop=True)
        last = h[h.z.abs() >= 3]
        if last.empty:
            return pd.Timestamp(around)
        sign = np.sign(last.z.iloc[-1])
        hit = ((h.z * sign) >= 3).values
        # change point: the earliest hour from which >= 70% of the hours up to the detection deviate the same way
        for i in range(len(h)):
            if hit[i] and hit[i:].mean() >= 0.7:
                return h.ts.iloc[i]
        return last.ts.iloc[-1]

    # ------------------------------------------------------------------ decomposition
    def decompose(self, metric: str, seg: dict, start, end) -> dict:
        expr = self.g.formulas.get(metric)
        edges = self.g.inputs(metric)
        if not expr or not edges:
            return {"possible": False, "reason": f"{metric} has no validated formula: it is a leaf of the driver tree"}
        f = Formula.parse(expr)
        missing = [v for v in f.variables if not self.supports(v, seg)]
        if missing:
            return {"possible": False, "reason": f"inputs {missing} cannot be sliced by {seg_key(seg)}"}
        devs = {v: self.window_dev(v, seg, start, end) for v in f.variables}
        exp = {v: d["expected"] for v, d in devs.items()}
        act = {v: d["actual"] for v, d in devs.items()}
        base = f.evaluate(exp)
        total = f.evaluate(act) - base
        parts = []
        for v in f.variables:
            delta = f.evaluate({**exp, v: act[v]}) - base
            parts.append({"input": v, "rel": devs[v]["rel"], "z": devs[v]["z"], "delta": delta,
                          "share": delta / total if total else 0.0})
        parts.sort(key=lambda p: -abs(p["share"]))
        return {"possible": True, "expr": expr, "output_rel": f.evaluate(act) / base - 1 if base else np.nan,
                "parts": parts, "residual_share": 1 - sum(p["share"] for p in parts) if total else 0.0}

    # ------------------------------------------------------------------ localisation
    def drilldown(self, metric: str, seg: dict, dim: str, start, end) -> dict:
        node = self.g.nodes[metric]
        df = self.data.metric(metric, start, end, filters=seg, group_by=[dim])
        df = df[df.complete]
        rows = []
        for member, part in df.groupby(dim):
            s2 = {**seg, dim: str(member)}
            w = self.mon.baseline(metric, s2).window(
                part, factor_fn=lambda t, s2=s2: self.mon.planned_factor(metric, s2, pd.Timestamp(t))[0])
            vol = float(part.denominator.sum()) if node.kind in ("ratio", "gauge") else w["expected"]
            rows.append({"member": str(member), "actual": w["actual"], "expected": w["expected"], "rel": w["rel"],
                         "z": w["z"], "volume": vol})
        if not rows:
            return {"dim": dim, "members": [], "top": None}
        tv = sum(r["volume"] for r in rows) or 1.0
        for r in rows:
            r["vol_share"] = r["volume"] / tv
            r["effect"] = (r["vol_share"] * (r["actual"] - r["expected"])) if node.kind in ("ratio", "gauge") \
                else (r["actual"] - r["expected"])
        te = sum(r["effect"] for r in rows)
        for r in rows:
            r["dev_share"] = r["effect"] / te if te else 0.0
            r["lift"] = r["dev_share"] / r["vol_share"] if r["vol_share"] else 0.0
        rows.sort(key=lambda r: -r["dev_share"])
        return {"dim": dim, "members": rows, "top": rows[0]}

    def compare_control(self, metric: str, seg: dict, key: str, start, end) -> dict:
        """Is the change specific to seg[key]? Compare the window deviation against the sibling members of
        `key` (volume-weighted); with >= 3 hours also a Welch t-test on hourly deviations."""
        parent = {k: v for k, v in seg.items() if k != key}
        dd = self.drilldown(metric, parent, key, start, end)
        me = next((m for m in dd["members"] if m["member"] == seg[key]), None)
        sibs = [m for m in dd["members"] if m["member"] != seg[key]]
        if me is None or not sibs:
            return {"possible": False}
        vol = sum(m["vol_share"] for m in sibs) or 1.0
        sib_rel = sum(m["rel"] * m["vol_share"] for m in sibs if np.isfinite(m["rel"])) / vol
        sib_z = max((abs(m["z"]) for m in sibs), default=0.0)
        res = {"possible": True, "key": key, "focus_rel": float(me["rel"]), "focus_z": float(me["z"]),
               "siblings_rel": float(sib_rel), "siblings_max_abs_z": float(sib_z),
               "siblings": [{"member": m["member"], "rel": m["rel"], "z": m["z"]} for m in sibs],
               # siblings moving the other way, or by less than a third as much, do not contradict confinement
               "confined": bool(abs(me["z"]) >= 3 and (np.sign(sib_rel) != np.sign(me["rel"])
                                                       or abs(sib_rel) <= abs(me["rel"]) / 3))}
        focus = self.hourly(metric, seg, start, end)
        if len(focus) >= 3:
            sib_h = [self.hourly(metric, {**parent, key: m["member"]}, start, end) for m in sibs]
            sib_vals = [x for h in sib_h if not h.empty for x in h.rel]
            if len(sib_vals) >= 3:
                with np.errstate(all="ignore"):
                    t, p = stats.ttest_ind(focus.rel, sib_vals, equal_var=False)
                if np.isfinite(t):
                    res.update({"t": float(t), "p": float(p)})
        return res

    # ------------------------------------------------------------------ causal
    def causal_check(self, cause: str, effect: str, sign: str, seg: dict, start, end, effect_rel: float) -> dict:
        cseg = {k: v for k, v in seg.items() if k in self.g.nodes[cause].dimensions}
        w = self.window_dev(cause, cseg, start, end)
        want = {"+": 1, "-": -1}.get(sign, 0)
        moved = abs(w["z"]) >= 3
        consistent = moved and (want == 0 or np.sign(w["rel"]) * want == np.sign(effect_rel))
        res = {"cause": cause, "segment": seg_key(cseg), "rel": w["rel"], "z": w["z"], "moved": bool(moved),
               "consistent": bool(consistent)}
        try:   # Granger on hourly residuals over a week before + the incident window
            from statsmodels.tsa.stattools import grangercausalitytests
            s0 = pd.Timestamp(start) - timedelta(days=7)
            hc = self.hourly(cause, cseg, s0, end).set_index("ts").rel
            he = self.hourly(effect, {k: v for k, v in seg.items() if k in self.g.nodes[effect].dimensions},
                             s0, end).set_index("ts").rel
            al = pd.concat([he, hc], axis=1, join="inner").dropna()
            if len(al) > 40 and al.iloc[:, 1].std() > 0:
                r = grangercausalitytests(al.values, maxlag=2, verbose=False)
                res["granger_p"] = float(min(v[0]["ssr_ftest"][1] for v in r.values()))
        except Exception as ex:  # statistical tool is best-effort evidence
            res["granger_error"] = str(ex)[:80]
        return res
