# Shoply simulator (Data component of "Eye of the System")

Deterministic hourly data generator for the Shoply marketplace. Writes the six
DuckDB tables from the kickoff spec (`fact_traffic`, `fact_commerce`,
`fact_inventory`, `fact_ads`, `events`, `ingestion_log`) and stores only counts
and EUR totals; ratios are computed at query time.

## Usage

```bash
pip install -r requirements.txt
python -m shoply.sim --db shoply.duckdb --seed 42 --until 2026-10-29T00:00
python -m pytest -q
```

```python
from shoply.sim.simulator import Simulator
sim = Simulator("shoply.duckdb", seed=42)            # all scenarios on
sim.build_history()                                   # 2026-08-31 .. 2026-10-25 (8 weeks, no faults)
ts = sim.step()                                       # one live hour
sim.save_state("sim.pkl")                             # resume later:
sim = Simulator.load_state("sim.pkl", "shoply.duckdb")
sim.inject("android_payment_de")                      # start a scenario at the next hour
Simulator(":memory:", scenarios=None)                 # same seed, no scenarios (A/B counterfactual)
Simulator(":memory:", exclude=["pacing_bug"])         # optional scenarios can be excluded
```

## Simulator model (per hour)

- Sessions ~ Poisson(λ · Gamma(k=20, mean 1)); λ = 600000/24 · hour curve (Berlin local hour, peak 20:00,
  lunch bump 13:00) · weekend factor (mobile 1.15, web 0.9) · (1 + 0.001·day) · holiday (DE 2026-10-03 ×0.8)
  · scenario multipliers. Shares: country 40/25/15/10/10, platform 45/35/20, channel 60/25/15,
  category 25/30/20/15/10. Country and platform scale add-to-cart rate and AOV, so mix effects exist.
- Searches, search clicks, direct views, feed clicks → carts (add-to-cart ≈ 5.1% of views) → checkouts
  (0.35) → orders (payment success 0.94); cart abandonment = 1 − orders/carts ≈ 67%.
  Feed clicks on out-of-stock items never convert.
- GMV is Gamma per order around the category AOV (70/40/55/25/30); discounts 3%; fees by take rate;
  payment costs 1.4% + €0.10/checkout; €0.35 per paid session.
- 200 SKUs (Zipf popularity, lognormal 0.35 forecast error); POs every 3 days at 06:00 UTC up to
  9·1.15 days of an EWMA sales forecast, delivered 3 days later. 30 advertisers, budgets summing to
  €25k/day, spend capped by the remaining Berlin-day budget. ~2 harmless background releases/day.

## Layout

- `shoply/contracts.py` – contracts from the spec
- `shoply/sim/scenarios.yaml` – the 7 scenarios from the spec
- `shoply/sim/params.py` – every baseline parameter (spec values and assumptions)
- `shoply/sim/scenarios.py` – scenario loader and effect resolver
- `shoply/sim/simulator.py` – generator (traffic → commerce → inventory → ads → events → ingestion)
- `shoply/warehouse/schema.py` – exact DuckDB DDL
- `shoply/graph/` – metric graph: `graph.py`, `loader.py`, `export.py`, `data/*.yaml`

## Metric graph

`shoply/graph` is the business knowledge graph (NetworkX `MultiDiGraph`). Nodes are
`contracts.MetricNode`, edges are `contracts.Edge`, and every arrow points from the driver to
the metric it drives.

| Relationship | `Edge.type` | Source |
|---|---|---|
| FORMULA | `formula` | `data/formulas.yaml`: each name in `expr` is an edge input -> output |
| BREAKDOWN | `breakdown` | each metric's `dimensions` in `data/metrics.yaml` (metric -> itself, one per dimension) |
| POSSIBLE_CAUSE | `causal` | `data/causal.yaml` |
| CONTEXT | `context` | none supplied; supported for later additions |

```python
from shoply.contracts import Edge
from shoply.graph import load_graph, load_held_back
g = load_graph()                                           # 29 metrics, validated
g.paths("payment_success_rate", "orders")                  # payment_success_rate -> conversion_rate -> orders
g.formula("conversion_rate")                               # {"expr": ..., "inputs": {metric: op}}
g.possible_causes("add_to_cart_rate"); g.upstream("orders"); g.describe("payment_success_rate")
g.metrics(service="payments-android"); g.breakdowns("orders"); g.validate()
aov, extra = load_held_back()["avg_order_value"]           # withheld: only added on request
g.add_metric(aov, edges=g.formula_edges("gmv", "orders * avg_order_value"), extra=extra)
```

`add_metric`, `add_edge` and `add_formula` validate first and raise `GraphValidationError`
(`missing_metric`, `invalid_relationship`, `duplicate_id`, `incompatible_units`) without changing the graph.
Supplied fields that `MetricNode` has no slot for (`expr`, `note`, the two tables of a cross-table
ratio, `good_direction: two_sided`) are kept in `g.extra(id)`.

Export: `python -m shoply.graph --format json|dot|mermaid [--out FILE]`.

## Scenario semantics

- Effects change internal parameters before sampling, never the output.
- Numeric effects are multipliers, except these absolute values:
  `oos_exposure_share` (share of For You feed impressions on OOS items = 0.35),
  `delivery_lag_days` (electronics deliveries take 6 days), `pacing_controller: off`.
- `for_you_model_shift` affects only `channel=for_you` rows.
- `late_partition`: the 13:00 `fact_commerce` hour lands with 60% of rows; the rest
  arrive at the next tick and `ingestion_log.rows` is updated.
- `ios_release` is event-only (red herring).

## Determinism

Hour `h` (hours since 2026-08-31 UTC) draws from `SeedSequence([seed, h])`, one child stream per draw
site, by inverse CDF. A batch run, repeated `step()` calls and a `save_state`/`load_state` resume give
identical tables, and the with/without-scenario A/B comparison is exact outside a scenario's scope.

## Simulator check script

`python scripts/check_sim.py [--seed 42]` builds the history, runs 30 live hours with `android_payment_de` and
`autumn_campaign`, and prints with vs without scenarios (same seed): payment success for DE×android, DE×iOS and
FR×android on Mon 15–21 UTC, paid sessions and orders on Mon 06–12 UTC, overall conversion and cart abandonment,
and `check_formula` for every formula in `formulas.yaml`. Exits 1 if a formula fails.

## Tests

`tests/test_sim_acceptance.py` holds the spec acceptance tests (seed 42): conversion 2.6–3.6%, cart abandonment 66–72%, ~600k sessions/day,
determinism, payment-fault A/B, rollback, campaign A/B, no midnight cliff, slices sum to totals.
