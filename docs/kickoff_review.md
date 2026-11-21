# Kickoff spec review (Brain / person B)

`sentinel/contracts.py` is committed **verbatim**. The items below are proposals for A + B + C to agree
on. Items 1–8 touch shared contracts or files; 9–15 are internal to one component. Every claim was
checked against the spec numbers, and most of them are demonstrated by tests in
`tests/test_brain_graph.py` running on a dev stand-in for Data (`dev/fake_data.py`).

## Proposals that change a shared contract

| # | Problem | Proposal | Owner |
|---|---|---|---|
| 1 | `MetricNode` has one `table`, but `conversion_rate`, `view_rate` and `search_ctr` divide a fact_commerce numerator by a fact_traffic denominator. `contribution` has no table at all. | Allow a table-qualified column, e.g. `denominator_sql: "SUM(fact_traffic.sessions)"`, evaluated on that table and joined on ts + dims. A node without SQL is computed from its formula. No field changes. (Implemented this way in the stand-in.) | A, B |
| 2 | Seed `gmv` is a leaf, so **orders is not connected to contribution** until AOV is added live — but the main payment fault (10-26 15:00) happens first. The investigator cannot walk contribution → orders, and `impact_eur_per_h` for orders has no € path. Test: `test_driver_tree_is_split_review_item_2`. | Put `avg_order_value` and `gmv = orders × avg_order_value` in the seed. For the "extender discovers a formula" demo, hold back `payment_cost_rate = payment_costs / gmv` instead; the extender should discover `payment_costs = gmv × payment_cost_rate`. | all |
| 3 | DST ends **Sun 2026-10-25**, the history/live boundary. The 20:00 local peak is 18:00 UTC in history and 19:00 UTC live, so UTC-keyed baselines flag every ramp hour. | Brain keys seasonality by Europe/Berlin local time (done: `local_profile_key`). Data must generate the peak in local time, as the spec already says. Nothing changes in the contract; this is a shared caution. | A, B |
| 4 | `for_you` is a session **channel**, but feed metrics have no `channel` dimension. So `for_you_model_shift` (scope `channel: for_you`) cannot be localised, and `product_views` reconciles only on country/platform. | Either add `channel` to the feed metrics (fact_commerce already has the column), or define for_you as a surface and scope the shift to all feed traffic. | all |
| 5 | `effects` mixes multipliers (`payment_success_rate: 0.755`, `feed_ctr: 1.2`) with absolute values (`oos_exposure_share: 0.35`, `completeness: 0.6`, `delivery_lag_days: 6`). Read as a multiplier, 0.35 would *lower* OOS exposure. | Use explicit `effects: {mul: {...}, set: {...}}`. | A |
| 6 | `Incident.fingerprint = first_cause\|segment\|hypothesis_id` changes whenever the leading hypothesis changes, which duplicates incidents. That is exactly what the brief penalises. | Fingerprint = `first_cause\|segment` (+ onset window). Hypotheses stay in the incident body. | B, C |
| 7 | The store has no graph tables, but the judges must see the graph, edge statuses and the reasons for `needs_review`. `Edge` has no reason field. ExtensionReports also have no table. | Add store tables `graph_nodes`, `graph_edges` (Edge + `reason` + `stats_json`) and `extension_reports`. Brain writes them on bootstrap and on every change. | B, C |
| 8 | `MetricSpec.sql` has no result convention. `for_you_oos_exposure_share` needs `feed_oos_impressions`, which is not a metric. Data also has no way to learn a node that was added live. | The contributor gives aggregate expressions (`numerator_sql`/`denominator_sql` or `value_sql`) and a `table`, or an `expr` over metric ids. Brain builds the MetricNode, and Data gets `register(node)`. | A, B |

## Internal to one component (no agreement needed, listed for awareness)

9. **`Edge.op` per edge** cannot describe a sum of products (`product_views`). Brain derives each input's role from the AST and keeps `op` as a summary.
10. **Seed formulas are re-validated at startup** against data. If one fails, it becomes `needs_review` and is excluded from decomposition. All 7 currently pass in every slice, except `contribution`, which is computed from its own formula and so cannot be tested independently.
11. **Ratio definitions are not formula edges**, only decompositions are, so `gmv = orders × aov` plus `aov = gmv / orders` cannot form a cycle. Formula cycles are rejected (`test_formula_cycle_rejected`).
12. **Gate 1:** `contribution` and `product_views` depend on non-bold metrics. At Gate 1, treat `product_views` as a plain count and add its formula in Step 4.
13. **`pacing_ratio`** ("cumulative today") breaks Σnumerator/Σdenominator over multi-hour windows. Needs a special case in metric(), or an hourly definition (used in the stand-in for now).
14. **Late partitions are back-filled**, so Brain re-reads the last few hours every tick and retracts conclusions drawn on `complete = False` data.
15. **2 s ticks vs LLM latency.** The loop is synchronous, so the demo only slows down. Budget LLM calls, or pause during investigations.

Causal seeds checked on the stand-in data:
- 5 supported: in_stock → add_to_cart, in_stock → feed_purchase, delivered → in_stock (on changes), po → delivered (lag 48 h), acquisition_cost → sessions.
- `platform_discounts → conversion_rate` is **confounded**: discounts scale with orders, and the partial correlation drops from 0.88 to 0.02. Flagged as `needs_review`.
- `pacing_ratio → ad_revenue` has a definitional overlap (shared `spend_eur`).
- `search_relevance → search_ctr` is weak.

Minor: the spec says "seven areas" but lists six. `view_rate` is ≈1.8, not a share, so its unit should be `score`. 10-03 falls on a Saturday, so the holiday effect is barely visible.

---

## Message to A and C (draft)

> Hi both — I've committed `sentinel/contracts.py` exactly as in the kickoff spec, plus the seed graph (29 nodes, 7 formulas, 8 causal links). The graph validates against a stand-in for Data that follows the spec baseline: 600k sessions, 3.3% conversion, ~19.5k orders/day. All seed formulas hold in every slice.
>
> Eight things touch shared contracts, so I'd like us to agree before freezing (details in `docs/kickoff_review.md`):
> 1. Cross-table ratios (conversion_rate, view_rate, search_ctr) don't fit a single `table`. Proposal: allow `SUM(fact_traffic.sessions)` style qualified columns, with no field change.
> 2. Without AOV in the seed, orders isn't connected to contribution, so the payment fault can't be traced to €. Proposal: seed AOV, and hold back `payment_cost_rate` for the discover-a-formula demo.
> 3. DST ends on 10-25, our history/live boundary. Generate and key seasonality in Europe/Berlin local time.
> 4. for_you is a channel, but the feed metrics have no channel dimension, so the for_you fault can't be localised. Add `channel` to the feed metrics?
> 5. Scenario `effects` mix multipliers and absolute values. Use `{mul: …, set: …}`?
> 6. The incident fingerprint includes hypothesis_id, so it changes mid-investigation and duplicates incidents. Use `first_cause|segment`.
> 7. The store needs `graph_nodes`, `graph_edges` (with a reason) and `extension_reports` so the UI can show the graph.
> 8. MetricSpec.sql needs a result convention, plus a way for Data to learn live-added nodes.
>
> Everything else I can handle inside Brain.
