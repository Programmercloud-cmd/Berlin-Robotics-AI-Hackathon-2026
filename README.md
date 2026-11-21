# Sentinel — an agentic monitoring system for a marketplace

The agent watches a simulated marketplace (Shoply) hour by hour with no human prompting. It:

- decides what to look at;
- separates real problems from planned changes and from incomplete data;
- investigates causes through a metric graph;
- extends that graph when it gets stuck;
- reports each problem once, with evidence, and follows it until it recovers.

```
pip install -r requirements.txt
python -m sentinel.run                       # silent mode: reports in outbox/ (Slack / e-mail if configured)
python -m sentinel.run --mode live           # live mode: open http://127.0.0.1:8765 and watch it think
python -m pytest -q                          # 35 tests, including an end-to-end run
```

## Architecture (docs/agent_architecture.md)

```
[ simulator: dev/fake_data.py ]  normal behaviour + injected faults, late data, planned events
        │  metric() · completeness() · events() · schema()
        ▼
1. Monitor Engine         sentinel/monitor/   — the eyes
   seasonal robust Z-score baselines (local hour × day type, DST-safe), spike + CUSUM detectors,
   data-quality hold, planned-event filter (INNOCENT_CHANGE), euro impact through the formula graph
        │ ANOMALY_DETECTED
        ▼
2. Event Bus              sentinel/bus.py     — the nervous system
   every alert, thought, action, observation, hypothesis, graph mutation and report is an event
   (kept in memory for the UI, appended to state/events.jsonl)
        │                                              │
        ▼                                              ▼
5a. Silent mode   sentinel/outputs/silent.py   5b. Live mode   sentinel/outputs/live.py + live.html
    FINAL_REPORT -> outbox/*.md (+Slack/e-mail)     FastAPI + SSE: chain of thought, dynamic graph,
                                                    incidents, reports, actual-vs-expected charts
        ▲
3. Investigator (ReAct)   sentinel/investigator/  — the brain
   Thought → Action (tool) → Observation, each step stored as numbered evidence:
   data check → decompose along formula edges (first cause vs downstream symptoms) → drill down
   (lift) → generalise → negative control (Welch t-test) → events near the onset → causal parents
   (+ Granger) → predictions update posteriors → conclude: supported cause / hypothesis /
   explained / data issue / unexplained, with impact in EUR.
   Incidents: one per problem (merge by shared root cause), alert → monitoring → resolved, reopen.
        │ DEAD_END
        ▼
4. Meta-Agent             sentinel/meta/      — creativity
   reads the data dictionary; adds missing dimensions (cross dimensions) and derived metrics from
   unused columns; sandbox-validates them; writes only what passes into the graph (GRAPH_MUTATION).
   Also registers contributor metrics with no placement given, and discovers their formulas.

Knowledge: sentinel/brain/   metric graph (formula / breakdown / causal / context edges), validated on data
```

## What one run shows (simulated 2026-10-26 → 10-27)

| time | what happens in the world | what the agent does on its own |
|---|---|---|
| 10-26 08:00 | planned campaign: paid traffic x1.4, lower intent | the volume rise is an innocent change; the add-to-cart drop in `channel=paid` is investigated and explained by the campaign, with no alert |
| 13:00 | `fact_commerce` partition only 60% loaded | held as a data issue, re-evaluated when complete, with no false alarm |
| 15:00 | payments-android 5.12 breaks DE Android payments; iOS 7.3 released at the same time | alert at 16:00 (the first possible hour). First cause: payment success rate in `country=de\|platform=android`. The Android release is the supported cause (94%); iOS is rejected because its scope conflicts. Impact is given in EUR. |
| 21:00 | rollback | monitoring, then resolved after 3 normal hours; the resolution report notes the rollback |
| 22:00 | — | Meta-Agent adds `channel` to the feed metrics (it needed it to verify the campaign scope) |
| 23:00 | contributor submits `payment_cost_rate` (no placement given) | dimensions inferred; formula `payment_costs = gmv × payment_cost_rate` discovered and validated |
| 10-27 10:00 | recommendation model v42 surfaces out-of-stock items in For You | one incident (its symptoms are merged). Localised to `channel=for_you` thanks to the new dimension; model v42 is the supported cause. Meta-Agent creates `feed_oos_share` from an unused column (+6900%) with a causal edge to add-to-cart. |

The data layer here is a simulated marketplace (`dev/fake_data.py`) with realistic tables, baseline and
scenarios. The agent only uses `metric()`, `completeness()`, `events()` and `schema()`, so a real
warehouse can replace the stand-in without changing the agent.
