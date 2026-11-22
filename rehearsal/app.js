const EYE = "http://127.0.0.1:8000";
const LAB = "http://127.0.0.1:8002";
const percent = new Intl.NumberFormat("en", { style: "percent", maximumFractionDigits: 1 });
const clock = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", hour: "2-digit", minute: "2-digit" });
const when = new Intl.DateTimeFormat("en-GB", {
  timeZone: "UTC", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"
});
const get = (id) => document.getElementById(id);
const label = (value) => value.replaceAll("_", " ");
const asDate = (timestamp) => new Date(timestamp.replace(" ", "T"));
const time = (timestamp) => clock.format(asDate(timestamp));
const wallTime = (timestamp) => asDate(timestamp).toISOString().slice(11, 19);
const incidentUrl = (incident) => EYE + "/?incident=" + incident.id + "#signals-panel";
const evidenceUrl = (incident) => EYE + "/api/incidents/" + incident.id + "/evidence";
const steps = [
  {
    id: "telemetry",
    title: "Telemetry",
    summary: "The Test Lab applies a private fault plan and publishes timestamped requests and outcomes for Eye to observe.",
    guidance: "Under Devin chat, click Review replay on the imported plan, then open the Run timeline and use Show on map to inspect the applied run.",
    links: (data) => [
      ["Open Test Lab preview ↗", LAB + "/#chat-title"],
      ["Run timeline ↗", LAB + "/#results"],
      ...(data?.counts ? [["Run outcome data ↗", LAB + "/api/runs/" + data.run.id + "/workflow"]] : []),
      ["Download replay plan", "/stripe-timeout.json", true]
    ],
    facts(data) {
      const notes = [line("Private Lab settings", "Germany, mobile, Stripe, a timeout probability setting of 85%, and two virtual hours.")];
      if (data?.counts) {
        const { attempted, authorized, timed_out, declined } = data.counts;
        notes.push(line("Published run", `${attempted} attempts, ${authorized} authorizations, ${timed_out} timeout events and ${declined} declines in the affected cohort.`));
      }
      const event = data?.event_capture?.event || data?.incident?.event;
      if (event) {
        const beforeFault = asDate(event.occurred_at) < asDate(data.run.start_at);
        notes.push(line("Eye’s event sample", `${event.activity} · ${event.attrs.response} · event ${event.id} · ${time(event.occurred_at)} virtual UTC` +
          (beforeFault ? " (before injection, inside the rolling detection window)." : ".")));
        if (data.event_capture?.event) {
          notes.push(line("Source", data.event_capture.tool + " returned the event with its linked payment, order, and session IDs."));
        }
      }
      if (data && !data.run) {
        notes.push(line("Fresh startup", "Start the Lab from the operator panel, download the plan, import it with Review saved scenario, and apply it once Eye is ready."));
      }
      return notes;
    }
  },
  {
    id: "discovery",
    title: "Discovery and graph",
    summary: "Eye uses PM4Py to discover activity sequences along shared objects and attaches metric nodes to the activities their calculations use.",
    guidance: "In the Company graph, open the eye menu and choose Journey, then follow checkout into payment and look for the attached metric nodes.",
    links: (data) => [
      ["Open Company graph ↗", (data?.incident ? EYE + "/?incident=" + data.incident.id : EYE + "/") + "#graph-panel"],
      ["Graph data ↗", EYE + "/api/state"]
    ],
    facts(data) {
      const edge = data?.graph?.workflow;
      if (!edge) return [];
      const count = edge.properties.object_types.payment;
      return [
        line("Discovered edge", "Payment attempted → Payment succeeded · precedes · " + edge.provenance + "."),
        line("Shared object", (count ? count.toLocaleString("en") + " observed pairs in the payment lifecycle. " : "") +
          "A metric’s measures edge comes from its calculation definition.")
      ];
    }
  },
  {
    id: "detection",
    title: "Independent detection",
    summary: "Eye opens an incident when a scoped payment success rate breaks from the comparable historical baseline, before the report worker writes its explanation.",
    guidance: "Read the selected Payment success incident, then open the initial trigger below to see the exact alert among its related alerts.",
    links: (data) => data?.incident ? [
      [`Open incident #${data.incident.id} ↗`, incidentUrl(data.incident)],
      ["Initial trigger evidence ↗", data.incident.trigger ? "/api/replay/trigger" : evidenceUrl(data.incident) + "?alerts_only=true"]
    ] : [["Open Eye investigations ↗", EYE + "/#signals-panel"]],
    facts(data) {
      const incident = data?.incident;
      if (!incident?.break || !incident.detection) return [line("Waiting for evidence", "A matching Payment success incident will appear here when Eye evaluates this replay.")];
      const reading = incident.break;
      const window = incident.detection;
      const notes = [
        line("Payment transition", `Germany · mobile · Stripe. ${percent.format(reading.observed)} of eligible attempts completed (${reading.completed} of ${reading.eligible}) versus ${percent.format(reading.expected)} expected.`),
        line("Evidence window", `${time(window.window_start)}–${time(window.window_end)} virtual UTC. Eye evaluated it at ${time(window.observed_at)} after the 15-minute ingestion grace period.`)
      ];
      if (incident.trigger?.detection_reference?.denominator) {
        const trigger = incident.trigger;
        const reference = trigger.detection_reference;
        notes.splice(1, 0, line("Trigger metric", `${percent.format(trigger.observed)} of finalized attempts succeeded (${reference.numerator} of ${reference.denominator}).`));
      }
      if (incident.registration.payment_report_pending) {
        notes.push(line("Captured at detection", "The incident was visible with the report pending and Payment timeout rate still awaiting registration."));
      }
      if (incident.alert_written_at && incident.report_written_at) {
        notes.push(line("Real UTC", `Alert persisted at ${wallTime(incident.alert_written_at)}. Original report saved at ${wallTime(incident.report_written_at)}.`));
      }
      return notes;
    }
  },
  {
    id: "registration",
    title: "Live metric registration",
    summary: "Registering Payment timeout rate adds a calculation, readings, and graph links using payment telemetry already flowing through Eye.",
    guidance: "Open Table → Payment timeout rate. Use Register if it is awaiting registration, or View metric → Connections when available. Scoped replay readings opens the fault windows directly.",
    links: (data) => [
      ["Open Eye metric catalog ↗", EYE + "/#table-tab"],
      ...(data?.readings_url ? [["Scoped replay readings ↗", data.readings_url]] : [])
    ],
    facts(data) {
      if (!data?.graph) return [];
      if (!data.graph.registered && data.incident?.registration.captured_at) {
        return [line("Catalog status", "Registration was requested after detection and is still processing.")];
      }
      if (!data.graph.registered) {
        return [line("Catalog status", "Awaiting registration. For a fresh replay, wait until the payment incident appears before registering.")];
      }
      const notes = [];
      const capture = data.incident?.registration;
      if (capture?.registered_at) {
        notes.push(line("Registration receipt", `Eye showed the metric as unregistered after detection at ${wallTime(capture.captured_at)} real UTC and returned the registration at ${wallTime(capture.registered_at)}.`));
      }
      const links = data.graph.metric_links;
      if (links.length) {
        notes.push(disclosure("Graph connections", links.map((edge) =>
          `${label(edge.kind)} → ${edge.target_label} · ${edge.provenance === "curator.llm" && edge.properties.reasoning_provider === "devin" ? "Devin proposal" : edge.provenance}`
        )));
      }
      return notes;
    }
  },
  {
    id: "recovery",
    title: "Investigation and recovery",
    summary: "Eye checks payment evidence and tracks the recovery streak while Devin can write the incident explanation from the evidence Eye gathered.",
    guidance: "Return to the same incident and open the evidence branches, Uncertainty, and Recovery; use Report history to see the first explanation.",
    links: (data) => data?.incident ? [
      [`Return to incident #${data.incident.id} ↗`, incidentUrl(data.incident)],
      ["Evidence and recovery readings ↗", evidenceUrl(data.incident)],
      ...(data.incident.report_written_at ? [["Original report ↗", EYE + "/api/incidents/" + data.incident.id + "/original-report"]] : [])
    ] : [["Open Eye investigations ↗", EYE + "/#signals-panel"]],
    facts(data) {
      const incident = data?.incident;
      if (!incident) return [];
      const notes = [];
      for (const branch of incident.branches) {
        const points = [
          ...branch.supports.map((point) => "Supports · " + point),
          ...branch.weakens.map((point) => "Weakens · " + point),
          ...branch.unresolved.map((point) => "Unresolved · " + point)
        ];
        if (points.length) notes.push(disclosure(branch.title, points));
      }
      if (incident.report_pending) notes.push(line("Report", "The explanation is still pending."));
      else if (incident.reasoning_provider === "devin") {
        notes.push(line("Devin", "Authored the investigation text from Eye’s scoped evidence. The report carries its Devin session provenance."));
      } else {
        notes.push(line("Report provenance", incident.reasoning === "statistical" ? "Eye used the statistical report for this run." :
          (incident.reasoning_provider || incident.reasoning || "Unavailable for this incident.")));
      }
      if (incident.uncertainty) notes.push(line("Uncertainty", incident.uncertainty));
      if (incident.recovered_at) {
        notes.push(line("Recovered", when.format(asDate(incident.recovered_at)) + " virtual UTC. " +
          (incident.recovery || incident.healthy_windows + " healthy evaluations.")));
      } else {
        notes.push(line("Recovery tracking", `${incident.healthy_windows} consecutive healthy evaluations recorded so far.`));
      }
      if (incident.final_checks.length) {
        notes.push(disclosure("Readings behind the recovery", incident.final_checks.map((reading) =>
          `${time(reading.window_start)} UTC window start · ${percent.format(reading.observed)} observed vs ${percent.format(reading.expected)} expected · ${reading.completed}/${reading.eligible}`
        )));
      }
      return notes;
    }
  }
];

function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

function line(heading, text) {
  const node = element("p");
  node.append(element("strong", heading), document.createTextNode(" · " + text));
  return node;
}

function disclosure(heading, items) {
  const node = element("details");
  const list = element("ul");
  list.append(...items.map((item) => element("li", item)));
  node.append(element("summary", heading), list);
  return node;
}

function setContent(target, entries) {
  const incoming = document.createElement("div");
  incoming.append(...entries);
  if (target.dataset.content === incoming.innerHTML) return;
  target.dataset.content = incoming.innerHTML;
  const open = new Set([...target.querySelectorAll("details[open] summary")].map((node) => node.textContent));
  incoming.querySelectorAll("details").forEach((node) => {
    node.open = open.has(node.firstElementChild.textContent);
  });
  target.replaceChildren(...incoming.childNodes);
}

let snapshot;
function render(focus = false) {
  const chosen = steps.findIndex((step) => "#" + step.id === location.hash);
  const index = Math.max(chosen, 0);
  const step = steps[index];
  const run = snapshot?.run;
  const incident = snapshot?.incident;
  const title = get("stop-title");
  title.textContent = step.title;
  get("stop-summary").textContent = step.summary;
  get("stop-guidance").textContent = step.guidance;
  get("position").textContent = `Step ${index + 1} of ${steps.length}`;
  get("previous").hidden = index === 0;
  if (index > 0) get("previous").href = "#" + steps[index - 1].id;
  get("next").hidden = index === steps.length - 1;
  if (index < steps.length - 1) get("next").href = "#" + steps[index + 1].id;
  for (const [i, link] of [...document.querySelectorAll(".pipeline a")].entries()) {
    if (i === index) link.setAttribute("aria-current", "step");
    else link.removeAttribute("aria-current");
  }
  const facts = step.facts(snapshot);
  get("facts").hidden = !facts.length;
  setContent(get("facts"), facts);
  setContent(get("links"), step.links(snapshot).map(([text, href, download]) => {
    const node = element("a", text);
    node.href = href;
    if (download) node.download = "";
    else {
      node.target = "_blank";
      node.rel = "noopener";
    }
    return node;
  }));
  if (snapshot) {
    const status = get("replay-status");
    const message = run?.start_at ?
      `Selected replay · ${when.format(asDate(run.start_at))}–${time(run.end_at)} virtual UTC` +
      (incident ? ` · Eye incident #${incident.id}` : " · waiting for a matching payment incident") :
      run ? "Stripe replay queued in the Test Lab." :
      snapshot.lab_status === "ready" ? "Apply the seeded replay in the Test Lab to populate the walkthrough." :
      "Waiting for the Test Lab. Open the operator panel to check startup.";
    if (status.textContent !== message) status.textContent = message;
  }
  if (focus) title.focus({ preventScroll: true });
}

window.addEventListener("hashchange", () => render(true));
async function refresh() {
  try {
    const response = await fetch("/api/replay");
    if (!response.ok) throw new Error();
    snapshot = await response.json();
    render();
  } catch {
    get("replay-status").textContent = "Live evidence is temporarily unavailable.";
  } finally {
    setTimeout(refresh, 15000);
  }
}
render();
refresh();
