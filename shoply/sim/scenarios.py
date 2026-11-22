"""Loading scenarios.yaml and resolving which effects apply to an hour and a row."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import yaml

from shoply.contracts import Event, seg_key
from shoply.sim import params as P

DEFAULT_PATH = Path(__file__).with_name("scenarios.yaml")
SCOPE_DIMS = ("country", "platform", "channel", "category", "advertiser")


def _parse_ts(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@dataclass(frozen=True)
class Scenario:
    id: str
    start: datetime
    end: Optional[datetime] = None
    scope: dict = field(default_factory=dict)
    table: Optional[str] = None
    effects: dict = field(default_factory=dict)
    event: Optional[dict] = None
    end_event: Optional[dict] = None

    def __post_init__(self):
        for k in self.scope:
            if k not in SCOPE_DIMS:
                raise ValueError(f"{self.id}: unknown scope dimension {k!r}")
        for k in self.effects:
            if k not in P.MULTIPLICATIVE_EFFECTS and k not in P.ABSOLUTE_EFFECTS:
                raise ValueError(f"{self.id}: unknown effect {k!r}")

    def active(self, ts: datetime) -> bool:
        return self.start <= ts and (self.end is None or ts < self.end)

    def duration(self):
        return None if self.end is None else self.end - self.start

    def shifted(self, start: datetime, suffix: str) -> "Scenario":
        d = self.duration()
        return replace(self, id=f"{self.id}{suffix}", start=start,
                       end=None if d is None else start + d)

    def _event(self, spec: dict, ts: datetime, eid: str) -> Event:
        return Event(
            id=eid, ts=ts, type=spec["type"], service=spec.get("service"),
            scope={k: str(v) for k, v in self.scope.items()},
            description=str(spec.get("description", "")),
            planned=bool(spec.get("planned", False)),
            expected_effect={k: float(v) for k, v in (spec.get("expected_effect") or {}).items()},
        )

    def events(self) -> list[Event]:
        out = []
        if self.event:
            out.append(self._event(self.event, self.start, self.id))
        if self.end_event and self.end is not None:
            out.append(self._event(self.end_event, self.end, f"{self.id}_end"))
        return out


def load_scenarios(path: str | Path = DEFAULT_PATH) -> list[Scenario]:
    with open(path) as f:
        raw = yaml.safe_load(f) or []
    out = []
    for r in raw:
        effects = {}
        for k, v in (r.get("effects") or {}).items():
            # YAML 1.1 reads `off` as False.
            effects[k] = "off" if v is False else ("on" if v is True else v)
        out.append(Scenario(
            id=r["id"], start=_parse_ts(r["start"]), end=_parse_ts(r.get("end")),
            scope={k: str(v) for k, v in (r.get("scope") or {}).items()},
            table=r.get("table"), effects=effects,
            event=r.get("event"), end_event=r.get("end_event"),
        ))
    return out


class ScenarioSet:
    """The scenarios a simulator run uses, plus any injected live."""

    def __init__(self, scenarios: list[Scenario]):
        self.scenarios = list(scenarios)
        self._reset()

    def _reset(self) -> None:
        self._active_ts: Optional[datetime] = None
        self._active: list[Scenario] = []
        self._events: Optional[dict[datetime, list[Event]]] = None

    def add(self, s: Scenario) -> None:
        self.scenarios.append(s)
        self._reset()

    def active(self, ts: datetime) -> list[Scenario]:
        if ts != self._active_ts:
            self._active_ts, self._active = ts, [s for s in self.scenarios if s.active(ts)]
        return self._active

    def multiplier(self, effect: str, ts: datetime, grid: dict[str, np.ndarray], n: int) -> np.ndarray:
        """Product of all active multiplicative `effect`s, per row of a grid.

        `grid` maps a dimension to the per-row values; a scenario applies to a row
        only when the table has every scoped dimension and all values match.
        """
        m = np.ones(n)
        for s in self.active(ts):
            if effect in s.effects:
                mask = _scope_mask(s.scope, grid, n)
                if mask is not None:
                    m = np.where(mask, m * float(s.effects[effect]), m)
        return m

    def override(self, effect: str, ts: datetime, grid: dict[str, np.ndarray], n: int,
                 default) -> np.ndarray:
        """Absolute effects: the last active scenario that sets `effect` wins."""
        out = np.full(n, default, dtype=object if isinstance(default, str) else float)
        for s in self.active(ts):
            if effect in s.effects:
                mask = _scope_mask(s.scope, grid, n)
                if mask is not None:
                    out = np.where(mask, s.effects[effect], out)
        return out

    def table_completeness(self, table: str, ts: datetime) -> float:
        c = 1.0
        for s in self.active(ts):
            if "completeness" in s.effects and (s.table is None or s.table == table):
                c *= float(s.effects["completeness"])
        return c

    def events_at(self, ts: datetime) -> list[Event]:
        if self._events is None:
            self._events = {}
            for s in self.scenarios:
                for e in s.events():
                    self._events.setdefault(e.ts, []).append(e)
        return list(self._events.get(ts, []))


def _scope_mask(scope: dict, grid: dict[str, np.ndarray], n: int) -> Optional[np.ndarray]:
    mask = np.ones(n, dtype=bool)
    for dim, val in scope.items():
        if dim not in grid:
            return None
        mask &= grid[dim] == val
    return mask


__all__ = ["Scenario", "ScenarioSet", "load_scenarios", "seg_key"]
