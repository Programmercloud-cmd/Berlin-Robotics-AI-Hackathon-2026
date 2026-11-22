# Agent v4: how it was tuned

v4 = v3 (pruned BFS investigator, evidence anchoring) plus compatibility with the team's latest data
components, a data-profile / calibration layer, and the fixes three independent test beds exposed.
Every change is behind the feature flag `calibrate` (on in v4, off in v1-v3), so the versions stay comparable.

| config | monitor | investigator | calibration |
|---|---|---|---|
| v1 | single-dimension seasonal baselines, spike + CUSUM | greedy formula walk | off |
| v2 | v1 + dimension-pair scan, RiskLoc clues, process warnings, learned baselines | greedy | off |
| v3 | v2 | pruned BFS + evidence anchoring | off |
| v1+bfs | v1 | pruned BFS + evidence anchoring | off |
| **v4** | v3 + early warning on rolling windows | v3 + slow-harm attachment, premise hypotheses | **on** |

## 1. Test beds

1. **dev/fake_data** (own simulator, kickoff spec, 8 weeks history): 3 fault-injection scenarios,
   `python -m sentinel.eval.evaluate`.
2. **Shoply** (Chaimaa's simulator, branch `chaimaa`): independent data, 3 scenarios incl. two silent faults,
   `python -m sentinel.eval.evaluate --suite shoply` (needs the `.shoply-chaimaa` worktree).
3. **Jacob's Test Lab benchmark** (branch `jacob/v1`, `eye/benchmark_cases.py`, 10 cases with rubrics):
   `.venv-eye/Scripts/python -m sentinel.eval.eye_benchmark --cases timeouts --configs v1,v2,v3,v4 --review`.
   Eye and Sentinel run hour by hour on the same seeded database; scoring is rubric-based plus Jacob's Devin
   reviewer (rubric v2 prompt, v3 sessions API; needs `DEVIN_API_KEY` / `DEVIN_ORG_ID` in the environment).

## 2. What changed, in the order the test beds forced it

### Compatibility with jacob/v1 b8e7f83 (Eye)
- **A1** hourly series read only hour-aligned rolling windows (Eye now writes one every five minutes).
- **A2** event evidence only counts events ingested by the hour's end + 15 min (no lookahead).
- **A3** an hour is final only once published through its end + 15 min.
- **A4** releases measured from event version fields (`search_release`, `checkout_release_version`).

### Calibration layer (`sentinel/calibrate.py`), found on Shoply
- noise profile per metric (dispersion vs Poisson/binomial, cross-cell correlation) feeds the scan and RiskLoc;
- target metrics (e.g. pacing_ratio): a deviation either way is harm (advertiser overspend pages);
- planned changes without an end time end when the scoped volume is back at its unplanned baseline;
- slow harm in the same slice joins the incident of its unplanned root release; scoped releases are
  searched up to 12 h back; minor incidents can escalate on new harmful symptoms.

### Generalisation fixes, found on Jacob's benchmark (tiny history, weekend hours, no release calendar)
- minimum history 50 h -> 12 h; hour-key fallback (same local hour, any day type);
- small-sample spike threshold (Student-t quantile with the 4.5-sigma tail probability: ~6.7 at 16 h, ~4.5 at 56 days);
- the source's own "do not alert" flag (Eye catalog `direction: none`);
- co-occurring symptoms of one silent fault merge (linked metrics <= 2 hops, same hour, compatible slices);
  merges keep the narrower slice; merged incidents never page; explained / data-issue incidents never co-merge;
- alert grouping: an unexplained symptom in the slice and hour of a supported incident is listed, not paged;
- early warning: rolling one-hour windows checked every five minutes (Eye), published as EARLY_WARNING.

### Report quality, from the Devin review
- event baseline divides by the days that have data (was a fixed 7);
- the impact line states where the money figure comes from (no contradictory margin);
- narrowing when a slice carries >= 80 % of the change and its siblings stay normal;
- non-significant control tests are reported as such;
- a rate's own denominator activity is never a "mechanism";
- escalations cite an evidence record; escalation only counts symptoms that joined after the verdict.

## 3. Results

### Own data (dev/fake_data), 3 scenarios

| metric | v1 | v2 | v3 | v1+bfs | v4 before benchmark fixes | **v4 final** |
|---|---|---|---|---|---|---|
| precision | 79% | 75% | 75% | 79% | 64% | **76%** |
| recall | 100% | 100% | 100% | 100% | 100% | 100% |
| false alarm rate | 2% | 5% | 5% | 2% | 5% | **2%** |
| top-1 root cause | 89% | 89% | 89% | 89% | 89% | **100%** |
| compound root-cause recall | 89% | 100% | 100% | 89% | 100% | 100% |
| label accuracy | 100% | 93% | 93% | 100% | 93% | 83% |
| alerts | 14 | 14 | 14 | 14 | 20 | 15 |

### Shoply (independent data), 3 scenarios incl. silent faults

| metric | v3 | **v4** |
|---|---|---|
| recall | 75% | **92%** |
| compound root-cause recall | 75% | **92%** |
| label accuracy | 63% | **72%** |
| mean time to detect | 3.4 h | **3.0 h** |
| precision | 48% | 43% |

### Jacob's benchmark, case `timeouts` (DE / mobile / Stripe gateway timeouts)

| | Eye | v1 | v2 | v3 | v4 before | **v4 final** |
|---|---|---|---|---|---|---|
| passed (rubric) | yes | yes | yes | yes | yes | yes |
| first detection | 20 min | 60 | 60 | 60 | 60 | **35** |
| alerts (unrelated) | 1 (0) | 7 (6) | 7 (6) | 7 (6) | 5 (4) | **3 (2)** |
| Devin: observable break | 100% | 50% | 57% | 57% | 50% | 50% |
| Devin: scope | 100% | 29% | 36% | 36% | 40% | **50%** |
| Devin: evidence | 75-100% | 43% | 43% | 43% | 20% | **50%** |

## 4. Open issues

- Detection is still 15 min behind Eye: early warnings cover the total and single dimensions, not the
  DE x mobile x Stripe cell.
- About ten unsupported claims per report remain in Devin's review (mostly impact estimates).
- Label accuracy and path recall on the own data dropped (two non-paging campaign-slice incidents now
  "unexplained"); the own-data suite runs slower than v3 (partly fixed: cached graph distances, vectorised
  RiskLoc fallback).
- **Experimental:** `sentinel/frame.py` turns an operator paragraph into a problem on existing metrics
  (Devin maps the text; local grounding; verification on data; the usual investigation; claimed causes as
  `premise` hypotheses that stay unresolved unless the telemetry identifies them). First run on `timeouts`:
  Devin's framing was correct (symptoms, DE / mobile, noise, Frankfurt left unverified); the entry choice has
  since been fixed to the most upstream confirmed symptom, and a run that reuses a cached framing still
  returns no incident - not yet resolved. Only used with `--frame-text`; it does not affect v1-v4 results.
