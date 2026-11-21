"""2. Event Bus — the nervous system.

Every component publishes what it does; nothing talks to the UI or the notifier directly.
Events are kept in memory (for the live UI to replay), appended to a JSONL file (audit trail /
offline replay) and fanned out to subscribers.

Event types
  SYSTEM, GRAPH_LOADED, GRAPH_MUTATION                       graph / lifecycle
  MONITOR_TICK, WATCH, DATA_ISSUE, INNOCENT_CHANGE,
  ANOMALY_DETECTED, SERIES_RECOVERED                         monitor engine
  INCIDENT_OPENED, INCIDENT_UPDATED, INCIDENT_RESOLVED       incident lifecycle
  THOUGHT, ACTION, OBSERVATION, HYPOTHESIS, DEAD_END          investigator (ReAct chain of thought)
  META_PROPOSAL, META_RESULT                                 meta-agent
  FINAL_REPORT                                               reports
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


@dataclass
class BusEvent:
    seq: int
    type: str
    source: str
    sim_ts: str | None
    wall_ts: str
    incident_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str, ensure_ascii=False)


class EventBus:
    def __init__(self, log_path: str | Path | None = "state/events.jsonl"):
        self.history: list[BusEvent] = []
        self._subs: list[tuple[set[str] | None, Callable[[BusEvent], None]]] = []
        self._lock = threading.Lock()
        self.sim_ts: str | None = None
        self.log_path = Path(log_path) if log_path else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text("", encoding="utf-8")

    def subscribe(self, fn: Callable[[BusEvent], None], types: Iterable[str] | None = None):
        self._subs.append((set(types) if types else None, fn))

    def publish(self, type: str, source: str, payload: dict | None = None, incident_id: str | None = None) -> BusEvent:
        with self._lock:
            ev = BusEvent(seq=len(self.history) + 1, type=type, source=source, sim_ts=self.sim_ts,
                          wall_ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          incident_id=incident_id, payload=_clean(payload or {}))
            self.history.append(ev)
            if self.log_path:
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(ev.to_json() + "\n")
        for types, fn in list(self._subs):
            if types is None or type in types:
                fn(ev)
        return ev

    def since(self, seq: int) -> list[BusEvent]:
        return self.history[seq:]

    def of_type(self, *types: str) -> list[BusEvent]:
        return [e for e in self.history if e.type in types]


def _clean(x):
    """Make payloads JSON-safe (numpy scalars, timestamps, sets)."""
    import numpy as np
    import pandas as pd
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.floating, float)):
        v = float(x)
        return None if v != v else round(v, 6)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, (pd.Timestamp, datetime)):
        return str(x)
    return x
