"""Validation of graph edges against data .

  formula    both sides computed from data; median relative error < 0.5% in >= 95% of slices
  breakdown  slices reconcile with the total: summed for counts, numerator/denominator for ratios
  causal     lagged rank correlation on residuals (de-seasonalised by LOCAL hour-of-day x day type,
             DST-safe), pooled over shared segments; updates confidence
  context    data source has rows; service names occur in the events table

Brain talks to Data only through metric(), completeness() and events().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy import stats

from sentinel.contracts import Edge, seg_key

from .formula import Formula
from .graph import SERVICE, TABLE, BrainGraph, edge_key

TZ = ZoneInfo("Europe/Berlin")
FORMULA_TOL, FORMULA_SLICE_SHARE = 0.005, 0.95
CAUSAL_MIN_RHO, CAUSAL_MAX_P, MAX_SEGMENTS = 0.10, 1e-3, 60


@dataclass
class ValidationSummary:
    verdicts: dict[str, str] = field(default_factory=dict)   # edge key -> verdict

    def counts(self) -> dict:
        out: dict[str, dict[str, int]] = {}
        for k, v in self.verdicts.items():
            t = k.split(":", 1)[0]
            out.setdefault(t, {})
            out[t][v] = out[t].get(v, 0) + 1
        return out


def local_profile_key(ts: pd.Series) -> pd.Series:
    """(local hour, weekend) — seasonality must be keyed in local time, not UTC (DST)."""
    loc = pd.to_datetime(ts).dt.tz_localize("UTC").dt.tz_convert(TZ)
    return loc.dt.hour.astype(str) + np.where(loc.dt.dayofweek >= 5, "we", "wd")


# ====================================================================== formula
def check_formula(g: BrainGraph, data, output: str, start: datetime, end: datetime) -> tuple[str, str, dict]:
    expr = g.formulas[output]
    f = Formula.parse(expr)
    dims = set(g.nodes[output].dimensions)
    for v in f.variables:
        dims &= set(g.nodes[v].dimensions)
    order = [d for d in g.nodes[output].dimensions if d in dims]
    medians: dict[str, float] = {}
    for level in [[]] + [[d] for d in order]:
        keys = ["ts"] + level
        lhs = data.metric(output, start, end, group_by=level).set_index(keys).value
        parts = {v: data.metric(v, start, end, group_by=level).set_index(keys).value for v in f.variables}
        al = pd.concat({"__lhs": lhs, **parts}, axis=1, join="inner").dropna()
        if al.empty:
            continue
        rel = (al["__lhs"] - f.evaluate({v: al[v] for v in f.variables})).abs() / al["__lhs"].abs().clip(lower=1e-9)
        if level:
            for member, r in rel.groupby(level=level[0]):
                medians[seg_key({level[0]: str(member)})] = float(r.median())
        else:
            medians[""] = float(rel.median())
    if not medians:
        return "needs_review", "no overlapping data to compare both sides", {}
    passing = [k for k, m in medians.items() if m < FORMULA_TOL]
    share = len(passing) / len(medians)
    failing = sorted((k or "total" for k, m in medians.items() if m >= FORMULA_TOL))
    st = {"slices": len(medians), "passing_share": round(share, 3), "worst_median_rel_err":
          max(medians.values()), "slice_dims": order, "failing": failing[:10], "window": [str(start), str(end)]}
    if share >= FORMULA_SLICE_SHARE:
        return "validated", f"identity holds: {len(passing)}/{len(medians)} slices with median rel. error < 0.5%", st
    return "needs_review", f"identity fails in {len(failing)}/{len(medians)} slices (e.g. {', '.join(failing[:3])})", st


# ====================================================================== breakdown
def check_breakdown(g: BrainGraph, data, metric: str, dim: str, start: datetime, end: datetime):
    kind = g.nodes[metric].kind
    tot = data.metric(metric, start, end).set_index("ts")
    sl = data.metric(metric, start, end, group_by=[dim])
    members = sorted(sl[dim].astype(str).unique().tolist())
    s = sl.groupby("ts")[["value", "numerator", "denominator"]].sum()
    gauge_without_parts = kind == "gauge" and bool((sl.denominator.dropna() == 1).all())
    if gauge_without_parts and g.nodes[metric].unit in ("seconds", "ms", "score"):
        # percentiles / scores: slices are compared with each other, they never add up to the total
        return "validated", f"{len(members)} slices; non-additive ({g.nodes[metric].unit}): compared, not summed", \
            {"members": members, "n_members": len(members), "additive": False}
    if kind in ("count", "sum") or gauge_without_parts:   # snapshot levels (stock units) add up across slices
        kind = "count"
    if kind in ("count", "sum"):
        errs = [(tot.value - s.value).abs() / tot.value.abs().clip(lower=1e-9)]
    else:
        errs = [(tot[c] - s[c]).abs() / tot[c].abs().clip(lower=1e-9) for c in ("numerator", "denominator")]
    err = float(max(e.median() for e in errs))
    st = {"members": members, "n_members": len(members), "median_rel_err": err}
    how = "values sum" if kind in ("count", "sum") else "numerators and denominators sum"
    if err < FORMULA_TOL:
        return "validated", f"{len(members)} slices; {how} to the total", st
    return "needs_review", f"slices do not reconcile with the total (median rel. error {err:.2%})", st


# ====================================================================== causal
_SQL_WORDS = {"sum", "count", "avg", "min", "max", "nullif", "case", "when", "then", "else", "end", "and", "or"}


def _columns(node) -> set[str]:
    import re
    sql = " ".join(s for s in (node.value_sql, node.numerator_sql, node.denominator_sql) if s).lower()
    return {w for w in re.findall(r"\b[a-z_][a-z0-9_]*\b", re.sub(r"\bfact_[a-z]+\.", "", sql))} - _SQL_WORDS


def _residuals(df: pd.DataFrame, kind: str, seg_cols: list[str]) -> pd.DataFrame:
    df = df.dropna(subset=["value"]).copy()
    x = np.log1p(df.value.clip(lower=0)) if kind in ("count", "sum") else np.log(df.value.clip(lower=1e-6))
    df["x"] = x
    df["k"] = local_profile_key(df.ts).values
    grp = seg_cols + ["k"]
    df["r"] = df.x - df.groupby(grp).x.transform("median")
    return df


def check_causal(g: BrainGraph, data, e: Edge, start: datetime, end: datetime):
    src, dst = g.nodes[e.src], g.nodes[e.dst]
    shared = [d for d in src.dimensions if d in dst.dimensions]
    level, n_seg = [], 1
    for d in shared:  # finest shared grain that stays tractable
        k = data.metric(e.src, end - timedelta(hours=6), end, group_by=[d])[d].nunique()
        if n_seg * k <= MAX_SEGMENTS:
            level.append(d)
            n_seg *= k
    src_df = data.metric(e.src, start, end, group_by=level)
    dst_df = data.metric(e.dst, start, end, group_by=level)
    a = _residuals(src_df, src.kind, level)
    b = _residuals(dst_df, dst.kind, level)
    # changes of the effect: a pulse cause (a delivery) moves the effect's *level* step-wise
    b = b.sort_values(["ts"]).assign(d=lambda x: x.groupby(level).r.diff() if level else x.r.diff())
    # the effect's own numerator, to detect causes that only proxy the effect's volume
    num = None
    if dst.kind in ("ratio", "gauge"):
        num = _residuals(dst_df.assign(value=dst_df.numerator), "count", level)[["ts"] + level + ["r"]]
    lo, hi = e.lag_h
    lags = sorted(set(range(lo, hi + 1, max(1, (hi - lo) // 12))) | {hi})
    want = {"+": 1, "-": -1}.get(e.sign or "±", 0)
    results = []
    for lag in lags:
        a2 = a[["ts"] + level + ["r"]].copy()
        a2["ts"] = a2.ts + pd.Timedelta(hours=lag)
        for mode, col in (("level", "r"), ("change", "d")):
            m = a2.merge(b[["ts"] + level + [col]].rename(columns={col: "r"}), on=["ts"] + level,
                         suffixes=("_c", "_e")).dropna()
            if len(m) < 50 or m.r_c.std() == 0 or m.r_e.std() == 0:
                continue
            rho, p = stats.spearmanr(m.r_c, m.r_e)
            results.append((lag, float(rho), float(p), len(m), m, mode))
    if not results:
        return "untestable", "no overlapping variation between cause and effect", {"grain": level}, e.prior
    strong = [r for r in results if r[2] < CAUSAL_MAX_P and abs(r[1]) >= CAUSAL_MIN_RHO]
    agree = [r for r in strong if want == 0 or np.sign(r[1]) == want]
    oppose = [r for r in strong if want != 0 and np.sign(r[1]) == -want]
    # a lag *range* hypothesis is supported if some lag in the range shows the hypothesised sign
    best = max(agree or oppose or results, key=lambda r: abs(r[1]))
    lag, rho, p, n, m, mode = best
    st = {"best_lag_h": lag, "rho": round(rho, 3), "p": p, "n": n, "grain": level, "mode": mode,
          "per_lag": {f"{r[0]}h/{r[5]}": round(r[1], 3) for r in results}}
    msg = (f"rho={rho:+.2f} at lag {lag}h on {'changes' if mode == 'change' else 'levels'} "
           f"(n={n}, grain {'/'.join(level) or 'total'})")

    caveats = []
    shared_cols = _columns(src) & _columns(dst)
    if shared_cols and src.table == dst.table:
        caveats.append(f"definitional overlap: cause and effect share column(s) {sorted(shared_cols)}")
    if agree and num is not None:
        mm = m.merge(num.rename(columns={"r": "r_n"}), on=["ts"] + level).dropna()
        if len(mm) > 50 and mm.r_n.std() > 0:
            beta = np.polyfit(mm.r_n, mm.r_c, 1)[0]
            partial, _ = stats.spearmanr(mm.r_c - beta * mm.r_n, mm.r_e)
            proxy, _ = stats.spearmanr(mm.r_c, mm.r_n)
            st["rho_partial_on_numerator"] = round(float(partial), 3)
            st["rho_cause_vs_numerator"] = round(float(proxy), 3)
            # a cause that IS (almost) the effect's numerator is a proxy (discounts = f(orders)); a cause that acts
            # THROUGH the numerator (in-stock -> carts) is a mediated cause and must not be flagged
            if abs(proxy) >= 0.8 and abs(partial) < 0.5 * abs(rho):
                caveats.append(f"confounded by volume: controlling for {dst.id}'s numerator drops rho to "
                               f"{partial:+.2f}; the cause tracks the effect's own volume")
    if agree and caveats:
        st["caveats"] = caveats
        return "confounded", f"co-moves ({msg}) but {'; '.join(caveats)}", st, min(e.prior + 0.1, 0.9)
    if agree:
        conf = e.prior + (1 - e.prior) * min(1.0, 2 * abs(rho)) * 0.8
        return "supported", f"supported: {msg}", st, conf
    if oppose:
        return "contradicted", f"sign contradicts the hypothesis ({e.sign}) at every lag: {msg}", st, e.prior * 0.3
    return "weak", f"no clear co-movement in history: {msg}", st, e.prior * 0.8


# ====================================================================== orchestration
def validate_graph(g: BrainGraph, data, as_of: datetime, formula_days: int = 7, causal_days: int = 49,
                   only: set[str] | None = None) -> ValidationSummary:
    """Validate edges using history before `as_of`; writes status, confidence and reasons onto the graph."""
    end = pd.Timestamp(as_of).floor("h") - pd.Timedelta(hours=2)   # skip possibly incomplete hours
    f_start, c_start = end - pd.Timedelta(days=formula_days), end - pd.Timedelta(days=causal_days)
    out = ValidationSummary()
    touched = lambda e: only is None or e.src in only or e.dst in only or e.dst.split("@")[0] in only

    for output in list(g.formulas):
        es = [e for e in g.edges.values() if e.type == "formula" and e.dst == output and touched(e)]
        if not es:
            continue
        n = g.nodes[output]
        if not (n.value_sql or n.numerator_sql or n.table):   # computed from its formula by the data layer
            for e in es:
                g.set_status(e, "seed" if e.status == "seed" else "validated",
                             "computed from this formula by construction; not independently testable")
                out.verdicts[edge_key(e)] = "by_construction"
            continue
        status, reason, st = check_formula(g, data, output, f_start, end)
        for e in es:
            g.set_status(e, "validated" if status == "validated" else "needs_review", reason, st)
            out.verdicts[edge_key(e)] = "validated" if status == "validated" else "failed"

    for e in [e for e in g.edges.values() if e.type == "breakdown" and touched(e)]:
        status, reason, st = check_breakdown(g, data, e.src, e.dimension, f_start, end)
        g.set_status(e, status, reason, st)
        out.verdicts[edge_key(e)] = status

    for e in [e for e in g.edges.values() if e.type == "causal" and touched(e)]:
        verdict, reason, st, conf = check_causal(g, data, e, c_start, end)
        status = {"supported": "validated", "contradicted": "needs_review", "confounded": "needs_review"}.get(verdict, e.status)
        g.set_status(e, status, reason, st, confidence=conf)
        out.verdicts[edge_key(e)] = verdict

    seen_services: dict[str, bool] = {}
    for e in [e for e in g.edges.values() if e.type == "context" and touched(e)]:
        if e.src.startswith(TABLE):
            t = e.src[len(TABLE):]
            ok = data.completeness(t, end) > 0
            g.set_status(e, "validated" if ok else "needs_review",
                         "source table has rows" if ok else f"no rows found for table {t}")
        else:
            s = e.src[len(SERVICE):]
            if s not in seen_services:
                seen_services[s] = bool(data.events(c_start, end, services=[s]))
            ok = seen_services[s]
            g.set_status(e, "validated" if ok else "needs_review",
                         "service appears in the events table" if ok else f"service {s!r} never appears in events")
        out.verdicts[edge_key(e)] = "validated" if ok else "needs_review"
    return out
