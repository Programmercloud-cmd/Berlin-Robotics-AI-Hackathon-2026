"""RiskLoc-style multi-dimensional root-cause localisation (our implementation of the published idea:
2-way partitioning of leaf elements + a weighted risk score, searched layer by layer).

Input: the leaf elements of an aggregate KPI (every combination of the dimensions) with an actual value v
and a forecast f. For a ratio KPI the additive part is used (e.g. successes v against expected successes
f = attempts x usual rate), so leaves add up to the aggregate.

  1. deviation   d_i = (f_i - v_i) / sqrt(f_i), oriented so the anomaly direction is positive; the
                 standardised form keeps small, noisy leaves from setting the cutoff (the paper uses a
                 relative deviation; on hourly marketplace slices the standardised one is more robust)
  2. partition   cutoff t = |most negative deviation| (the opposite-direction tail measures the noise);
                 leaf weight w_i = d_i - t, abnormal if w_i > 0
  3. risk        for a candidate element e (e.g. country=DE, or country=DE & platform=mobile)
                   r_e  = sum of positive w in e / sum of |w| in e        (how purely abnormal e is)
                   ep_e = sum (f - v) in e / sum (f - v) overall          (how much of the change it explains)
                 e qualifies if r_e >= 0.5, ep_e >= 0.02 and its deviation is significant
                 against the rest (z of its excess change over the rest's relative change, above a
                 Bonferroni-adjusted threshold over all candidate
                 elements, at least 3) and concentrated (lift = ep_e / volume
                 share >= 1.3) with contrast (relative change outside e at most half the one inside);
                 the best has the largest r_e * ep_e, so a
                 broad but impure element and a pure but tiny one both lose to the right combination
  4. iterate     remove the chosen element's leaves and search again while >= 10% of the change is left
                 (multiple root causes)
"""
from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd
from scipy import stats

from sentinel.brain.validate import local_profile_key

RISK_THRESHOLD, PEP_THRESHOLD, RESIDUAL_STOP, MAX_CAUSES, MIN_Z, MIN_LIFT = 0.5, 0.02, 0.10, 3, 3.0, 1.3


def riskloc(leaves: pd.DataFrame, dims: list[str], max_layer: int = 3) -> dict:
    """leaves: one row per leaf with columns dims + ['v', 'f']. Returns {direction, causes, ...}."""
    df = leaves.dropna(subset=["v", "f"]).copy()
    df = df[(df.v + df.f) > 0]
    if df.empty:
        return {"causes": [], "reason": "no data"}
    total_dev = float((df.f - df.v).sum())
    if abs(total_dev) < 1e-9:
        return {"causes": [], "reason": "no aggregate deviation"}
    sign = 1.0 if total_dev > 0 else -1.0          # +1: actual below forecast (a drop)
    df["dev"] = sign * (df.f - df.v)               # oriented deviation amount
    df["d"] = sign * (df.f - df.v) / np.sqrt(df.f.clip(lower=1.0))
    t = max(0.0, -float(df.d.min()))
    df["w"] = df.d - t
    original = float(df.dev.sum())
    # many candidate elements are tested at once: Bonferroni-adjust the significance threshold
    n_cand = sum(df.groupby(list(c), observed=True).ngroups
                 for layer in range(1, min(max_layer, len(dims)) + 1) for c in combinations(dims, layer))
    z_min = max(MIN_Z, float(stats.norm.isf(0.05 / max(n_cand, 1))))
    causes, remaining = [], df
    for _ in range(MAX_CAUSES):
        best = None
        for layer in range(1, min(max_layer, len(dims)) + 1):
            for combo in combinations(dims, layer):
                g = remaining.groupby(list(combo), observed=True)
                pos = g.w.apply(lambda w: w.clip(lower=0).sum())
                tot = g.w.apply(lambda w: w.abs().sum())
                ep = g.dev.sum() / original
                vol = g.f.sum() / max(float(df.f.sum()), 1e-9)          # share of the forecast volume
                rel_in = g.dev.sum() / g.f.sum().clip(lower=1e-9)       # relative change inside the element
                rest_dev = float(remaining.dev.sum()) - g.dev.sum()
                rest_f = (float(remaining.f.sum()) - g.f.sum()).clip(lower=1e-9)
                rel_out = rest_dev / rest_f                              # ... and in everything else
                # null hypothesis: the element changed like everything else (not: did not change at all)
                z = (g.dev.sum() - g.f.sum() * rel_out.clip(lower=0)) / np.sqrt(g.f.sum().clip(lower=1.0))
                for key in pos.index:
                    r = float(pos[key] / tot[key]) if tot[key] > 0 else 0.0
                    e = float(ep[key])
                    lift = e / max(float(vol[key]), 1e-9)
                    # an element that explains no more than its volume share is not a localisation:
                    # a uniform change (every platform down 1.4%) stays an aggregate-wide change
                    contrast = float(rel_out[key]) <= float(rel_in[key]) / 2   # the rest changed much less
                    if r < RISK_THRESHOLD or e < PEP_THRESHOLD or float(z[key]) < z_min or lift < MIN_LIFT                             or not contrast:
                        continue
                    score = r * e
                    if best is None or score > best["score"] + 1e-12 or \
                            (abs(score - best["score"]) <= 1e-12 and layer < len(best["segment"])):
                        k = key if isinstance(key, tuple) else (key,)
                        best = {"segment": dict(zip(combo, [str(x) for x in k])), "risk": round(r, 3),
                                "explanatory_power": round(e, 3), "score": round(score, 4),
                                "z": round(float(z[key]), 2), "lift": round(lift, 2)}
        if best is None:
            break
        mask = np.ones(len(remaining), bool)
        for k, val in best["segment"].items():
            mask &= remaining[k].astype(str).values == val
        best["abnormal_leaves"] = int((remaining.w[mask] > 0).sum())
        best["leaves"] = int(mask.sum())
        causes.append(best)
        remaining = remaining[~mask]
        if remaining.empty or float(remaining.dev.sum()) / original < RESIDUAL_STOP:
            break
    return {"direction": "drop" if sign > 0 else "rise", "cutoff": round(t, 4), "leaves": len(df),
            "candidates_tested": n_cand, "z_threshold": round(z_min, 2),
            "abnormal_leaves": int((df.w > 0).sum()), "causes": causes}


class LeafForecaster:
    """Leaf-level forecasts from history: same local hour x day type, per leaf (sums of numerator and
    denominator, so ratio leaves keep their weight)."""

    def __init__(self, data, graph, history_start, history_end):
        self.data, self.g = data, graph
        self.h0, self.h1 = history_start, history_end
        self.tables: dict[tuple, pd.DataFrame] = {}

    def _fit(self, metric: str, dims: tuple) -> pd.DataFrame:
        key = (metric, tuple(sorted(dims)))
        if key not in self.tables:
            h = self.data.metric(metric, self.h0, self.h1, group_by=list(dims))
            h = h[h.complete].dropna(subset=["value"]).copy()
            h["k"] = local_profile_key(h.ts).values
            agg = h.groupby(list(dims) + ["k"]).agg(num=("numerator", "mean"), den=("denominator", "mean"),
                                                    val=("value", "mean"), sd=("value", "std"),
                                                    n=("value", "size")).reset_index()
            self.tables[key] = agg
        return self.tables[key]

    def leaves(self, metric: str, dims: list[str], start, end) -> pd.DataFrame:
        """Actual vs forecast per leaf over [start, end) in the KPI's additive form."""
        dims = tuple(dims)
        hist = self._fit(metric, dims)
        cur = self.data.metric(metric, start, end, group_by=list(dims))
        cur = cur[cur.complete].dropna(subset=["value"]).copy()
        if cur.empty:
            return pd.DataFrame(columns=list(dims) + ["v", "f"])
        cur["k"] = local_profile_key(cur.ts).values
        for d in dims:
            cur[d] = cur[d].astype(str)
            hist[d] = hist[d].astype(str)
        m = cur.merge(hist, on=list(dims) + ["k"], how="left")
        if self.g.nodes[metric].kind in ("ratio", "gauge"):
            rate = (m.num / m.den.where(m.den != 0))
            m["v"], m["f"] = m.numerator, m.denominator * rate   # successes vs expected successes
        else:
            m["v"], m["f"] = m.value, m.val
        return m.groupby(list(dims))[["v", "f"]].sum().reset_index()
