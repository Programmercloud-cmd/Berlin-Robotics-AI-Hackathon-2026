"""Sandbox helpers: evaluate a candidate metric without writing it into the graph."""
from __future__ import annotations

from sentinel.monitor.baseline import SeriesBaseline


def baseline_for(node, data, monitor, seg: dict) -> SeriesBaseline:
    df = data.metric(node.id, monitor.h0, monitor.h1, filters=seg)
    return SeriesBaseline(node.kind, node.unit, df)
