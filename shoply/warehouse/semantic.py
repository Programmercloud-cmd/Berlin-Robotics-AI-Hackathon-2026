"""Semantic layer: metric(), completeness() and events() over the DuckDB warehouse.

SQL is generated from the metric graph (metrics.yaml). Fact tables hold additive counts and EUR sums only,
so every ratio is SUM(numerator) / SUM(denominator) at the requested grain, never an average of ratios.
"""
from __future__ import annotations

import ast
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Union, get_args

import duckdb
import numpy as np
import pandas as pd

from shoply.contracts import Dim, Event, Segment

DIMS: tuple[str, ...] = get_args(Dim)
COMPLETE_THRESHOLD = 0.95
LOCAL_DAY = "CAST(timezone('Europe/Berlin', timezone('UTC', ts)) AS DATE)"   # ts is a naive UTC hour start
FrameColumns = ["ts", "value", "numerator", "denominator", "complete"]
FilterValue = Union[str, Iterable[str]]
_ARITH = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide}
_WINDOW = re.compile(r"PARTITION\s+BY\s+(.*?)\s+ORDER\s+BY", re.IGNORECASE | re.DOTALL)


def _utc(t: Any) -> datetime:
    """datetime/str -> naive UTC datetime (the warehouse convention)."""
    ts = pd.Timestamp(t)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.to_pydatetime()


def expr_names(expr: str) -> list[str]:
    """Metric names in expr, left to right. Only names, parentheses and + - * / are allowed."""
    try:
        body = ast.parse(expr, mode="eval").body
    except SyntaxError as e:
        raise ValueError(f"cannot parse {expr!r}: {e.msg}") from None
    names: list[str] = []

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.BinOp) and type(node.op) in _ARITH:
            walk(node.left), walk(node.right)
        elif isinstance(node, ast.Name):
            names.append(node.id) if node.id not in names else None
        else:
            raise ValueError(f"{expr!r}: only metric names and + - * / are allowed")

    walk(body)
    return names


def eval_expr(expr: str, values: dict[str, Any]) -> Any:
    """Evaluate expr over arrays/Series (names -> values). Division by zero gives NaN."""
    expr_names(expr)

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Name):
            return values[node.id]
        with np.errstate(divide="ignore", invalid="ignore"):
            out = _ARITH[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(out, pd.Series):
            return out.replace([np.inf, -np.inf], np.nan)
        return np.where(np.isinf(out), np.nan, out) if np.ndim(out) else (np.nan if np.isinf(out) else out)

    return ev(ast.parse(expr, mode="eval").body)


def normalize_filters(filters: Optional[dict[str, FilterValue]]) -> dict[str, Union[str, list[str]]]:
    """Copy filters, materialising iterables (generators, sets) into lists so they can be read twice."""
    return {d: v if isinstance(v, str) else list(v) for d, v in (filters or {}).items()}


def parse_segment(text: Optional[str]) -> Segment:
    """'country=de|platform=android' -> {'country': 'de', 'platform': 'android'}; '' -> {}."""
    return dict(part.split("=", 1) for part in (text or "").split("|") if part)


class Semantic:
    """Typed, validated access to the warehouse. One instance per DuckDB connection."""

    def __init__(self, con: Union[str, Path, duckdb.DuckDBPyConnection] = ":memory:", graph=None):
        self.con = con if isinstance(con, duckdb.DuckDBPyConnection) else duckdb.connect(str(con))
        if graph is None:
            from shoply.graph.loader import load_graph
            graph = load_graph()
        self.graph = graph

    def _df(self, sql: str, params: list) -> pd.DataFrame:
        return self.con.execute(sql, params).df()           # .df() consumes the result (DuckDB 2.x)

    # ---------------------------------------------------------------- metric()
    def metric(self, id: str, start: Any, end: Any, filters: Optional[dict[str, FilterValue]] = None,
               group_by: Optional[list[str]] = None) -> pd.DataFrame:
        """Hourly MetricFrame for ts in [start, end): ts, *group_by, value, numerator, denominator, complete.

        Counts/sums: numerator = value, denominator = 1. Ratios/gauges: SUM(num) / SUM(den) per row.
        complete = every source table has rows / expected_rows >= 0.95 for that hour (ingestion_log).
        Raises ValueError for a dimension the metric does not list or an unknown dimension value.
        """
        node, extra = self.graph.metric(id), self.graph.extra(id)
        filters, group_by = normalize_filters(filters), list(group_by or [])
        self._check_slicing(id, node.dimensions, filters, group_by)
        start, end = _utc(start), _utc(end)
        filters = {**node.filters, **filters}
        if node.table == "derived":
            frame = self.evaluate(extra["expr"], start, end, filters, group_by)
            frame["numerator"], frame["denominator"] = frame["value"], 1.0
            return frame[["ts", *group_by, *FrameColumns[1:]]]

        if node.kind in ("count", "sum"):
            sides = {"numerator": (node.table, node.value_sql), "denominator": (node.table, "1.0")}
        elif node.table == "cross_table":
            sides = {k: (extra[k]["table"], extra[k]["sql"]) for k in ("numerator", "denominator")}
        else:
            sides = {"numerator": (node.table, node.numerator_sql), "denominator": (node.table, node.denominator_sql)}

        tables = list(dict.fromkeys(t for t, _ in sides.values()))
        if len(tables) == 1:
            sql, params = self._aggregate(tables[0], {k: s for k, (_, s) in sides.items()}, start, end, filters, group_by)
        else:                                            # aggregate each table to ts + group_by first, then join
            keys = ["ts", *group_by]
            parts = {k: self._aggregate(t, {k: s}, start, end, filters, group_by) for k, (t, s) in sides.items()}
            on = " AND ".join(f"n.{c} IS NOT DISTINCT FROM d.{c}" for c in keys)
            sql = (f"SELECT {', '.join(f'COALESCE(n.{c}, d.{c}) AS {c}' for c in keys)}, "
                   f"COALESCE(n.numerator, 0) AS numerator, COALESCE(d.denominator, 0) AS denominator "
                   f"FROM ({parts['numerator'][0]}) n FULL OUTER JOIN ({parts['denominator'][0]}) d ON {on}")
            params = parts["numerator"][1] + parts["denominator"][1]

        frame = self._df(f"SELECT * FROM ({sql}) ORDER BY {', '.join(['ts', *group_by])}", params)
        frame["numerator"] = frame["numerator"].astype(float)
        frame["denominator"] = frame["denominator"].astype(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            frame["value"] = (frame["numerator"] / frame["denominator"]).where(frame["denominator"] != 0)
        frame["complete"] = self._complete(frame["ts"], self.graph.tables(id), start, end)
        return frame[["ts", *group_by, *FrameColumns[1:]]]

    def evaluate(self, expr: str, start: Any, end: Any, filters: Optional[dict[str, FilterValue]] = None,
                 group_by: Optional[list[str]] = None) -> pd.DataFrame:
        """expr over metric values at the requested grain: ts, *group_by, value, complete (AND across inputs)."""
        filters, group_by = normalize_filters(filters), list(group_by or [])
        keys = ["ts", *group_by]
        merged = None
        for name in expr_names(expr):
            f = self.metric(name, start, end, filters, group_by)[[*keys, "value", "complete"]]
            f = f.rename(columns={"value": name, "complete": f"__c_{name}"})
            merged = f if merged is None else merged.merge(f, on=keys, how="outer")
        names = expr_names(expr)
        merged["value"] = eval_expr(expr, {n: merged[n].astype(float) for n in names}).astype(float)
        merged["complete"] = merged[[f"__c_{n}" for n in names]].fillna(False).astype(bool).all(axis=1)
        return merged.sort_values(keys, ignore_index=True)[[*keys, "value", "complete"]]

    def _check_slicing(self, id: str, dims: list[str], filters: dict, group_by: list[str]) -> None:
        if len(set(group_by)) != len(group_by):
            raise ValueError(f"group_by repeats a dimension: {group_by}")
        for d in [*filters, *group_by]:
            if d not in DIMS or d not in dims:
                raise ValueError(f"{id} cannot be sliced by {d!r}; allowed: {dims}")
        known = self.graph.dimension_values
        for d, v in filters.items():
            values = [v] if isinstance(v, str) else list(v)
            bad = [x for x in values if d in known and x not in known[d]]
            if bad or not values:
                raise ValueError(f"{id}: invalid {d} filter {v!r}; allowed: {known.get(d)}")

    @staticmethod
    def _where(filters: dict[str, FilterValue]) -> tuple[str, list]:
        sql, params = "", []
        for d, v in filters.items():                      # d is a validated dimension name; values are parameters
            values = [v] if isinstance(v, str) else list(v)
            sql += f" AND {d} IN ({', '.join('?' * len(values))})"
            params += values
        return sql, params

    def _aggregate(self, table: str, exprs: dict[str, str], start: datetime, end: datetime,
                   filters: dict, group_by: list[str]) -> tuple[str, list]:
        """SELECT ts, *group_by, <expr> AS <alias>... for one table, aggregated to ts + group_by."""
        where, wparams = self._where(filters)
        keys = ", ".join(["ts", *group_by])
        if not any(" OVER " in s.upper() for s in exprs.values()):
            cols = ", ".join(f"{s} AS {a}" for a, s in exprs.items())
            return (f"SELECT {keys}, {cols} FROM {table} WHERE ts >= ? AND ts < ?{where} GROUP BY {keys}",
                    [start, end, *wparams])
        # Window metrics (e.g. pacing_ratio): cumulative sums per partition, with group_by dims added to the
        # partition; rows start one day early so each Berlin local day is complete, then summed per grain.
        part = list(dict.fromkeys(c.strip() for s in exprs.values() for m in _WINDOW.findall(s) for c in m.split(",")))
        extra_part = [d for d in group_by if d not in part]
        cols = ", ".join(f"{self._partition(s, extra_part)} AS {a}" for a, s in exprs.items())
        inner_keys = ", ".join(dict.fromkeys(["ts", *group_by, *part]))
        inner = (f"SELECT {inner_keys}, {cols} FROM (SELECT *, {LOCAL_DAY} AS local_day FROM {table} "
                 f"WHERE ts >= ? AND ts < ?{where}) GROUP BY {inner_keys}")
        sums = ", ".join(f"SUM({a}) AS {a}" for a in exprs)
        return (f"SELECT {keys}, {sums} FROM ({inner}) WHERE ts >= ? GROUP BY {keys}",
                [start - timedelta(days=1), end, *wparams, start])

    @staticmethod
    def _partition(sql: str, dims: list[str]) -> str:
        return re.sub(r"PARTITION\s+BY\s+", lambda m: m.group(0) + "".join(f"{d}, " for d in dims), sql,
                      flags=re.IGNORECASE) if dims else sql

    def _complete(self, ts: pd.Series, tables: list[str], start: datetime, end: datetime) -> pd.Series:
        log = self._df(f"SELECT table_name, ts, rows, expected_rows FROM ingestion_log WHERE ts >= ? AND ts < ? "
                       f"AND table_name IN ({', '.join('?' * len(tables))})", [start, end, *tables])
        ok = (log["expected_rows"] == 0) | (log["rows"] >= COMPLETE_THRESHOLD * log["expected_rows"])
        good = log[ok].groupby("ts")["table_name"].nunique()
        complete_ts = set(good[good == len(tables)].index)
        return ts.isin(complete_ts).astype(bool)

    # ------------------------------------------------------- completeness()
    def completeness(self, table: str, ts: Any) -> float:
        """rows / expected_rows for one table and hour (0..1); 0.0 when nothing was logged."""
        rows = self._df("SELECT rows, expected_rows FROM ingestion_log WHERE table_name = ? AND ts = ?",
                        [table, _utc(ts)])
        if rows.empty:
            return 0.0
        got, expected = float(rows["rows"].sum()), float(rows["expected_rows"].sum())
        return 1.0 if expected == 0 else min(got / expected, 1.0)

    # ------------------------------------------------------------- events()
    def events(self, start: Any, end: Any, services: Optional[list[str]] = None) -> list[Event]:
        """Events with ts in [start, end), oldest first; optionally only those of the given services."""
        sql, params = ("SELECT id, ts, type, service, scope, description, planned, CAST(expected_effect AS VARCHAR) "
                       "AS expected_effect FROM events WHERE ts >= ? AND ts < ?"), [_utc(start), _utc(end)]
        if services is not None:
            if not services:
                return []
            sql += f" AND service IN ({', '.join('?' * len(services))})"
            params += list(services)
        out = []
        for r in self._df(sql + " ORDER BY ts, id", params).to_dict("records"):
            effect = json.loads(r["expected_effect"]) if isinstance(r["expected_effect"], str) else {}
            out.append(Event(id=r["id"], ts=pd.Timestamp(r["ts"]).to_pydatetime(), type=r["type"],
                             service=r["service"] if isinstance(r["service"], str) else None,
                             scope=parse_segment(r["scope"] if isinstance(r["scope"], str) else ""),
                             description=r["description"] or "", planned=bool(r["planned"]),
                             expected_effect=effect or {}))
        return out


# ---------- module-level API (contracts.metric / completeness / events) ----------
_default: Optional[Semantic] = None


def connect(db: Union[str, Path, duckdb.DuckDBPyConnection, None] = None, graph=None) -> Semantic:
    """Set the connection used by the module functions. Default: $SHOPLY_DB or shoply.duckdb."""
    global _default
    _default = Semantic(db if db is not None else os.environ.get("SHOPLY_DB", "shoply.duckdb"), graph)
    return _default


def default() -> Semantic:
    return _default or connect()


def metric(id: str, start: datetime, end: datetime, filters: Optional[Segment] = None,
           group_by: Optional[list[str]] = None) -> pd.DataFrame:
    return default().metric(id, start, end, filters, group_by)


def completeness(table: str, ts: datetime) -> float:
    return default().completeness(table, ts)


def events(start: datetime, end: datetime, services: Optional[list[str]] = None) -> list[Event]:
    return default().events(start, end, services)
