# Company simulation

Status: proposed defaults. The simulator supports the agreed catalog; it is independent of the monitoring agent.

## Company and clock

A fictional EUR marketplace connects customers to sellers. The platform earns per-product commissions, funds some discounts, and pays for acquisition and variable operations.

Default small world:
- Countries DE and FR; platforms Android and Web; channels organic, paid_search, paid_social.
- 24 SKUs in three categories, six sellers, two payment providers, four campaigns.
- Country/platform/channel remain fixed for a session. A campaign has one target country and one paid channel.
- One campaign per country x paid channel combination; active 08:00-22:00 Berlin time, 100,000-cent daily budget, planned cumulative allowance linear across active hours. Default auction opportunity rate is 60 impressions/minute per active campaign; normal controller throttles participation to its allowance.
- One item per order in v1, one confirmed order per session, up to two payment attempts per checkout.
- One-minute generation steps; five-minute observation ticks; 15-minute maximum session lifetime.
- 14 completed simulated days for baseline history; a six-hour replay for the primary demo. Replay starts at 09:00 Europe/Berlin on 2026-11-21.
- All scheduled timestamps are stored as explicit UTC instants, with Europe/Berlin calendar conversion tested.
- Seeded pseudorandom generation and stable IDs. Seed, scenario config, and latent parameters are controller-only.

Use accelerated virtual time. At each observation tick, generate and deliver facts through the tick, create a fixed observation snapshot, and let the agent finish its bounded work. Wall-clock speed must not change generated outcomes. The controller advances time; the agent only reads the current snapshot.

## Generate operations, not independent metric curves

Use one event model. Metrics are projections of linked facts. Never directly lower the orders series and separately raise an unrelated payment-error series.

Proposed starting parameters below are configurable simulator parameters, not facts given to the agent or detection thresholds:
- Organic session arrivals: Poisson, base 120/minute across the company, scaled by local hour (0.35 overnight, 0.8 morning, 1.3 afternoon, 1.0 evening) and weekday/weekend (1.0/0.9).
- Default population mix: DE/FR 60/40; Android/Web 55/45. Split using categorical draws.
- Paid campaign impressions: Poisson around a planned schedule; base click probability 0.025; per-click cost 40-120 cents, determined by campaign and seeded variation. A paid click creates a session with probability 0.8, within one minute.
- Route each session to search (0.45), feed (0.35), or direct browse (0.20). Search/feed may show several products but select at most one product for the eventual checkout.
- Normal search: zero results probability 0.03; otherwise result click probability 0.45 within two minutes.
- Normal feed: select up to three in-stock cards, each with click probability 0.06; do not assume at most one click per session.
- Direct browse opens a product. A clicked search/feed result becomes a product view. A session with a product view adds to cart with probability 0.30, then starts checkout with probability 0.65.
- Provider selection 70/30. Final payment outcomes normally success 0.95, timeout 0.02, other failure 0.03. An unsuccessful customer retries once with probability 0.30 if time remains.
- All events/outcomes for a session occur within its 15-minute lifetime. Pending attempts resolve before that limit. Abandonment creates no order.
- At checkout confirm stock availability atomically with successful order creation. If unavailable, abandon before making a successful payment. A success and order commit together; stop retrying.
- Product prices 1,000-10,000 cents, commission 10-20% by SKU, normal platform discount 0-5%, variable cost 30 cents + 1% merchandise value per order. Round commission, discount, and percentage cost per line to integer cents.
- Initial SKU stock 500 units; six-hour replenishment batches restore toward 500. Record each receipt and sale. Tune demand/stock for availability scenarios rather than silently letting every baseline SKU sell out.

Before accepting these parameters, run baseline simulations over several seeds. Require adequate event counts in DE/Android, stable normal stock availability, and understandable funnel volumes. Record any calibration changes in a run manifest.

## Minimum source model

Every event table includes event_id, occurred_at, ingested_at, source_id, and event_type. Event-specific IDs are globally stable.

| Relation | Key fields beyond event envelope |
| --- | --- |
| sessions | session_id, started_at, country, platform, acquisition_channel, campaign_id? |
| journey_events | session_id, kind=view/cart/checkout, sku_id |
| search_requests | search_id, session_id, query_language, result_count, search_version |
| search_clicks | search_id, session_id, sku_id, clicked_at |
| feed_impressions | impression_id, session_id, sku_id, model_version, available_at_impression |
| feed_clicks | impression_id, session_id, clicked_at |
| payments | attempt_id, session_id, provider, checkout_release_version, outcome, finalised_at |
| orders / order_lines | order_id, successful_attempt_id, session_id, sku_id, quantity, unit_price_cents, commission_cents, discount_cents, operating_cost_cents |
| products | sku_id, category, seller_id, active, price_cents, commission_rate |
| stock_movements / snapshots | sku_id, receipt/sale quantity, resulting stock, snapshot_at |
| ad_events | campaign_id, kind=impression/click, cost_cents, target_country, advertising_channel |
| campaign_config | campaign_id, daily_budget_cents, effective_from/to, planned cumulative spend curve |
| context_events | context_id, kind=release/campaign/maintenance/message, scope, published_at, text |
| source_manifests | source_id, event_type, closed interval, declared unique record count, published_at |

Versioned product and campaign attributes are joined as of the fact time. Do not rewrite historical dimensions when configuration changes. Derived records inherit visibility restrictions from all input records.

Source manifests are normal producer evidence: publish interval counts shortly after interval closure via a separate delivery path. Do not expose withheld business-event payloads in manifests.

## Delivery and data-health behaviour

Generate an occurrence ledger privately, then independently deliver records into the observation store. Normal delivery delay is 0-60 seconds; manifests arrive within two minutes after interval close. Replay can delay or drop an event stream while manifests continue. This makes low order counts distinguishable from missing order records.

The agent sees only ingested records, manifests already published, and quality inferred from those. Querying an earlier snapshot must never reveal records ingested later. p95 lag only describes received events; when a stream stops completely, completeness and watermark age must expose the problem.

## Scenario mechanisms

Each scenario config contains private start/end, affected scope, parameter changes, and evaluator expectations. It is not an agent document.

| Scenario | Actual simulator change | Ordinary observable context |
| --- | --- | --- |
| payment regression | Raise timeout probability from 0.02 to 0.50 for one provider within DE/Android; reduce success by the same amount | Checkout release published at fault onset, plus unrelated releases as distractors |
| search relevance | Reduce result-click probability from 0.45 to 0.10 for German-language queries; keep result count and demand stable | Search release, affected query samples |
| recommendation availability | Disable stock filtering and bias ranking toward popular zero-stock SKUs; attractive-card click probability rises | Model rollout and inventory records |
| pacing failure | Ignore the planned cumulative spend limit; increase ad auction participation while retaining per-click charging | Campaign budget and schedule remain visible |
| unprofitable growth | Increase paid acquisition, increase platform discounts to 20%, and favour lower-commission products | Campaign announcement, product and cost records |
| innocent campaign | Increase a declared campaign's traffic with unchanged customer-quality, conversion, and cost rules | Announcement published before the campaign starts |
| ingestion delay | Delay order events 30 minutes while manifests continue | Late delivery and completeness evidence; no fabricated business outage |

For recommendation availability, make selected popular SKUs sell out through stock/demand changes and record those changes; do not mark an in-stock product unavailable only in a metric. Test standalone scenarios first; support composing them later without hardcoded scenario-specific diagnoses.

## Primary replay

Use a simple deterministic schedule as an initial fixture, not as agent knowledge:
- T+00: agent is already bootstrapped on baseline data; autonomous monitoring begins.
- T+30 to +60: announced innocent campaign; expect no unsupported failure alert.
- T+90 to +180: payment regression; expect autonomous detection before any new metric registration.
- T+135: contributor registers payment_timeout_rate; this demonstrates extension during a running incident, not the detection trigger.
- T+180 onward: normal behaviour restored; observe recovery through mature windows.
- End at T+360. Separate runs exercise other scenarios and the other two heldback metrics.

Vary onset, affected segment/provider, duration, seed, and distractor context in evaluation. The agent must not use the schedule above as an investigation script.

## Independence boundary

Run controller/evaluator outside the agent's workspace and credentials. The agent has only the observation adapter plus its own graph/incident storage. It cannot read the repository, scenario docs, generator source, seed, fault schedule, private ledger, or evaluator reports during a run.

A separate process alone is insufficient if both have unrestricted access to the same files. Use an isolated agent container or equivalent restricted filesystem/network boundary. The observation service owns the database; no direct DB connection is handed to the agent.

The presentation may show controller controls and ground truth in a separate operator view. Never include those fields in screenshots or DOM observations supplied to the agent.
