"""Dynamic baselines: robust seasonal Z-scores keyed by LOCAL hour x day type.

For every (metric, segment) the history gives, per key "<local hour><wd|we>":
  * expected value  = median of that key,
  * noise scale     = binomial for share ratios (with an over-dispersion factor fitted on history),
                      otherwise a robust (MAD) scale of log residuals per key.
Local time matters: DST ends on 2026-10-25, so a UTC-keyed baseline would be one hour off for the
whole live run.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from sentinel.brain.validate import local_profile_key

MAD = 1.4826


class SeriesBaseline:
    def __init__(self, kind: str, unit: str, hist: pd.DataFrame):
        self.kind, self.unit = kind, unit
        self.binomial = kind == "ratio" and unit == "share"
        self.log_c = 1.0 if kind in ("count", "sum") else 1e-6
        h = hist.dropna(subset=["value"])
        h = h[h["complete"]] if "complete" in h else h
        self.n_hist = len(h)
        if h.empty:
            self.med, self.med_den, self.sig, self.phi = {}, {}, {}, 1.0
            self.overall, self.overall_den, self.sig_all = np.nan, np.nan, 0.05
            return
        k = local_profile_key(h.ts).values
        self.med = h.groupby(k).value.median().to_dict()
        self.med_den = h.groupby(k).denominator.median().to_dict()
        self.overall, self.overall_den = float(h.value.median()), float(h.denominator.median())
        exp = np.array([self.med[x] for x in k])
        if self.binomial:
            p = np.clip(exp, 1e-4, 1 - 1e-4)
            z = (h.value.values - p) / np.sqrt(p * (1 - p) / np.maximum(h.denominator.values, 1))
            self.phi = max(1.0, (MAD * np.median(np.abs(z - np.median(z)))) ** 2)
            self.sig, self.sig_all = {}, 0.0
        else:
            e = np.log(h.value.values.clip(min=0) + self.log_c) - np.log(exp.clip(min=0) + self.log_c)
            s = pd.Series(e, index=k)
            self.sig_all = max(0.01, MAD * float(np.median(np.abs(e - np.median(e)))))
            self.sig = {key: max(self.sig_all * 0.7, MAD * float(np.median(np.abs(g - g.median()))))
                        for key, g in s.groupby(level=0) if len(g) >= 6}
            self.phi = 1.0

    def expected(self, ts) -> tuple[float, float]:
        key = local_profile_key(pd.Series([pd.Timestamp(ts)])).iloc[0]
        return float(self.med.get(key, self.overall)), float(self.med_den.get(key, self.overall_den))

    def score(self, ts, value: float, denominator: float, factor: float = 1.0) -> dict:
        """Expected value, 4-sigma band and z for one hour. `factor` scales volume (planned events)."""
        exp, _ = self.expected(ts)
        if self.binomial:
            p = float(np.clip(exp, 1e-4, 1 - 1e-4))
            se = float(np.sqrt(self.phi * p * (1 - p) / max(denominator, 1)))
            z = (value - p) / se
            return {"expected": p, "low": max(0.0, p - 4 * se), "high": min(1.0, p + 4 * se), "z": z}
        key = local_profile_key(pd.Series([pd.Timestamp(ts)])).iloc[0]
        s = self.sig.get(key, self.sig_all)
        exp = exp * factor
        if self.kind in ("count", "sum"):   # Poisson floor for small counts
            s = float(np.hypot(s, 1 / np.sqrt(max(exp, 1)))) if self.kind == "count" else s
        z = (np.log(max(value, 0) + self.log_c) - np.log(max(exp, 0) + self.log_c)) / s
        return {"expected": exp, "low": exp * np.exp(-4 * s), "high": exp * np.exp(4 * s), "z": float(z)}

    def window(self, df: pd.DataFrame, factor_fn=None) -> dict:
        """Aggregate actual vs expected over several hours (ratio of sums for ratios)."""
        d = df.dropna(subset=["value"])
        if d.empty:
            return {"actual": np.nan, "expected": np.nan, "rel": np.nan, "z": 0.0, "hours": 0}
        exps = np.array([self.expected(t)[0] for t in d.ts])
        if factor_fn is not None and not self.binomial:
            exps = exps * np.array([factor_fn(t) for t in d.ts])
        if self.kind in ("count", "sum"):
            act, ex = float(d.value.sum()), float(exps.sum())
            s = self.sig_all / np.sqrt(len(d))
            z = (np.log(act + self.log_c) - np.log(ex + self.log_c)) / max(s, 1e-3)
        else:
            w = d.denominator.values.clip(min=0)
            act = float(d.numerator.sum() / max(d.denominator.sum(), 1e-12))
            ex = float((exps * w).sum() / max(w.sum(), 1e-12))
            if self.binomial:
                p = np.clip(ex, 1e-4, 1 - 1e-4)
                z = (act - p) / np.sqrt(self.phi * p * (1 - p) / max(w.sum(), 1))
            else:
                z = (np.log(max(act, 1e-9)) - np.log(max(ex, 1e-9))) / max(self.sig_all / np.sqrt(len(d)), 1e-3)
        return {"actual": act, "expected": ex, "rel": act / ex - 1 if ex else np.nan, "z": float(z), "hours": len(d)}
