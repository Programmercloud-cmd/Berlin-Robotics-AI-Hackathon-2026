const clock = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", hour: "2-digit", minute: "2-digit" });
const percent = new Intl.NumberFormat("en", { style: "percent", maximumFractionDigits: 2 });
const date = (stamp) => stamp.slice(0, 10);
const time = (stamp) => clock.format(new Date(stamp.replace(" ", "T")));
const rate = (value) => percent.format(value);
const decimal = (value, places = 6) => value.toFixed(places);
const json = (value) => JSON.stringify(value, null, 2);
const record = (id, label = id + " ↗") => [label, "/evidence/" + id];
const session = "https://api.devin.ai/v3/organizations/{org_id}/sessions";
const currentSession = session + "/{session_id}";

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}

function table(headers, rows) {
  const wrapper = node("div", undefined, "method-table");
  const grid = node("table");
  const heading = node("thead");
  const headingRow = node("tr");
  for (const label of headers) {
    const cell = node("th", label);
    cell.scope = "col";
    headingRow.append(cell);
  }
  heading.append(headingRow);
  const body = node("tbody");
  for (const cells of rows) {
    const row = node("tr");
    for (const text of cells) row.append(node("td", String(text)));
    body.append(row);
  }
  grid.append(heading, body);
  wrapper.append(grid);
  return wrapper;
}

function code(text) {
  const pre = node("pre");
  pre.append(node("code", text));
  return pre;
}

function badgeLink([label, url]) {
  const link = node("a", label);
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener";
  return link;
}

function readOutput(read) {
  const result = read.result;
  if (!read.available) return [node("p", result.reason || result.error)];
  if (read.tool === "activity_mix") {
    return [table(["Activity", "Observed", "Historical mean"], result.activities.map((item) =>
      [item.activity, item.observed, item.expected]
    ))];
  }
  if (read.tool === "supporting_events") {
    return [table(["Event", "Occurred UTC", "Response"], result.map((event) =>
      [event.id, time(event.occurred_at), event.attrs.response]
    ))];
  }
  if (read.tool === "metric_breakdown") {
    const cohorts = result.filter((row) => row.scope.country && row.scope.platform && row.scope.payment_provider);
    return [table(["Scope", "Rate", "Finalized attempts"], (cohorts.length ? cohorts : result).map((row) =>
      [Object.values(row.scope).join(" / ") || "Marketplace", row.value == null ? "—" : rate(row.value), row.sample_size]
    ))];
  }
  return [node("p", result.node.label + " · " + result.edges.length + " typed edges")];
}

function requestedReads(reads) {
  return reads.map((read) => {
    const details = node("details", undefined, "method-read");
    const title = node("summary");
    title.append(node("code", read.id + " · " + read.tool));
    if (read.cited || !read.available) title.append(node("span", read.cited ? "Cited" : "Unavailable"));
    details.append(
      title,
      node("p", read.reason),
      code(json(read.params)),
      ...readOutput(read),
      badgeLink(record(read.id, "Full request and response ↗"))
    );
    return details;
  });
}

function conclusionDetails(data) {
  const detail = node("details", undefined, "method-read");
  const citations = node("div", undefined, "method-links");
  citations.append(...data.alternative.evidence_ids.map((id) => badgeLink(record(id))));
  detail.append(
    node("summary", "Alternative and stopping decision"),
    node("p", data.alternative.alternative),
    node("p", data.alternative.weakened_by),
    citations,
    node("p", data.stop_reason)
  );
  return detail;
}

const methods = {
  telemetry: (d) => [
    {
      id: "plan", label: "Seed the publisher",
      note: "Seed 42 produces patterned marketplace traffic. A one-hour fault changes the timeout probability for German mobile Stripe attempts at 09:00 UTC.",
      snippet: 'simulator.emit(db, 09:00 UTC, seed=42, patterned=True, scenarios=(payment_fault,))',
      links: [["Replay plan ↗", "/stripe-timeout.json"]]
    },
    {
      id: "event", label: "Emit the payment outcome",
      note: "For matching attempts the simulator compares a seeded draw with the configured timeout probability. A timeout emits payment_timed_out with a gateway_timeout response and qualified object links.",
      snippet: json({
        id: d.sample.id, activity: d.sample.activity, occurred_at: d.sample.occurred_at,
        ingested_at: d.sample.ingested_at, payment_id: d.sample.attrs.payment_id,
        response: d.sample.attrs.response
      }),
      links: [record(d.sample.evidence_id, "Sample and object links ↗")]
    },
    {
      id: "publish", label: "Persist the OCEL batch",
      note: "The fixture publishes through PostgreSQL. The transaction inserts the window and objects, then copies events, event→object links and expected counts by minute, source and activity.",
      snippet: "publish_window(...) → windows, objects, events, event_objects, source_manifests"
    },
    {
      id: "watermark", label: "Wait for the watermark",
      note: "Eye steps a rolling 60-minute window forward every five virtual minutes when published data reaches the window end plus 15 minutes of ingestion grace.",
      snippet: "next_start = last_window + 5 min\nwindow_end = next_start + 60 min\nobserved_at = window_end + 15 min\nrun the check when published_through ≥ observed_at"
    },
    {
      id: "lab-api", label: "Lab API equivalent",
      note: "These routes are used by the separate Lab UI. The recorded fixture drove the simulator directly.",
      snippet: 'POST /api/replay  { ...scenario }\nPOST /api/plans/{plan_id}/apply\nGET  /api/lab\nGET  /api/runs/{run_id}/workflow',
      links: [["Separate Lab preview ↗", d.lab_preview]]
    }
  ],
  discovery: (d) => [
    {
      id: "select", label: "Select observed events",
      note: "Discovery uses up to 24 recent source windows and filters on event occurrence and ingestion cutoff.",
      snippet: "events JOIN event_objects JOIN objects\noccurred_at < occurrence_limit\ningested_at ≤ observation_cutoff"
    },
    {
      id: "ocel", label: "Construct the object-centric log",
      note: "Eye builds the OCEL from events, objects and qualified links. For discovery it counts each event once per object even if the object has multiple link qualifiers.",
      snippet: "OCEL(event_frame, object_frame, relation_frame)\nunique (ocel:eid, ocel:oid) for discovery"
    },
    {
      id: "pm4py", label: "Mine typed relationships",
      note: "PM4Py discovers directly-follows pairs by object type. Eye saves activity nodes, lifecycle nodes, precedes edges and part_of edges.",
      snippet: "pm4py.discover_ocdfg(ocel, compute_edges_performance=False)\npm4py.discover_process_tree_inductive(payment_lifecycle)",
      links: [record(d.workflow_evidence, "Recorded graph neighbourhood ↗")]
    },
    {
      id: "edge", label: "Inspect a payment edge",
      note: d.workflow_edge.properties.object_types.payment + " payment-object event pairs support the discovered edge from payment attempted to payment timed out.",
      snippet: json(d.workflow_edge),
      links: [["Company graph ↗", d.dashboard + "#graph-panel"]]
    }
  ],
  registration: (d) => [
    {
      id: "define", label: "Declare the metric",
      note: "The fixture sets instrumentation.catalog to none and explicitly registers Payment success rate through runtime.register before publishing the fault.",
      snippet: 'runtime.register(dsn, {**CATALOG["payment_success_rate"], "cadence_minutes": 5})',
      links: [record(d.alert_evidence, "Registered definition ↗")]
    },
    {
      id: "formula", label: "Count finalized attempts",
      note: "The catalog measurement uses successful outcomes over finalized payment attempts. Declines and timeouts contribute to the denominator.",
      snippet: json(d.metric.calculation)
    },
    {
      id: "materialize", label: "Attach links and backfill",
      note: "Registration validates the calculation, creates the metric node and calculation-derived edges, materializes observations from existing events, then updates eligible metric associations.",
      snippet: d.graph_links.map((edge) => edge.kind + "  " + edge.target).join("\n"),
      links: [record(d.graph_evidence, "Recorded graph links ↗")]
    },
    {
      id: "http", label: "Metric API equivalent",
      note: "A catalog client can perform the registration over HTTP. The readings endpoint recomputes the requested time range and scope from telemetry.",
      snippet: 'POST /api/catalog/payment_success_rate/register\n{ "cadence_minutes": 5 }\n\nGET /api/catalog/payment_success_rate/readings\n    ?start=…&end=…&as_of=…&scope=…',
      links: [["Scoped replay readings ↗", d.readings_url]]
    }
  ],
  detection: (d) => {
    const a = d.alert;
    const r = a.detection_reference;
    const detail = d.detection;
    return [
      {
        id: "window", label: "Close and scope the window",
        note: "Eye evaluates " + time(a.window_start) + "–" + time(a.window_end) + " UTC at " + time(a.observed_at) + " UTC. The observation covers Germany, mobile and Stripe.",
        snippet: json(a.scope)
      },
      {
        id: "quality", label: "Check eligibility",
        note: "The detector requires an allowed scope, a due metric and at least three reference readings. The rate noise rule requires at least 30 current and 90 historical attempts. The inspected packet had complete delivery. The detector applies a 97% completeness gate when a completeness metric is registered.",
        snippet: detail.coverage.observed_events + " observed events / " + detail.coverage.manifest_events + " expected manifest events",
        links: [record(d.alert_evidence, "Delivery and sample evidence ↗")]
      },
      {
        id: "baseline", label: "Pool historical counts",
        note: "Eye looks back at up to four prior baseline days at the same UTC hour and minute. Three windows contributed here. The expected rate pools their counts. The detector needs four references to calculate the P10–P90 band.",
        snippet: r.baseline_numerator + " / " + r.baseline_denominator + " = " + rate(a.expected),
        view: () => table(["UTC date", "Successes", "Finalized", "Rate"], detail.history.map((row) =>
          [date(row.start), row.numerator, row.denominator, rate(row.value)]
        ))
      },
      {
        id: "noise", label: "Choose the noise scale",
        note: "Rate scoring uses the largest of historical population standard deviation, binomial sampling noise at the expected rate, and a 0.0125 floor.",
        snippet: "max(pstdev(history_rates), √(expected × (1 − expected) / current_n), 0.0125)\nmax(" +
          decimal(detail.population_sd) + ", " + decimal(detail.sampling_noise) + ", 0.012500) = " + decimal(r.noise)
      },
      {
        id: "score", label: "Score the drop",
        note: "Payment success monitors downward changes. Eye requires a negative signed score and magnitude at least " + r.score_threshold + ".",
        snippet: "observed = " + r.numerator + " / " + r.denominator + " = " + rate(a.observed) +
          "\nscore = (observed − expected) / noise = −" + decimal(a.score, 3)
      },
      {
        id: "tail", label: "Apply the binomial tail gate",
        note: "Eye adds a half-count to the historical successes and one to total attempts, then evaluates the one-sided lower tail. The replay probability below is recomputed from the archived counts.",
        snippet: "p₀ = (" + r.baseline_numerator + " + 0.5) / (" + r.baseline_denominator + " + 1) = " +
          decimal(detail.smoothed_probability) + "\nP[X ≤ " + r.numerator + "], X ~ Binomial(" +
          r.denominator + ", p₀) = " + detail.lower_tail.toExponential(6) + "\nRaise only when tail ≤ 0.000032."
      },
      {
        id: "group", label: "Group nested alerts",
        note: "After Eye upserts the alerts, it subtracts the narrow cohort from each broader one. Both the narrow and remaining cohorts need at least 30 attempts. The remaining rate must stay within its tolerance and explain at most 25% of the broader deviation.",
        snippet: "remainder_rate = (broad_rate × broad_n − narrow_rate × narrow_n) / remainder_n\n" +
          "rate_tolerance = max(0.025, 3 × √(remainder_baseline × (1 − remainder_baseline) / remainder_n))\n" +
          "|remainder_outcome_gap| ≤ 0.25 × |broad_outcome_gap|",
        view: () => table(["Broader scope", "Remaining N", "Remaining rate", "Expected", "Outcome gap"], d.grouping.map((row) =>
          [Object.values(row.broad_scope).join(" / ") || "Marketplace", row.other_sample_size,
            rate(row.other_observed_rate), rate(row.other_expected_rate), decimal(row.unexplained_outcomes, 2)]
        )),
        links: d.grouping.map((row) => record(row.id))
      },
      {
        id: "persist", label: "Open incident and queue investigation",
        note: "Eye opens the incident for the supported narrow scope, attaches the related alerts and saves the pending report context.",
        snippet: "anomalies → incidents → pending_reports\nGET /api/state?incident=" + d.incident,
        links: [record(d.alert_evidence, "Persisted trigger ↗"), ["Eye incident ↗", d.dashboard + "#signals-panel"]]
      }
    ];
  },
  recovery: (d) => [
    {
      id: "capture", label: "Freeze the incident context",
      note: "Eye captures the trigger, scoped history, delivery counts, typed neighbours and metric definitions at incident opening. Subsequent local reads use this fixed cutoff.",
      snippet: "incident #" + d.incident + " · " + time(d.observed_at) + " UTC cutoff\n" +
        d.alert_evidence + " alert and baseline · " + d.graph_evidence + " discovered neighbours",
      links: [record(d.alert_evidence), record(d.graph_evidence)]
    },
    {
      id: "create", label: "Turn 1 · Create and poll",
      note: "Eye creates a resumable Devin API v3 session with the frozen evidence and a strict structured output schema. It polls every 10 seconds with a ceiling of 480 seconds per turn.",
      snippet: "POST " + session +
        "\n{\n  prompt: system + \"\\n\" + JSON.stringify({ turn: 1, evidence, observation_cutoff, read_batches: 2, previous_turns: [] })," +
        "\n  structured_output_schema: Turn, structured_output_required: true,\n  repos: [], memory_enabled: false, resumable: true\n}" +
        "\n\nGET " + currentSession + "\nAccept structured_output only when its turn matches."
    },
    {
      id: "batch-one", label: "Execute the first 3 reads",
      note: "Devin chose these reads. Eye validates targets and executes the read-only observations locally, then persists each request and result.",
      snippet: "read_query(dsn, frozen_snapshot, query)\n→ read-only PostgreSQL observation / frozen graph lookup\n→ evidence row with params, result and observation cutoff",
      view: () => requestedReads(d.reads.slice(0, 3))
    },
    {
      id: "resume-two", label: "Turn 2 · Send results and poll",
      note: "Eye sends the previous batch to the existing session. Recorded continuation: " + d.continuations[0] + ".",
      snippet: "POST " + currentSession + "/messages\n{ message: instructions + JSON.stringify({ turn: 2, evidence: [E140, E141, E142] }) }" +
        "\n\nGET " + currentSession + "\nWait for turn 2 structured_output."
    },
    {
      id: "batch-two", label: "Execute the next 3 reads",
      note: "The second batch samples timeout events, compares marketplace outcomes, and inspects the graph around the timeout activity.",
      view: () => requestedReads(d.reads.slice(3, 6))
    },
    {
      id: "resume-three", label: "Turn 3 · Request cited report",
      note: "Recorded continuation: " + d.continuations[1] + ". Eye can start a new session with the accumulated context if a follow-up fails or returns no matching output.",
      snippet: "POST " + currentSession + "/messages\n{ message: instructions + JSON.stringify({ turn: 3, evidence: [E143, E144, E145] }) }" +
        "\n\nGET " + currentSession + "\nExpected output: { turn: 3, queries: [], report: Diagnosis }"
    },
    {
      id: "validate", label: "Validate, persist and clean up",
      note: "Eye requires the report’s citations to refer to persisted evidence, with a successful new read cited when available. It saves the cited conclusion and uses the statistical report if validation or reasoning fails.",
      snippet: "Citations: " + d.citations.join(", ") + "\nDELETE " + currentSession + "\n" +
        "Recorded sessions: " + d.session_count + ". Transport endpoints are reconstructed from the implementation.",
      view: () => conclusionDetails(d),
      links: [record(d.conclusion_evidence, "Conclusion, alternative and stop reason ↗"), ["Original report ↗", d.report_url]]
    },
    {
      id: "parallel", label: "In parallel · Verify recovery",
      note: "On due checks, the generic tracker resets the streak for matching breaches or an out-of-band reading. Missing, ineligible or insufficient comparisons are skipped. A healthy check requires |value − baseline| < 4 × noise. This run reached 12 qualifying checks at " + time(d.recovered_at) + " UTC with the report pending.",
      snippet: "required = max(2, 60 // cadence_minutes) = 12",
      view: () => table(["Window start UTC", "Observed", "Baseline", "Finalized attempts", "Evidence"], d.recovery.map((row) =>
        [time(row.window_start), rate(row.value), rate(row.expected), row.sample_size, row.id]
      )),
      links: [["All recovery observations ↗", "/evidence/recovery"]]
    }
  ]
};

export function renderMethods(details, stage, requested, data, focus) {
  if (!data) return;
  const entries = methods[stage](data);
  const found = entries.find((entry) => entry.id === requested);
  const current = found || entries[0];
  details.hidden = false;
  if (found || new URLSearchParams(location.search).get("technical") === "1") details.open = true;
  details.querySelector("#technical-count").textContent = entries.length + " steps";
  const rail = node("ol", undefined, "method-rail");
  for (const entry of entries) {
    const item = node("li");
    const anchor = node("a", entry.label);
    anchor.href = "#" + stage + "/" + entry.id;
    if (entry === current) anchor.setAttribute("aria-current", "step");
    item.append(anchor);
    rail.append(item);
  }
  const panel = node("section", undefined, "method-panel");
  const heading = node("h3", current.label);
  heading.tabIndex = -1;
  panel.append(heading, node("p", current.note));
  if (current.snippet) panel.append(code(current.snippet));
  if (current.view) {
    const view = current.view();
    panel.append(...(Array.isArray(view) ? view : [view]));
  }
  if (current.links) {
    const links = node("div", undefined, "method-links");
    links.append(...current.links.map(badgeLink));
    panel.append(links);
  }
  details.querySelector("#technical-body").replaceChildren(rail, panel);
  if (found) {
    if (focus) heading.focus({ preventScroll: true });
    requestAnimationFrame(() => {
      if (heading.getBoundingClientRect().top > window.innerHeight - 110) {
        details.scrollIntoView({ block: "start" });
      }
    });
  }
}
