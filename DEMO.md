# Eye of the System · Judges’ demo

Eye discovers how a marketplace works from observed events, learns what normal looks like, and explains where the shopping journey breaks. The Test Lab publishes the simulated events. Eye sees the telemetry, while the fault settings and expected diagnoses stay in the lab.

## What to show the judges

| Capability | What to put on screen |
| --- | --- |
| **Workflow discovery from event logs** | Eye’s **Journey** view. PM4Py discovers the workflow from object-linked events. Select an incident to highlight the affected step and supporting metrics on the graph. |
| **Seasonality and planned events** | Eye’s **Planned events** panel. Show the ordinary expectation of **284 orders** and the holiday-adjusted expectation of **425**, with a range of **372–478** based on one prior occurrence. |
| **Metric alert cadence** | In the catalog, choose a check cadence when registering a metric or leave **Infer cadence** selected so Devin recommends one from the metric’s definition. Open **Metric Insights** to inspect and edit the saved cadence. The edit applies to future checks, and earlier alerts retain their original detection evidence. |
| **Independent detection during a traffic surge** | The healthy replay’s first hour had **399 orders** and no incident. During the regression, the same hour still had **311 orders**, above the ordinary baseline. Eye detected the German mobile checkout break in **25 virtual minutes** after checkout starts fell to **43.2% versus 79.4% expected**. Open the evidence for the **94 recorded checkout failures**. |
| **Grouping alerts without hiding a second failure** | In the search replay, fewer Stripe attempts explained **84.5 of 87.7 missing successes**. Eye grouped the two volume alerts and showed search as a partial contributor. In a held-out replay, Stripe success fell to **26.0% versus 96.1% expected**, with **132 recorded gateway timeouts**. Eye kept that payment incident separate. |
| **Evidence, uncertainty, and recovery** | Expand an investigation to show what supports each explanation, what weakens it, and what remains unresolved. Show **Related alerts** and **View evidence**. The alert is visible while Devin investigates, and monitoring continues through recovery. |
| **A Test Lab with plain-language scenarios** | Ask Devin to simulate payment timeouts, review its interpretation of the affected step, scope, severity, and duration, then apply or replay the plan. The lab timeline links to Eye’s independently detected alert and records recovery when available. |

## Three-minute video

| Time | Shot | Narration cue |
| --- | --- | --- |
| **0:00–0:15** | Eye’s Journey graph, then the holiday result showing 311 orders | “Sales are above normal for this hour. So why is Eye raising a checkout incident?” |
| **0:15–0:40** | Test Lab scenario preview, then Eye’s Journey view | “The lab simulates the marketplace. Eye observes the resulting events and discovers the journey from linked sessions, carts, orders, and payments.” |
| **0:40–1:15** | **Planned events** and the healthy holiday replay | “The last holiday shopping period gave Eye a provisional forecast of 425 orders for the first hour. It observed 399, inside the expected range, with healthy checkout.” |
| **1:15–1:55** | Regression replay, then the checkout investigation and its evidence | “Orders were still above the ordinary forecast. German mobile checkout fell from 79% expected to 43%. Eye found the break in 25 virtual minutes and surfaced the failed checkout events.” |
| **1:55–2:30** | Stripe volume group and **Related alerts**, then the separate gateway incident | “Fewer attempts explained the missing successes, so Eye grouped those alerts. In the next replay, success per attempt collapsed and the events showed timeouts. Eye kept that failure separate.” |
| **2:30–3:00** | Investigation evidence and recovery, **Check cadence** in Metric Insights, then the Benchmark scoreboard | “Eye shows the evidence, tracks recovery, and lets you edit how often each metric is checked. Devin can suggest the cadence at registration. The Test Lab can replay cases against a recorded commit.” |

## Extra shots if there’s room

- **Late data versus a real fault.** Show a timely linked order whose payment receipt arrives later, then compare it with an explicitly recorded gateway timeout.
- **A metric added after detection.** Show the 36-definition catalog and the payment replay in which `payment_timeout_rate` is registered after the initial alert. Devin proposes where it belongs in the graph.
- **Metric Insights.** Open a metric, choose a traffic segment, and show the observed readings and baseline in the same chart.

## Recording setup

- [Eye dashboard](http://127.0.0.1:8000) for Journey, Planned events, and investigations. [Test Lab](http://127.0.0.1:8002) for scenario preview and replay. [Benchmark](http://127.0.0.1:8002/benchmark) for the scoreboard. [Startup instructions](README.md).
- Run `python -m eye check-holiday` to reproduce the two holiday comparisons. The terminal check saves the evidence to `.runtime/seasonality/holiday-replay.json`.
- Cue the healthy replay, checkout incident, and Stripe comparison before recording. Use the recorded evidence for the exact counts and label replay times as virtual time in captions.
