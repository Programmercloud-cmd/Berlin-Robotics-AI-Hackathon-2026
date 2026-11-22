"""0. Data profile and calibration: the agent studies the data before it trusts its own defaults.

Runs once on the history window, before the monitor is used, and again whenever asked. It measures the
properties the agent's statistics depend on, writes them to the bus (DATA_PROFILE) and hands the monitor
and the investigator the parameters derived from them. Nothing here is tuned to one dataset: every
number comes from the data being monitored.

  dispersion       per key metric, how much more cells vary than Poisson / binomial noise predicts
                   (phi = var / mean for counts, var / (p (1 - p) / n) for rates; median over the cells of
                   its first dimension pair, same hour-of-week across history days). Used by the
                   dimension scan and by RiskLoc instead of a fixed Poisson assumption.
  common shock     how strongly cells of the same metric move together within an hour (mean pairwise
                   correlation of the cells' hourly residuals). High values mean slices rise and fall
                   together, so a slice that moves alone is more telling.
  releases         unplanned releases per day in the history: the base rate of coincidences an event
                   hypothesis has to beat (reported; event priors already require scope and timing).
  target metrics   ratio / gauge metrics whose numerator and denominator measure the same quantity and
                   whose history sits at 1 (pacing: spend / planned spend). For those a deviation in
                   either direction is harm (an overspending advertiser is a problem even though the
                   marketplace earns more), measured in the denominator's unit.
  planned effects  checked online by the monitor: a planned change with no end time is over once the
                   scoped volume is back at its unplanned baseline (MonitorEngine.planned_end).
"""
from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd

from sentinel.brain.validate import local_profile_key

PHI_MIN, PHI_MAX = 1.0, 400.0
TARGET_BAND = (0.9, 1.1)


class DataProfile:
    def __init__(self, graph, data, monitor, bus=None):
        self.g, self.data, self.mon, self.bus = graph, data, monitor, bus
        self.profile: dict = {}

    # ------------------------------------------------------------------ measurements
    def dispersion(self, metric: str) -> dict | None:
        node = self.g.nodes[metric]
        if len(node.dimensions) < 2:
            return None
        pair = tuple(node.dimensions[:2])
        hist = self.mon.forecaster._fit(metric, pair)
        hist = hist[hist.n >= 3].dropna(subset=["sd"])
        if node.kind == "ratio":
            hist = hist[hist.den >= 50]
            p = (hist.num / hist.den).clip(1e-4, 1 - 1e-4)
            base = p * (1 - p) / hist.den
        else:
            hist = hist[hist.val >= 20]
            base = hist.val
        if hist.empty:
            return None
        ratio = (hist.sd ** 2 / base).replace([np.inf, -np.inf], np.nan).dropna()
        if ratio.empty:
            return None
        phi = float(np.clip(np.nanmedian(ratio), PHI_MIN, PHI_MAX))
        cv = float(np.nanmedian(hist.sd / (hist.val if node.kind != "ratio" else hist.num / hist.den)))
        return {"phi": round(phi, 2), "cell_cv": round(cv, 4), "cells": int(len(ratio)), "pair": list(pair)}

    def common_shock(self, metric: str, days: int = 14) -> float | None:
        node = self.g.nodes[metric]
        if not node.dimensions:
            return None
        d0 = node.dimensions[0]
        end = self.mon.h1
        df = self.data.metric(metric, end - pd.Timedelta(days=days), end, group_by=[d0]).dropna(subset=["value"])
        if df.empty:
            return None
        df = df.assign(k=local_profile_key(df.ts).values)
        df["res"] = np.log(df.value.clip(lower=1e-9)) - df.groupby([d0, "k"]).value.transform(
            lambda s: np.log(s.clip(lower=1e-9)).median())
        wide = df.pivot_table(index="ts", columns=d0, values="res")
        if wide.shape[1] < 2:
            return None
        c = wide.corr().values
        off = c[~np.eye(len(c), dtype=bool)]
        return round(float(np.nanmean(off)), 3)

    def release_rate(self) -> dict:
        evs = self.data.events(self.mon.h0, self.mon.h1)
        rel = [e for e in evs if e.type == "release" and not e.planned]
        days = max((self.mon.h1 - self.mon.h0) / pd.Timedelta(days=1), 1.0)
        by = pd.Series([e.service or "?" for e in rel]).value_counts().to_dict() if rel else {}
        return {"per_day": round(len(rel) / days, 2), "by_service": {k: int(v) for k, v in by.items()}}

    def target_metrics(self) -> list[str]:
        out = []
        for n in self.g.nodes.values():
            if n.kind not in ("ratio", "gauge") or not (n.numerator_sql and n.denominator_sql):
                continue
            num = n.numerator_sql.lower().replace("sum(", "").replace(")", "")
            den = n.denominator_sql.lower().replace("sum(", "").replace(")", "")
            if not (num.endswith("_eur") and den.endswith("_eur")) and n.unit not in ("score",):
                continue
            try:
                v = self.data.metric(n.id, self.mon.h1 - pd.Timedelta(days=14), self.mon.h1).value.median()
            except Exception:                                           # noqa: BLE001 - profile is best effort
                continue
            if TARGET_BAND[0] <= v <= TARGET_BAND[1]:
                out.append(n.id)
        return out

    # ------------------------------------------------------------------ profile + calibration
    def run(self) -> dict:
        disp, shock = {}, {}
        for m in self.mon.scan_metrics():
            d = self.dispersion(m)
            if d:
                disp[m] = d
            s = self.common_shock(m)
            if s is not None:
                shock[m] = s
        self.profile = {"dispersion": disp, "common_shock": shock, "releases": self.release_rate(),
                        "target_metrics": self.target_metrics(),
                        "history": [str(self.mon.h0), str(self.mon.h1)]}
        self.apply()
        if self.bus is not None:
            self.bus.publish("DATA_PROFILE", "calibration", {
                **self.profile,
                "summary": self.summary()})
        return self.profile

    def apply(self):
        """Hand the derived parameters to the monitor (scan, RiskLoc, harm of target metrics)."""
        self.mon.calibration = {
            "phi": {m: d["phi"] for m, d in self.profile["dispersion"].items()},
            "target_metrics": set(self.profile["target_metrics"]),
            "common_shock": dict(self.profile["common_shock"]),
        }

    def summary(self) -> str:
        disp = self.profile["dispersion"]
        if disp:
            phis = sorted(d["phi"] for d in disp.values())
            cvs = sorted(d["cell_cv"] for d in disp.values())
            noise = (f"cells vary {phis[0]:.1f}x-{phis[-1]:.1f}x more than Poisson/binomial noise "
                     f"(median cell CV {np.median(cvs):.1%})")
        else:
            noise = "no dispersion measured"
        sh = self.profile["common_shock"]
        shock = f"mean cross-cell correlation {np.mean(list(sh.values())):+.2f}" if sh else "no common shock measured"
        return (f"{noise}; {shock}; {self.profile['releases']['per_day']} unplanned releases/day; "
                f"target metrics: {', '.join(self.profile['target_metrics']) or 'none'}")


def pairs(dims):  # kept for symmetry with the monitor's scan
    return list(combinations(dims, 2))
