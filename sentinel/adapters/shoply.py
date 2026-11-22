"""Sentinel on Shoply (Chaimaa's data component, branch `chaimaa`).

Shoply is the team's implementation of the kickoff spec's data side: a deterministic hourly simulator
that writes the six DuckDB tables (fact_traffic, fact_commerce, fact_inventory, fact_ads, events,
ingestion_log), 8 weeks of history, the spec scenarios and ~2 harmless background releases per day.

`ShoplyData` drives that simulator live (moving the agent's clock runs Shoply up to it) and serves the
agent's data interface on its warehouse:

  metric()        the agent's own metric definitions (value_sql / numerator_sql, formulas) are evaluated
                  on Shoply's tables, so metrics the meta-agent registers at run time work unchanged
  completeness()  rows / expected_rows from Shoply's ingestion_log; late partitions arrive one hour later
                  exactly as the simulator delivers them
  events()        Shoply's events table (releases, campaigns, holidays, background releases) up to now

The Shoply checkout is not vendored: `git worktree add .shoply-chaimaa <remote>/chaimaa`.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from dev.fake_data import TABLE_DIMS, FakeData, _ts
from sentinel.contracts import Event, MetricNode

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / ".shoply-chaimaa"


def import_shoply(path: str | Path = DEFAULT_PATH):
    p = str(Path(path).resolve())
    if not (Path(p) / "shoply").exists():
        raise FileNotFoundError(f"no Shoply checkout at {p} (git worktree add .shoply-chaimaa <remote>/chaimaa)")
    if p not in sys.path:
        sys.path.insert(0, p)
    import shoply  # noqa: F401
    return shoply


def shoply_scenarios(path: str | Path = DEFAULT_PATH, active: list[str] | None = None,
                     overrides: dict[str, dict] | None = None) -> list:
    """Shoply's scenario library, optionally restricted to `active` ids and with fault-injection overrides
    (start / end / scope / effects / event fields replaced per scenario id)."""
    import_shoply(path)
    from shoply.sim.scenarios import DEFAULT_PATH as SCEN_PATH, load_scenarios
    out = []
    for s in load_scenarios(SCEN_PATH):
        if active is not None and s.id not in active:
            continue
        upd = dict((overrides or {}).get(s.id, {}))
        for k in ("start", "end"):
            if isinstance(upd.get(k), str):
                upd[k] = _ts(upd[k]).to_pydatetime()
        out.append(replace(s, **upd) if upd else s)
    return out


class ShoplyData(FakeData):
    """The agent's data interface on a live Shoply simulator."""

    def __init__(self, nodes: list[MetricNode], formulas: dict[str, str] | None = None,
                 scenarios: list[str] | None = None, seed: int = 42, now: datetime | None = None,
                 overrides: dict[str, dict] | None = None, shoply_path: str | Path = DEFAULT_PATH):
        import_shoply(shoply_path)
        from shoply.sim import params as P
        from shoply.sim.simulator import Simulator
        self.nodes = {n.id: n for n in nodes}
        self.formulas = dict(formulas or {})
        self.scenario_list = shoply_scenarios(shoply_path, scenarios, overrides)
        self.scenarios = {s.id: s for s in self.scenario_list}
        self.active = [s.id for s in self.scenario_list]
        self.sim = Simulator(":memory:", seed=seed, scenarios=self.scenario_list)
        self.sim.build_history(P.LIVE_START)
        self.con = self.sim.con
        self.tables = {}
        self._cc: dict = {}
        self._now = pd.Timestamp(self.sim.now)
        if now is not None:
            self.now = now

    # ------------------------------------------------------------------ clock
    @property
    def now(self) -> pd.Timestamp:
        return self._now

    @now.setter
    def now(self, t):
        """Moving the agent's clock forward runs Shoply live up to it: each generated hour becomes visible
        when it closes (late partitions of the previous hour arrive with the next one)."""
        t = _ts(t)
        sim = self.__dict__.get("sim")
        while sim is not None and pd.Timestamp(sim.now) < t:
            sim.step()
        self._now = t

    def step(self):
        self.now = self._now + timedelta(hours=1)

    # ------------------------------------------------------------------ interface
    def schema(self) -> dict[str, dict[str, list[str]]]:
        out = {}
        for t, dims in TABLE_DIMS.items():
            cols = [r[0] for r in self.con.execute(f"describe {t}").fetchall() if r[0] != "ts"]
            out[t] = {"dimensions": [c for c in cols if c in dims], "measures": [c for c in cols if c not in dims]}
        return out

    def _completeness_series(self, table: str) -> pd.Series:
        key = (table, self.now)
        if key not in self._cc:
            log = self.con.execute("select ts, rows, expected_rows from ingestion_log where table_name = ?",
                                   [table]).df().set_index("ts")
            ratio = (log["rows"] / log["expected_rows"].where(log["expected_rows"] != 0)).fillna(1.0).clip(upper=1.0)
            self._cc = {k: v for k, v in self._cc.items() if k[1] == self.now}
            self._cc[key] = ratio.astype(float)
        return self._cc[key]

    def events(self, start: datetime, end: datetime, services: list[str] | None = None) -> list[Event]:
        end = min(_ts(end), self.now)
        rows = self.con.execute(
            """select id, ts, type, service, scope, description, planned, cast(expected_effect as varchar) as ee
               from events where ts >= ? and ts < ? order by ts, id""", [_ts(start), end]).df()
        out = []
        for r in rows.to_dict("records"):
            svc = r["service"] if isinstance(r["service"], str) and r["service"] else None
            if services is not None and svc not in services:
                continue
            scope = (dict(p.split("=", 1) for p in r["scope"].split("|"))
                     if isinstance(r["scope"], str) and r["scope"] else {})
            ee = json.loads(r["ee"]) if isinstance(r["ee"], str) and r["ee"] else {}
            out.append(Event(id=r["id"], ts=pd.Timestamp(r["ts"]).to_pydatetime(), type=r["type"], service=svc,
                             scope=scope, description=r["description"] or "", planned=bool(r["planned"]),
                             expected_effect=ee or {}))
        return out

    def _side(self, node: MetricNode, sql: str, start, end, filters, group_by):
        qual = set(re.findall(r"\b(fact_[a-z]+)\.", sql))
        if len(qual) > 1:
            raise ValueError(f"expression mixes tables {qual}: {sql}")
        table = qual.pop() if qual else node.table
        expr = re.sub(r"\bfact_[a-z]+\.", "", sql)
        dims = TABLE_DIMS[table]
        for d in list(filters) + group_by:
            if d not in dims:
                raise ValueError(f"table {table} has no column {d}")
        # what is in Shoply's warehouse has arrived (late rows are appended when they arrive)
        where, params = ["ts >= ?", "ts < ?"], [start, end]
        for k, v in {**node.filters, **filters}.items():
            where.append(f"{k} = ?")
            params.append(v)
        gb = ", ".join(["ts"] + group_by)
        q = f"SELECT {gb}, {expr} AS v FROM {table} WHERE {' AND '.join(where)} GROUP BY {gb}"
        return table, self.con.execute(q, params).df()
