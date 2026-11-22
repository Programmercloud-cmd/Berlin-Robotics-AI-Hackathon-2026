"""check_formula: does `output = expr` hold on warehouse data, in total and in every slice?"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

TOLERANCE = 0.005          # 0.5 % relative error
MIN_SHARE = 0.95           # share of rows that must be within TOLERANCE


@dataclass
class SliceResult:
    segment: str               # seg_key: "" = total, "country=de"
    rows: int                  # hours compared (both sides defined)
    median_rel_error: float
    share_within: float
    holds: bool
    skipped: int = 0           # hours where a side is undefined (0/0) or missing


@dataclass
class FormulaCheck:
    output: str
    expr: str
    dimensions: list[str]      # shared dimensions that were sliced
    holds: bool
    median_rel_error: float    # over every compared row (total + all slices)
    share_within: float
    slices: list[SliceResult] = field(default_factory=list)

    @property
    def failing_slices(self) -> list[SliceResult]:
        return [s for s in self.slices if not s.holds]


def _verdict(err: pd.Series, tol: float, share: float) -> tuple[float, float, bool]:
    if err.empty:
        return float("nan"), 0.0, False
    med, within = float(err.median()), float((err <= tol).mean())
    return med, within, med < tol and within >= share


def check_formula(output: str, expr: str, start: Any, end: Any, sem=None, filters: Optional[dict] = None,
                  tol: float = TOLERANCE, share: float = MIN_SHARE) -> FormulaCheck:
    """Compare `output` with `expr` hour by hour, in total and per value of every shared dimension.

    A dimension filtered to a single value is not sliced; one filtered to several values is sliced per value.

    A slice holds when median relative error < tol and >= share of its hours are within tol.
    The formula holds when the pooled rows pass and no slice fails. Raises ValueError for an expr that is
    not names and + - * /, or KeyError for an unknown metric.
    """
    from shoply.warehouse.semantic import default, expr_names, normalize_filters

    sem, filters = sem or default(), normalize_filters(filters)
    names = expr_names(expr)
    for m in [output, *names]:
        sem.graph.metric(m)
    pinned = {d for d, v in filters.items() if isinstance(v, str) or len(v) == 1}   # one value: nothing to slice
    dims = [d for d in sem.graph.shared_dims([output, *names]) if d not in pinned]
    slices, pooled = [], []
    for dim in [None, *dims]:
        group_by = [dim] if dim else []
        keys = ["ts", *group_by]
        lhs = sem.metric(output, start, end, filters, group_by)[[*keys, "value"]]
        rhs = sem.evaluate(expr, start, end, filters, group_by)[[*keys, "value"]]
        both = lhs.merge(rhs, on=keys, how="outer", suffixes=("_lhs", "_rhs"))
        a, b = both["value_lhs"].astype(float), both["value_rhs"].astype(float)
        ok = np.isfinite(a) & np.isfinite(b)
        scale = np.maximum(a.abs(), b.abs())
        both["err"] = ((a - b).abs() / scale.where(scale > 0)).fillna(0.0).where(ok)
        for value, part in (both.groupby(dim) if dim else [(None, both)]):
            err = part["err"].dropna()
            med, within, holds = _verdict(err, tol, share)
            slices.append(SliceResult(f"{dim}={value}" if dim else "", len(err), med, within, holds,
                                      int(part["err"].isna().sum())))
            pooled.append(err)
    med, within, holds = _verdict(pd.concat(pooled, ignore_index=True), tol, share)
    return FormulaCheck(output, expr, dims, holds and all(s.holds for s in slices), med, within, slices)
