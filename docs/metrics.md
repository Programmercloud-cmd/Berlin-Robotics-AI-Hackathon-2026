# Metric catalog

Status: draft captured from the brainstorming conversation. This records the metrics for the marketplace detective concept; it does not finalize their implementation.

## Initial metrics

| Metric ID | Metric | Working definition | Purpose |
| --- | --- | --- | --- |
| `traffic` | Traffic | Incoming marketplace activity. Whether this counts visitors, sessions, or visits is still to be decided. | Distinguish a demand change from a downstream funnel problem. |
| `orders` | Orders | Count of completed orders; the exact completion event is still to be defined. | Track the primary business outcome and locate order loss. |
| `conversion_rate` | Conversion rate | Share of eligible traffic or checkout activity that becomes an order. The funnel stage and denominator are still to be decided. | Investigate whether orders fell because fewer customers converted. |
| `revenue` | Revenue | Revenue attributable to the counted orders. Treatment of discounts, refunds, tax, and currency is still to be decided. | Measure the financial impact of an order change. |
| `average_order_value` | Average order value | `revenue / orders`, using the same order population and time window. | Separate changes in order volume from changes in order value. |
| `payment_failure_rate` | Payment failure rate | Proposed operational definition: failed payment attempts divided by all payment attempts. Failure categories and retry handling are still to be defined. | Test payment problems as an explanation for conversion loss. |
| `inventory_availability` | Inventory availability | Availability of inventory to customers. The unit of measurement and aggregation are still to be decided. | Test an inventory shortage as a competing explanation for order loss. |

“Inventory” and “payments” were investigation areas in the conversation. The corresponding metric IDs and working definitions above are provisional ways to make those areas measurable.

## Metric introduced during the demo

| Metric ID | Metric | Working definition | Purpose |
| --- | --- | --- | --- |
| `payment_timeout_rate` | Payment timeout rate | Proposed operational definition: payment attempts ending in timeout divided by all payment attempts. Timeout classification and retry handling are still to be defined. | Let the agent discover a new metric, validate its relevance, and use it to strengthen an ongoing payment investigation. |

The proposed demo introduces `payment_timeout_rate` through its definition and calculation/query, without supplying its placement in the metric graph. Its relationship to payment failures depends on whether timeouts are included in the agreed failure taxonomy.

## Relationships and breakdowns discussed

- Arithmetic relationship: `revenue = orders × average_order_value`, for consistent populations and windows where average order value is defined.
- Traffic and conversion help explain changes in orders. An exact arithmetic relationship needs agreed funnel and counting semantics.
- Payment failures, payment timeouts, and inventory shortages are candidate explanations for conversion loss. These are hypotheses to investigate, not established causal relationships.
- Country and platform are proposed breakdowns. The example incident concerns German Android customers; the proposed small demo uses two countries and two platforms, with the remaining values undecided.
- A campaign and a release are contextual events, not business metrics.

## Definitions still to settle

Before implementing the calculations, agree on source events/queries, time buckets and comparison windows, eligible populations, dimensions, units, retry/deduplication rules, and missing-data and zero-denominator behavior. No alert thresholds or baseline values have been established.

The separate Profit Paradox idea mentioned contribution, discounts, advertising costs, and product mix. Those have not been included in this initial catalog because that alternative was not selected in the supplied conversation.

Company simulation, MCP/interface, and system harness design remain undecided and are outside this document.
