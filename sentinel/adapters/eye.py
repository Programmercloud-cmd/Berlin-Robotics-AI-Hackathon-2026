"""Adapter: run the Sentinel agent on the Eye of the System data layer (PostgreSQL + OCEL 2.0, branch jacob/v1).

The agent only needs metric(), completeness(), events() and schema(). Eye already materialises every
catalog metric per closed hourly window into `eye.observations` (value, sample size, numerator /
denominator, quality, per scope), so this adapter reads those rows and re-aggregates them:

  * a requested slice (filters + group_by) is served from the materialised scope with exactly those
    keys, or re-aggregated from a finer materialised scope (sum of counts, ratio of summed numerators
    and denominators); non-additive metrics (p95 lag, completeness) are only served exactly;
  * completeness(source, hour) reads Eye's `data_completeness_ratio` per source_id (manifests);
  * events() returns context events (Eye's simulator publishes none yet);
  * schema() lists every source's dimensions and the event activities it carries.

`build_graph()` turns Eye's catalog definitions into graph nodes, and `profile` tells the agent which
metrics carry money and volume. Formula edges come from formulas_from_eye (PM4Py + catalog components).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from sentinel.brain.graph import BrainGraph
from sentinel.contracts import Event, MetricNode

ADDITIVE_UNITS = {"count", "units", "EUR cents"}
RATIO_UNITS = {"ratio", "EUR cents per click", "seconds per attempt"}

EYE_PROFILE = {"money_metric": "platform_fee_revenue", "gmv_metric": "gross_merchandise_value",
               "orders_metric": "orders", "money_scale": 0.01, "money_label": "platform fee revenue",
               "key_metrics": ["orders", "gross_merchandise_value", "session_conversion_rate",
                               "checkout_completion_rate", "payment_success_rate", "sessions", "search_click_rate"]}


def import_eye(eye_path: str | Path):
    p = str(Path(eye_path).resolve())
    if p not in sys.path:
        sys.path.insert(0, p)
    import warnings
    warnings.filterwarnings("ignore")
    import eye  # noqa: F401
    return eye


def _naive(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_convert("UTC").tz_localize(None) if t.tzinfo else t


def _aware(ts):
    return _naive(ts).tz_localize("UTC").to_pydatetime()


class EyeData:
    """Implements the agent's data interface on an Eye database."""

    def __init__(self, dsn: str, eye_path: str | Path, now=None):
        import_eye(eye_path)
        from eye.db import connect
        self.dsn = dsn
        self.db = connect(dsn)
        self.db.commit()   # connect() already ran a statement; close that transaction first
        self.db.autocommit = True
        from eye.metric_catalog import CATALOG
        self.catalog = CATALOG
        self.now = _naive(now) if now is not None else self.last_window_end()
        self._keysets: dict[str, list[frozenset]] = {}
        self._cache: dict = {}
        self.virtual: dict[str, tuple[str, str]] = {}   # derived ratio metrics computed from two registered ones
        self.agent: dict[str, dict] = {}                 # agent-only metrics stored in sentinel.observations

    def add_virtual_ratio(self, vid: str, numerator: str, denominator: str):
        self.virtual[vid] = (numerator, denominator)

    # ------------------------------------------------------------------ clock
    def last_window_end(self) -> pd.Timestamp:
        row = self.db.execute("select max(end_at) as t from windows").fetchone()
        return _naive(row["t"]) if row and row["t"] else pd.Timestamp("2026-11-21 09:00")

    def registered(self) -> list[dict]:
        return self.db.execute("select * from metrics order by id").fetchall()

    def register_node(self, node, formula=None):
        """Nodes created by the agent's meta-agent are computed by Eye after registration (see register_metric)."""

    # ------------------------------------------------------------------ metric()
    def add_agent_metric(self, definition: dict):
        self.agent[definition["id"]] = definition
        self.forget(definition["id"])

    def forget(self, metric_id: str):
        self._keysets.pop(metric_id, None)
        self._cache = {k: v for k, v in self._cache.items() if k[0] != metric_id}

    def _table(self, metric_id: str) -> str:
        return "sentinel.observations" if metric_id in self.agent else "observations"

    def _unit(self, metric_id: str) -> str:
        d = self.agent.get(metric_id) or self.catalog.get(metric_id, {})
        return d.get("access", {}).get("unit", "count")

    def _rows(self, metric_id: str, start, end) -> pd.DataFrame:
        key = (metric_id, str(start), str(end), str(self.now))
        if key not in self._cache:
            rows = self.db.execute(
                f"""select window_start, scope, value, sample_size, details from {self._table(metric_id)}
                   where metric_id = %s and window_start >= %s and window_start < %s""",
                (metric_id, _aware(start), _aware(min(_naive(end), self.now))),
            ).fetchall()
            self._cache = {k: v for k, v in self._cache.items() if k[3] == str(self.now)}
            self._cache[key] = pd.DataFrame(rows)
        return self._cache[key]

    def keysets(self, metric_id: str) -> list[frozenset]:
        if metric_id not in self._keysets:
            rows = self.db.execute(
                f"""select distinct array(select jsonb_object_keys(scope) order by 1) as k
                   from {self._table(metric_id)} where metric_id = %s""", (metric_id,)).fetchall()
            self._keysets[metric_id] = sorted({frozenset(r["k"]) for r in rows}, key=len)
        return self._keysets[metric_id]

    def metric(self, id: str, start, end, filters: dict | None = None, group_by: list | None = None) -> pd.DataFrame:
        filters, group_by = dict(filters or {}), list(group_by or [])
        if id in self.virtual:
            num_id, den_id = self.virtual[id]
            keys = ["ts"] + group_by
            a = self.metric(num_id, start, end, filters, group_by)[keys + ["value", "complete"]]
            b = self.metric(den_id, start, end, filters, group_by)[keys + ["value", "complete"]]
            m = a.merge(b, on=keys, suffixes=("_n", "_d"))
            m["numerator"], m["denominator"] = m.value_n, m.value_d
            m["value"] = m.value_n / m.value_d.where(m.value_d != 0)
            m["complete"] = m.complete_n & m.complete_d
            return m[keys + ["value", "numerator", "denominator", "complete"]]
        need = frozenset(filters) | frozenset(group_by)
        cols = ["ts"] + group_by + ["value", "numerator", "denominator", "complete"]
        df = self._rows(id, start, end)
        if df.empty:
            return pd.DataFrame(columns=cols)
        unit = self._unit(id)
        additive = unit in ADDITIVE_UNITS or unit in RATIO_UNITS
        usable = [k for k in self.keysets(id) if k >= need and (k == need or additive)]
        if not usable:
            raise ValueError(f"{id}: no materialised scope covers {sorted(need)}")
        keyset = usable[0]
        d = df[df.scope.map(lambda s: frozenset(s) == keyset)].copy()
        for k, v in filters.items():
            d = d[d.scope.map(lambda s, k=k, v=v: str(s.get(k)) == str(v))]
        if d.empty:
            return pd.DataFrame(columns=cols)
        for g in group_by:
            d[g] = d.scope.map(lambda s, g=g: str(s.get(g)))
        d["ts"] = d.window_start.map(_naive)
        det = d.details
        d["complete"] = det.map(lambda x: (x or {}).get("quality", "complete") in ("complete", "zero_denominator", "no_events"))
        if unit in RATIO_UNITS:
            d["numerator"] = det.map(lambda x: (x or {}).get("numerator"))
            d["denominator"] = det.map(lambda x: (x or {}).get("denominator"))
        else:
            d["numerator"], d["denominator"] = d.value, 1.0
        d["numerator"] = pd.to_numeric(d.numerator, errors="coerce")
        d["denominator"] = pd.to_numeric(d.denominator, errors="coerce")
        d["value"] = pd.to_numeric(d.value, errors="coerce")
        keys = ["ts"] + group_by
        if keyset != need:   # re-aggregate a finer scope
            agg = d.groupby(keys).agg(numerator=("numerator", "sum"), denominator=("denominator", "sum"),
                                      value=("value", "sum"), complete=("complete", "all")).reset_index()
            if unit in RATIO_UNITS:
                agg["value"] = agg.numerator / agg.denominator.where(agg.denominator != 0)
            else:
                agg["numerator"], agg["denominator"] = agg.value, 1.0
            d = agg
        return d[cols].sort_values(keys).reset_index(drop=True)

    # ------------------------------------------------------------------ data quality, events, schema
    def completeness(self, table: str, ts) -> float:
        t = _naive(ts)
        if t >= self.now:
            return 0.0
        rows = self.db.execute(
            """select scope, value from observations where metric_id = 'data_completeness_ratio'
               and window_start = %s""", (_aware(t),)).fetchall()
        if not rows:
            w = self.db.execute("select 1 from windows where start_at = %s", (_aware(t),)).fetchone()
            return 1.0 if w else 0.0
        by_source = {r["scope"].get("source_id"): r["value"] for r in rows if set(r["scope"]) == {"source_id"}}
        total = next((r["value"] for r in rows if not r["scope"]), 1.0)
        v = by_source.get(table, total)
        return float(v) if v is not None else 0.0

    def events(self, start, end, services=None) -> list[Event]:
        return []   # Eye publishes no release / campaign context yet; the agent works from observations

    def schema(self) -> dict:
        out = defaultdict(lambda: {"dimensions": [], "measures": []})
        for d in self.catalog.values():
            src = d["access"]["source"]
            out[src]["dimensions"] = sorted(set(out[src]["dimensions"]) | set(d["dimensions"]))
        return dict(out)

    # ------------------------------------------------------------------ process map (PM4Py OC-DFG in Eye's graph)
    def process_activities(self) -> list[dict]:
        """Every activity of the discovered OC-DFG with its volume, whether any metric measures it, and its
        most frequent predecessor / successor in the session and product lifecycles."""
        measured = {r["target"].split(":", 1)[1] for r in self.db.execute(
            "select target from graph_edges where kind = 'measures'")}
        for d in self.agent.values():
            c = d["calculation"]
            measured |= {c.get("activity"), c.get("denominator_activity")} - {None}
        counts = {r["activity"]: r["n"] for r in self.db.execute(
            "select activity, count(*) as n from events group by activity")}
        prev, nxt = defaultdict(list), defaultdict(list)
        for r in self.db.execute("""select source, target, properties from graph_edges
                                    where kind = 'precedes' and provenance = 'pm4py.ocdfg'"""):
            a, b = r["source"].split(":", 1)[1], r["target"].split(":", 1)[1]
            ots = r["properties"].get("object_types") or {}
            n = max(ots.get("session", 0), ots.get("product", 0))
            if a != b and n:
                nxt[a].append((n, b))
                prev[b].append((n, a))
        out = []
        for r in self.db.execute("select id from graph_nodes where kind = 'activity' order by id"):
            a = r["id"].split(":", 1)[1]
            def best(cands, a=a):
                # the step a request/failure pair belongs to shares its name prefix (product_page_requested ->
                # product_page_load_failed); frequency of the direct-follows relation breaks ties
                def prefix(b):
                    n = 0
                    for x, y in zip(a.split("_"), b.split("_")):
                        if x != y:
                            break
                        n += 1
                    return n
                ranked = sorted(cands, key=lambda c: (prefix(c[1]), c[0]), reverse=True)
                return [c[1] for c in ranked]
            out.append({"activity": a, "events": counts.get(a, 0), "measured": a in measured,
                        "predecessor": (best(prev[a]) or [None])[0], "successor": (best(nxt[a]) or [None])[0],
                        "successors": best(nxt[a])})
        return out

    def activity_dimensions(self, activity: str, candidates: list[str]) -> list[str]:
        row = self.db.execute("select attrs from events where activity = %s limit 1", (activity,)).fetchone()
        return [d for d in candidates if row and d in row["attrs"]]

    # ------------------------------------------------------------------ event-level evidence (OCEL)
    def activities_for(self, metric_id: str) -> list[str]:
        """Activities a metric is computed from: Eye's `measures` edges plus the catalog's activity lists."""
        if metric_id in self.virtual:
            return sorted({a for m in self.virtual[metric_id] for a in self.activities_for(m)})
        calc = self.catalog.get(metric_id, {}).get("calculation", {})
        acts = set(calc.get("activities", [])) | {calc[k] for k in ("activity", "denominator_activity") if calc.get(k)}
        acts |= {r["target"].split(":", 1)[1] for r in self.db.execute(
            "select target from graph_edges where kind = 'measures' and source = %s", (f"metric:{metric_id}",))}
        if any(a.startswith("payment_") and a != "payment_attempted" for a in acts):
            acts.add("payment_attempted")
        return sorted(acts)

    def activity_mix(self, start, end, scope: dict, activities: list[str], baseline_days: int = 7) -> list[dict]:
        """Counts per activity inside the segment, now vs the same hours on previous days."""
        s, e = _naive(start), min(_naive(end), self.now)
        cur = {r["activity"]: r["n"] for r in self.db.execute(
            """select activity, count(*) as n from events where activity = any(%s) and attrs @> %s
               and occurred_at >= %s and occurred_at < %s group by activity""",
            (activities, json.dumps(scope), _aware(s), _aware(e)))}
        past = defaultdict(float)
        for d in range(1, baseline_days + 1):
            for r in self.db.execute(
                    """select activity, count(*) as n from events where activity = any(%s) and attrs @> %s
                       and occurred_at >= %s and occurred_at < %s group by activity""",
                    (activities, json.dumps(scope), _aware(s - timedelta(days=d)), _aware(e - timedelta(days=d)))):
                past[r["activity"]] += r["n"] / baseline_days
        return [{"activity": a, "now": int(cur.get(a, 0)), "baseline": round(past.get(a, 0.0), 1)} for a in activities]

    def supporting_events(self, start, end, scope: dict, activity: str, limit: int = 3) -> list[dict]:
        rows = self.db.execute(
            """select id, activity, occurred_at, attrs from events where activity = %s and attrs @> %s
               and occurred_at >= %s and occurred_at < %s order by occurred_at limit %s""",
            (activity, json.dumps(scope), _aware(start), _aware(min(_naive(end), self.now)), limit)).fetchall()
        keep = ("response", "http_status", "payment_provider", "checkout_release_version", "result_count")
        return [{"id": r["id"], "activity": r["activity"], "at": str(_naive(r["occurred_at"])),
                 "attrs": {k: v for k, v in r["attrs"].items() if k in keep}} for r in rows]

    # ------------------------------------------------------------------ driving Eye's simulator
    def emit_and_observe(self, clock, **kwargs):
        """Publish one simulated hour through Eye's simulator and materialise its observations."""
        from eye import monitor, simulator
        simulator.emit(self.db, _aware(clock), **kwargs)
        monitor.materialize(self.db, window_start=_aware(clock))
        self.now = _naive(clock) + timedelta(hours=1)


def node_from_catalog(d: dict) -> MetricNode:
    unit = d["access"]["unit"]
    method = d["calculation"].get("method", "")
    if method in ("lag", "completeness") or method.startswith("inventory") or method == "budget":
        kind = "gauge"
    elif unit in RATIO_UNITS:
        kind = "ratio"
    elif unit == "EUR cents":
        kind = "sum"
    else:
        kind = "count"
    good = {"down": "up", "up": "down"}.get(d.get("direction"), "up")
    return MetricNode(id=d["id"], name=d["name"], description=d["definition"], kind=kind,
                      unit={"ratio": "share", "EUR cents": "eur", "EUR cents per click": "eur",
                            "seconds": "seconds", "seconds per attempt": "seconds"}.get(unit, "count"),
                      table=d["access"]["source"], dimensions=list(d["dimensions"]), good_direction=good,
                      owner=d["access"]["source"], status="seed")


def build_graph(data: EyeData) -> BrainGraph:
    """Graph nodes for every metric registered in Eye (breakdown and context edges included)."""
    g = BrainGraph()
    g.profile.update(EYE_PROFILE)
    for m in data.registered():
        d = data.catalog.get(m["id"])
        if d is None:   # custom registration: derive from its stored definition
            d = {"id": m["id"], "name": m["name"], "definition": m["definition"], "calculation": m["calculation"],
                 "dimensions": m["dimensions"], "direction": m["direction"], "access": {**m["access"],
                 "unit": m["access"].get("unit", "ratio" if m["calculation"].get("op") == "ratio" else "count"),
                 "source": m["access"].get("source", "events")}}
        g.add_node(node_from_catalog(d), actor="eye-catalog")
    return g
