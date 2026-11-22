"""The planned calendar (calendar.yaml) and past incidents, expanded into dated scenarios.

Every entry becomes a `Scenario` with UTC start/end computed from Europe/Berlin local time,
so the simulator applies calendar effects exactly like scenario effects. `ground_truth`
lists every calendar event and injected incident.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import yaml

from shoply.contracts import seg_key
from shoply.sim import params as P
from shoply.sim.scenarios import DEFAULT_PATH as SCENARIOS_PATH, Scenario, load_scenarios

DEFAULT_CALENDAR = Path(__file__).with_name("calendar.yaml")
GROUND_TRUTH_COLUMNS = ["id", "kind", "type", "service", "planned", "start", "end", "scope", "effects",
                        "description"]
TZ = ZoneInfo(P.TIMEZONE)


def utc(v: Any) -> datetime:
    """A calendar date or timestamp (local unless it carries a zone) as a naive UTC datetime."""
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, date):
        dt = datetime(v.year, v.month, v.day)
    else:
        dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _day(v: Any) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _month_starts(first: date, last: date):
    d = date(first.year, first.month, 1)
    while d <= last:
        yield d
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)


@dataclass
class Calendar:
    scenarios: list[Scenario]
    growth_per_year: float = 0.0
    growth_anchor: date = P.HISTORY_START.date()
    kinds: dict[str, str] = field(default_factory=dict)

    def growth(self, day: date) -> float:
        """Trend factor for a local day: (1 + g) ** (days since the anchor / 365)."""
        return (1 + self.growth_per_year) ** ((day - self.growth_anchor).days / 365)

    @property
    def ids(self) -> set[str]:
        return {s.id for s in self.scenarios}


def _planned(sid, start, end, scope, effects, etype, description, service=None, expected=None) -> Scenario:
    event = {"type": etype, "service": service, "description": description, "planned": True,
             "expected_effect": expected if expected is not None else effects}
    return Scenario(id=sid, start=start, end=end, scope={k: str(v) for k, v in (scope or {}).items()},
                    effects=dict(effects or {}), event=event)


def load_calendar(path: str | Path | None = DEFAULT_CALENDAR, first: date = P.YEAR_START.date(),
                  last: date = P.CALENDAR_END.date()) -> Calendar:
    """Expand calendar.yaml into scenarios; recurring rules cover the months between `first` and `last`."""
    if path is None:
        return Calendar([])
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    out: list[Scenario] = []
    kinds: dict[str, str] = {}

    def add(s: Scenario, kind: str):
        if s.id in kinds:
            raise ValueError(f"duplicate calendar id {s.id!r}")
        out.append(s)
        kinds[s.id] = kind

    def span(r) -> tuple[datetime, datetime]:
        if "day" in r:
            d = _day(r["day"])
            return utc(d), utc(d + timedelta(days=1))
        return utc(_day(r["from"])), utc(_day(r["to"]) + timedelta(days=1))

    for r in raw.get("retail") or []:
        add(_planned(r["id"], *span(r), r.get("scope"), r["effects"], "campaign", r.get("description", "")), "retail")
    for r in raw.get("seasonality") or []:
        for m in _month_starts(first, last):
            if m.month == r["month"]:
                nxt = next(_month_starts(m + timedelta(days=31), m + timedelta(days=31)))
                add(_planned(f"{r['id']}_{m:%Y-%m}", utc(m), utc(nxt), r.get("scope"), r["effects"], "campaign",
                             r.get("description", "")), "seasonality")
    hol = raw.get("holiday_effects") or {"sessions": 0.8}
    for r in raw.get("holidays") or []:
        d = _day(r["day"])
        add(_planned(f"holiday_{r['country']}_{d}", utc(d), utc(d + timedelta(days=1)), {"country": r["country"]},
                     hol, "holiday", r.get("description", "")), "holiday")
    pay = raw.get("payday")
    if pay:
        for m in _month_starts(first, last):
            if m == date(first.year, first.month, 1) and m + timedelta(days=pay["first_days"]) <= first:
                continue
            add(_planned(f"payday_{m:%Y-%m}", utc(m - timedelta(days=pay["last_days"])),
                         utc(m + timedelta(days=pay["first_days"])), pay.get("scope"), pay["effects"], "campaign",
                         f"Payday (last {pay['last_days']} and first {pay['first_days']} days of the month)"),
                "payday")
    for r in raw.get("structural") or []:
        start = utc(r["start"])
        effects, before = dict(r.get("effects") or {}), dict(r.get("before") or {})
        expected = {**effects, **{k: round(1 / float(v), 4) for k, v in before.items()}}
        add(_planned(r["id"], start, utc(r["end"]) if r.get("end") else None, r.get("scope"), effects,
                     r.get("type", "config"), r.get("description", ""), r.get("service"), expected), "structural")
        if before:   # the parameter's value before the change, relative to its current value in params
            add(Scenario(id=f"{r['id']}_before", start=utc(first), end=start,
                         scope={k: str(v) for k, v in (r.get("scope") or {}).items()}, effects=before),
                "baseline")
    for r in raw.get("incidents") or []:
        add(Scenario(id=r["id"], start=utc(r["start"]), end=utc(r["end"]),
                     scope={k: str(v) for k, v in (r.get("scope") or {}).items()}, effects=dict(r["effects"]),
                     event={"type": "release", "service": r.get("service"), "description": r.get("release", "")},
                     end_event={"type": "release", "service": r.get("service"),
                                "description": r.get("resolution", "")}),
            "incident")
    g = raw.get("growth") or {}
    return Calendar(out, float(g.get("per_year", 0.0)), _day(g.get("anchor", P.HISTORY_START.date())), kinds)


def _row(s: Scenario, kind: str) -> dict:
    ev = s.event or {}
    effects = s.effects or ev.get("expected_effect") or {}
    return {"id": s.id, "kind": kind, "type": ev.get("type", ""), "service": ev.get("service") or "",
            "planned": bool(ev.get("planned", False)), "start": s.start.isoformat(sep=" "),
            "end": s.end.isoformat(sep=" ") if s.end else "", "scope": seg_key(s.scope),
            "effects": json.dumps(effects, sort_keys=True), "description": ev.get("description", "")}


def ground_truth(calendar: Optional[Calendar] = None, scenarios: str | Path | None = SCENARIOS_PATH,
                 exclude: Iterable[str] = ()) -> list[dict]:
    """Every calendar event, past incident and demo scenario (minus `exclude`), with UTC start/end and effects."""
    cal = calendar if calendar is not None else load_calendar()
    rows = [_row(s, cal.kinds.get(s.id, "calendar")) for s in cal.scenarios
            if cal.kinds.get(s.id) != "baseline"]
    if cal.growth_per_year:
        rows.append({"id": "growth_trend", "kind": "trend", "type": "", "service": "", "planned": True,
                     "start": str(cal.growth_anchor), "end": "", "scope": "",
                     "effects": json.dumps({"sessions_per_year": 1 + cal.growth_per_year}),
                     "description": f"Sessions trend x{1 + cal.growth_per_year:g} per year"})
    if scenarios is not None:
        rows += [_row(s, "demo") for s in load_scenarios(scenarios)]
    exclude = set(exclude)
    return sorted((r for r in rows if r["id"] not in exclude), key=lambda r: (r["start"], r["id"]))


def write_ground_truth(out_dir: str | Path, calendar: Optional[Calendar] = None,
                       scenarios: str | Path | None = SCENARIOS_PATH,
                       exclude: Iterable[str] = ()) -> tuple[Path, Path]:
    """Write ground_truth.csv and ground_truth.yaml into `out_dir`."""
    rows = ground_truth(calendar, scenarios, exclude)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path, yaml_path = out / "ground_truth.csv", out / "ground_truth.yaml"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=GROUND_TRUTH_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    with open(yaml_path, "w") as f:
        f.write("# Generated by shoply.sim.calendar.write_ground_truth; times are UTC hour starts.\n")
        yaml.safe_dump([{**r, "effects": json.loads(r["effects"])} for r in rows], f, sort_keys=False,
                       allow_unicode=True, width=120)
    return csv_path, yaml_path


__all__ = ["Calendar", "DEFAULT_CALENDAR", "load_calendar", "ground_truth", "write_ground_truth", "utc"]
