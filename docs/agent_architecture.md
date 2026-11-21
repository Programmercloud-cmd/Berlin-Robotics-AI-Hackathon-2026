# Agent architecture

The design the agent implements. Each box maps to a package under `sentinel/`.

```
[ Simulator data stream ] (injected faults / normal fluctuation)
        │
        ▼
┌────────────────────────────────────────────────────────┐
│ 1. Monitor Engine — the system's "eyes"                 │
│  · dynamic baselines (Z-score / Prophet)                │
│  · anomaly detection -> filter innocent changes ->      │
│    emit alert events                                    │
└────────────────────────────────────────────────────────┘
        │ (Event: ANOMALY_DETECTED)
        ▼
┌────────────────────────────────────────────────────────┐
│ 2. Event Bus — the system's "nervous system"            │
│  · broadcasts every state: monitor alerts, agent        │
│    thoughts, graph changes, final reports               │
└────────────────────────────────────────────────────────┘
        │                                       │
        │ (subscribe)                           │ (subscribe)
        ▼                                       ▼
┌──────────────────────────┐         ┌──────────────────────────┐
│ 5a. Silent mode          │         │ 5b. Live mode (UI)       │
│  · only FINAL_REPORT     │         │  · WebSocket/SSE push    │
│  · send e-mail / Slack   │         │  · render chain of       │
│                          │         │    thought & live graph  │
└──────────────────────────┘         └──────────────────────────┘
        ▲
        │ (read / write)
┌────────────────────────────────────────────────────────┐
│ 3. Investigator (ReAct brain) — the system's "brain"    │
│  · receives alerts, starts graph traversal & drill-down │
│  · calls statistical tools to test hypotheses           │
│    (t-test / Granger)                                   │
│  · writes the final root-cause report                   │
└────────────────────────────────────────────────────────┘
        │ (triggered on a dead end)
        ▼
┌────────────────────────────────────────────────────────┐
│ 4. Meta-Agent (auto-extension) — the system's           │
│    "creativity"                                         │
│  · reads the data dictionary, generates new derived     │
│    metrics / cross dimensions                           │
│  · sandbox validation -> on success, writes into the    │
│    underlying graph (graph mutation)                    │
└────────────────────────────────────────────────────────┘
```

| Box | Implementation |
|---|---|
| 1. Monitor Engine | `sentinel/monitor/` (robust seasonal Z-score; Prophet is not used) |
| 2. Event Bus | `sentinel/bus.py` |
| 3. Investigator | `sentinel/investigator/` (Welch t-test, Granger via statsmodels) |
| 4. Meta-Agent | `sentinel/meta/` |
| 5a. Silent mode | `sentinel/outputs/silent.py` |
| 5b. Live mode | `sentinel/outputs/live.py`, `live.html` (Server-Sent Events) |
