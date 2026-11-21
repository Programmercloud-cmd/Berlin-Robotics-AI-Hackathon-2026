# Metric catalog

Status: 36-metric scope agreed in conversation. Detailed operational semantics below are proposed implementation defaults. This supersedes the initial brainstorming catalog.

## Shared calculation contract

- Store timestamps in UTC; use Europe/Berlin only for business-day budgets and seasonal calendar features. Use half-open intervals [start, end).
- Generate at one-minute steps; default analysis buckets are five minutes. Permit coarser buckets by recomputing aggregates from facts.
- Each fact has a unique ID, event time, and ingestion time. Deduplicate on ID. Queries see only records ingested by their observation snapshot.
- Counts and currency are additive only across disjoint populations. Store money as integer euro cents; one currency, EUR. Exclude taxes, refunds, and FX in v1.
- Rates are ratios of summed numerator/denominator values; never average bucket or segment rates. Zero denominator produces null with reason `zero_denominator`, not zero.
- Event counts use occurrence time unless a metric explicitly uses a session cohort. Rates must compare compatible populations and maturity.
- Session metrics use session start cohorts, attributed using immutable session country/platform/channel. A session lasts at most 15 minutes. Session cohorts become mature 15 minutes after bucket end, subject to source completeness. Product views, carts, checkouts, and orders retain session ID. Orders are confirmed only after a successful payment.
- In v1 there is at most one order per session, possibly several payment attempts. Keep confirmed-order counts distinct from converted-session counts.
- Finalised payment attempts use finalisation time; outcomes partition into success, timeout, and other failure. Pending attempts are excluded and flagged as immature where relevant. Rates describe attempts, not customers.
- Inventory snapshots use the latest snapshot at or before the requested timestamp; summing snapshots over time is invalid.
- Every result carries units, population/cohort, dimensions, definition version, observation snapshot, numerator/denominator where applicable, and quality/maturity information.
- A complete empty population produces zero for counts, but missing/incomplete data is not silently converted to zero.

## Catalog

Dimension groups are defined below. Physical source/query names are proposed handles; implementation must create executable queries for them.

| ID | Definition / calculation | Unit | Dimensions | Source handle |
| --- | --- | --- | --- | --- |
| `sessions` | Distinct session IDs by session start | count | journey | sessions |
| `product_view_sessions` | Session-cohort count with >=1 product view | count | journey | session_funnel |
| `add_to_cart_sessions` | Session-cohort count with >=1 cart addition | count | journey | session_funnel |
| `checkout_sessions` | Session-cohort count with checkout started | count | journey | session_funnel |
| `orders` | Confirmed orders by confirmation time | count | journey | orders |
| `session_conversion_rate` | Converted sessions / sessions in same mature session cohort | ratio | journey | session_funnel |
| `checkout_completion_rate` | Converted checkout sessions / checkout sessions in same mature session cohort | ratio | journey | session_funnel |
| `searches` | Search requests, by request time | count | search | search_requests |
| `searches_with_click` | Requests with >=1 result click within 2 minutes; bucket by request time | count | search | search_requests |
| `zero_result_searches` | Requests returning zero results | count | search | search_requests |
| `search_click_rate` | Searches with click / searches; mature after 2 minutes | ratio | search | search_requests |
| `feed_impressions` | Product-card impressions in personalised feed | count | feed | feed_events |
| `feed_clicks` | Click events referencing a prior feed impression; by click time | count | feed | feed_events |
| `unavailable_feed_impressions` | Impressions with zero sellable stock at impression time | count | feed | feed_events |
| `unavailable_feed_share` | Unavailable feed impressions / feed impressions | ratio | feed | feed_events |
| `payment_attempts` | Finalised attempts, including retries | count | payment | payments |
| `successful_payments` | Finalised attempts with outcome success | count | payment | payments |
| `payment_timeouts` | Finalised attempts with outcome timeout | count | payment | payments |
| `other_failed_payments` | Finalised attempts with outcome other_failure | count | payment | payments |
| `payment_success_rate` | Successful payments / finalised attempts | ratio | payment | payments |
| `payment_timeout_rate` | Timeouts / finalised attempts | ratio | payment | payments |
| `available_units` | Sum of sellable stock at requested snapshot | units | inventory | inventory_snapshots |
| `units_sold` | Sum of order-line quantities on confirmed orders | units | inventory | order_lines |
| `stockout_sku_share` | Active SKUs with zero sellable stock / active SKUs, at snapshot | ratio | inventory | inventory_snapshots |
| `gross_merchandise_value` | Merchandise list-price value on confirmed orders, before platform-funded discounts | EUR cents | economics | order_economics |
| `platform_fee_revenue` | Per-line marketplace commission on pre-discount merchandise value | EUR cents | economics | order_economics |
| `platform_funded_discounts` | Marketplace-funded discount cost on confirmed order lines | EUR cents | economics | order_economics |
| `advertising_spend` | Cost incurred on acquisition ad clicks, by click time | EUR cents | advertising | ad_events |
| `variable_operating_cost` | Processing and servicing costs attributed to confirmed order lines | EUR cents | economics | order_economics |
| `marketplace_contribution` | Fee revenue - discounts - ad spend - variable cost, aggregated on compatible dimensions | EUR cents | contribution | contribution |
| `ad_impressions` | Acquisition campaign impressions | count | advertising | ad_events |
| `ad_clicks` | Acquisition campaign clicks | count | advertising | ad_events |
| `effective_cost_per_click` | Advertising spend / ad clicks | EUR cents per click | advertising | ad_events |
| `daily_budget_utilization` | Campaign spend since Berlin midnight / configured campaign daily budget; not additive across time | ratio | advertising | campaign_pacing |
| `event_ingestion_lag_seconds` | p95 of ingestion time - event time for received records, bucketed by event time | seconds | data | ingestion_receipts |
| `data_completeness_ratio` | Received unique records / source-manifest count for a closed event-time interval | ratio | data | source_manifests |

### Dimension groups

| Group | Allowed dimensions |
| --- | --- |
| journey | country, platform, acquisition_channel |
| search | country, platform, query_language |
| feed | country, platform, category, recommendation_model_version |
| payment | country, platform, payment_provider, checkout_release_version |
| inventory | sku_id, category, seller_id |
| economics | country, platform, category, acquisition_channel |
| advertising | campaign_id, target_country, advertising_channel |
| contribution | country, acquisition_channel only, plus total |
| data | source_id, event_type |

Map advertising `target_country` to business `country`, and `advertising_channel` to `acquisition_channel` explicitly in the catalog's source metadata. Do not invent spend attribution to platform, SKU, seller, or category. Contribution is calendar-period contribution, not campaign lifetime ROI. All cost components use their actual occurrence/booking times, so avoid claiming a same-session accounting identity.

Constrain time and dimension aggregation for special metrics: recompute p95 from underlying delays; recompute stockout share from snapshot SKU counts; recompute budget utilization from spend and budgets. With group_by empty, budget utilization is sum campaign spend / sum distinct applicable campaign budgets. Reject multi-day utilization requests in v1.

## Proposed registration split

Initial registry: 33 metrics. Introduce these three independently later:
- `payment_timeout_rate`
- `unavailable_feed_share`
- `marketplace_contribution`

Each addition includes its definition and source query/calculation, dimensions, units, population, and temporal semantics. It includes no parent, graph edges, scenario label, or alert threshold. Referencing components in an arithmetic calculation is legitimate input; the agent still has to validate compatibility and construct edges.

Underlying counts already expose much of the same information. Do not claim a derived ratio adds a new independent signal. Test genuinely novel data integration separately if time permits.

## Validation invariants

- Mature cohort funnel: converted <= checkout <= add-to-cart <= product-view <= sessions.
- Exactly one success per confirmed order in v1; compare on linked IDs, not mismatched time buckets.
- Finalised attempts = success + timeout + other failure.
- Searches with click <= searches; zero-result searches <= searches; zero-result searches cannot have result clicks.
- Unavailable feed impressions <= feed impressions. Clicks may cross bucket boundaries; do not enforce clicks <= impressions per time bucket.
- All rates are in [0,1] except budget utilization, which can exceed 1 during a pacing failure.
- Stock never goes negative; stockout share uses distinct active SKUs, not inventory-unit weights.
- Contribution arithmetic reconciles exactly on supported dimensions and windows.
- Source-manifest received counts cannot exceed declared totals after deduplication. Missing manifests produce unknown completeness.

These are calculation checks, not a manually supplied causal graph.
