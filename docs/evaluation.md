# Evaluation: v1 vs v2 (fault injection with ground truth)

`python -m sentinel.eval.evaluate` replays the dev marketplace with known injected faults and scores the
agent against the ground truth. Two configurations run on identical data:

| config | monitor features |
|---|---|
| **v1** | single-dimension baselines (spike + CUSUM) only, the agent as tagged `sentinel-agent-v1` |
| **v2** | v1 + multi-dimensional **scan** (BH-FDR over dimension pairs) + **RiskLoc** clues handed to the investigator + **process** early warnings + **learning** (confirmed-normal patterns written back to the baseline) |

Each run executes in its own process (peak 0.40 GB for v1, 0.68 GB for v2), so the whole suite fits in
about 1 GB of free memory. `--resume` continues from finished runs, `--quick` runs one scenario.

## Suite

Every run simulates about 38 live hours (2026-10-26 00:00 to 2026-10-27 14:00 UTC) after 28 days of history.

| run | seed | injected faults (ground truth) | also present (must NOT page) |
|---|---|---|---|
| de-android | 42 | payment failure DE x Android (release payments-android) · for_you model shift (CTR up, out-of-stock cards, add-to-cart down) | autumn campaign on channel=paid, late data partition, app-ios release |
| fr-ios | 7 | payment failure FR x iOS, 11:00-16:00 (release payments-ios) · for_you model shift | same |
| es-web+supplier | 11 | payment failure ES x Web, 18:00-23:00 · supplier delay in electronics (stock-outs) · for_you model shift | same |

Ground truth per fault: the accepted first-cause metrics, the true segment, the propagation path and
the expected label. An alert matches a fault only if it falls in the fault window (+6 h) and names the
faulted segment (shares a key=value with it).

## Results

| | metric | v1 | v2 |
|---|---|---|---|
| **Monitor** | precision (alerts that match a fault) | 79% | 75% |
| | recall (faults alerted) | 100% | 100% |
| | false alarm rate (per fault-free monitored hour) | 2% | 5% |
| | innocent-change false alarm rate (planned changes paged) | 0% | 4% |
| | **MTTD** (fault start -> first alert) | 4.8 h | **3.8 h** |
| | trigger rate (investigations per live hour) | 0.46 | 0.54 |
| **Investigator** | top-1 root cause (first cause and segment) | 89% | 89% |
| | **root-cause recall** (compound faults, all causes found) | 89% | **100%** |
| | label accuracy | 100% | 93% |
| | **path recall** (true propagation path reproduced) | 15% | **31%** |
| | steps per investigation | 9.09 | 9.14 |
| | MTTR (alert -> concluded report, wall clock) | 1.3 s | 1.2 s |
| | **dead-end rate** | 56% | **49%** |
| | tool-call validity | 100% | 100% |
| | LLM tokens | 0 | 0 |

### Per run

| run | config | alerts | faults matched (alert time) | false alarms |
|---|---|---|---|---|
| de-android | v1 | 2 | payment 10-26 16:00 · for_you 10-27 14:00 | – |
| de-android | v2 | 2 | payment 10-26 16:00 · for_you 10-27 14:00 | – |
| fr-ios | v1 | 4 | payment 10-26 13:00 · for_you 10-27 14:00 | in_stock home 01:00, in_stock toys 11:00 |
| fr-ios | v2 | 5 | payment **10-26 12:00** · for_you **10-27 12:00** | same two + orders (total) 10-27 06:00 |
| es-web+supplier | v1 | 8 | supplier 10-26 21:00 (as *gmv, unexplained*) · payment 10-26 22:00 · for_you 10-27 12:00 | avg_order_value 21:00 |
| es-web+supplier | v2 | 7 | supplier **10-26 19:00** (as *add_to_cart_rate, supported cause*) · payment **10-26 19:00** · for_you 10-27 12:00 | avg_order_value 21:00 |

## What v2 changes

* **Earlier detection, about 1 h on average.** The scan and RiskLoc see a fault inside a slice before
  the slice's own series crosses its band: the FR x iOS payment failure was caught 1 h earlier, the
  ES x Web failure 3 h earlier, the supplier delay 2 h earlier and the for_you shift 2 h earlier in fr-ios.
* **Better root causes for compound incidents.** In es-web+supplier, v1 reported the supplier delay as an
  *unexplained GMV drop in electronics*. v2 reports *add_to_cart_rate in electronics, driven by
  in_stock_rate* as a supported cause, so the root-cause recall rises from 89% to 100% and the path recall
  doubles.
* **Fewer dead ends.** The investigator starts from RiskLoc's candidate set instead of walking the graph
  from the aggregate, so fewer investigations run out of hypotheses (56% -> 49%).
* **Cost.** v2 runs about 65% longer (105 s vs 61 s per simulated 38 h) and uses 0.68 GB instead of
  0.40 GB. Both are negligible next to an hourly monitoring cadence.

## Business value (estimate)

The euro impact each alert reports, times the hours v2 gains:

| fault | hours earlier | impact (EUR/h, from the alert) | loss avoided by earlier action |
|---|---|---|---|
| payment FR x iOS | 1 | ~120 | ~120 EUR |
| for_you shift (fr-ios) | 2 | ~250 | ~500 EUR |
| supplier delay electronics | 2 | ~480 | ~950 EUR |
| payment ES x Web | 3 | ~70 | ~210 EUR |
| **total, 3 scenarios** | | | **~1,800 EUR** |

This is an upper bound: it assumes the team acts on the alert as soon as it arrives. The qualitative gain
counts as much: v2's supplier report names the mechanism (stock-outs -> add-to-cart), which v1 left to a
human.

## Known weaknesses (honest reading)

* **One extra false alarm in v2** (fr-ios, orders total at 06:00 local, -28% in a low-volume hour,
  -151 EUR/h). No input of the decomposition is extreme on its own except the volume, so the alert is
  real noise at a quiet hour. The next step is a minimum-volume rule or a stricter early-morning band.
* **in_stock_rate false alarms** (fr-ios, both configs): SKU-level stock-outs in home and toys that do
  pass the euro threshold. They are not injected faults, but a human might still want them. Whether
  they count as false is a policy question.
* **Label accuracy 93% in v2**: one campaign-scoped incident (channel=paid) ends as *unexplained* instead
  of *explained*. It is not paged (below the threshold), so it costs no attention, but the label is wrong.
  The stricter planned-change rule (the plan's scope must be verifiable on the metric or its inputs)
  causes it. The rule is deliberate: before it, a planned campaign "explained" unrelated drops on timing
  alone.
* **Small suite.** Three seeds and seven fault instances: one alert moves precision by about 5 points.
  Treat differences below about 10 points as noise.

## Fixes the evaluation drove

Building the harness surfaced real bugs, now fixed and covered by tests:

1. **RiskLoc** picked tiny leaf combinations and returned causes on pure noise. It now uses the
   standardised deviation, a Bonferroni-adjusted significance gate against the rest of the data
   (contrast), and a concentration (lift) requirement. `tests/test_riskloc.py` covers single, compound,
   uniform and rising cases over 10 seeds.
2. **Scan noise**: cell tests now use each cell's own history spread and the planned/learned factors.
3. **Duplicate alerts per country** for a category-wide fault: when the alert's slice is not specific,
   the investigator widens to the total and localises again, so the duplicates merge.
4. **Amplified noise of difference metrics** (contribution = fees - costs): no page unless at least
   one formula input moved outside its own noise.
5. **Incident matching**: a slice must carry the deviation out of proportion to its volume, the
   best-covering incident wins, and a minor unexplained incident cannot absorb a much larger anomaly.
6. **Escalation**: an incident first seen through a favourable symptom (CTR up) is paged when harmful
   symptoms of the same root cause join it.
7. **Speed**: a scalar, cached profile key and a vectorised MAD make the monitor tick several times faster.

## Reproduce

```bash
python -m sentinel.eval.evaluate              # full suite, v1 and v2, about 10 minutes
python -m sentinel.eval.evaluate --quick      # one scenario
python -m sentinel.eval.evaluate --resume     # continue after an interruption
```

Outputs: `state/eval/results.json` (all metrics and matches per run), `state/eval/summary.md` (the table),
`state/eval/events_<run>_<config>.jsonl` (the full event trail of each run).
