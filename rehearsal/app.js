const byId = (id) => document.getElementById(id);
const clock = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", hour: "2-digit", minute: "2-digit" });
const percent = new Intl.NumberFormat("en", {
  style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1
});
const time = (timestamp) => clock.format(new Date(timestamp.replace(" ", "T")));
const evidence = (id) => "/evidence/" + id;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function link([text, href, download]) {
  const node = element("a", text);
  node.href = href;
  if (download) node.download = "";
  else {
    node.target = "_blank";
    node.rel = "noopener";
  }
  return node;
}

function datum(label, value) {
  const row = element("div", undefined, "datum");
  row.append(element("span", label), element("strong", value));
  return row;
}

function miniPanel(...nodes) {
  const panel = element("div", undefined, "mini-panel");
  panel.append(...nodes);
  return panel;
}

function reading(label, value) {
  const row = element("div", undefined, "reading");
  const track = element("span", undefined, "bar-track");
  const fill = element("i", undefined, "bar-fill");
  track.setAttribute("aria-hidden", "true");
  fill.style.setProperty("--reading", (value * 100) + "%");
  track.append(fill);
  row.append(element("span", label, "panel-label"), element("strong", percent.format(value)), track);
  return row;
}

function trace(reads) {
  const details = element("details", undefined, "trace");
  const list = element("ul");
  details.append(element("summary", reads.length + " Devin-requested reads"), list);
  for (const read of reads) {
    const row = element("li");
    const anchor = link([read.title, evidence(read.id)]);
    anchor.prepend(element("code", read.id));
    if (read.cited || !read.available) {
      anchor.append(element("small", read.available ? "Cited" : "Unavailable"));
    }
    row.append(anchor);
    list.append(row);
  }
  return details;
}

const steps = [
  {
    id: "telemetry",
    title: "Private plan. Observed events.",
    summary: "The simulator applies the fault and publishes payment events to Eye.",
    links: (data) => [
      ["Observed event ↗", evidence(data.sample.evidence_id)],
      ["Replay plan ↓", "/stripe-timeout.json", true],
      ["Lab preview · separate run ↗", data.lab_preview]
    ],
    facts(data) {
      const event = element("div", undefined, "event");
      event.append(
        element("span", "Eye receives", "panel-label"),
        element("code", data.sample.activity + " · " + data.sample.attrs.response),
        element("span", time(data.sample.occurred_at) + " UTC · linked payment, order and session", "proof-note")
      );
      return [datum("Lab controls", "85% timeout probability · 1 virtual hour"), event];
    }
  },
  {
    id: "discovery",
    title: "Discover the workflow.",
    summary: "Eye links activity sequences observed on the same business objects.",
    links: (data) => [
      ["Company graph ↗", data.dashboard + "#graph-panel"],
      ["Discovered edge ↗", evidence(data.workflow_evidence)]
    ],
    facts() {
      const graph = element("div", undefined, "graph-demo");
      const flow = element("div", undefined, "graph-flow");
      flow.append(
        element("span", "Payment attempted", "graph-node"),
        element("span", "→", "graph-arrow"),
        element("span", "Payment timed out", "graph-node")
      );
      graph.append(flow, element("p", "precedes · payment lifecycle", "graph-caption"));
      return [graph];
    }
  },
  {
    id: "registration",
    title: "Register the measure.",
    summary: "The replay registers Payment success rate on the discovered stream before the fault.",
    links: (data) => [
      ["Metric and graph links ↗", evidence(data.graph_evidence)],
      ["Scoped readings ↗", data.readings_url]
    ],
    facts(data) {
      const formula = element("div", undefined, "formula");
      formula.append(element("span", "Successful payments"), element("span", "Finalized payment attempts"));
      const metric = data.metric;
      return [
        miniPanel(element("span", metric.name, "panel-label"), formula),
        datum("Check cadence", "Every " + metric.access.cadence_minutes + " virtual minutes")
      ];
    }
  },
  {
    id: "detection",
    title: "Eye opens the incident.",
    summary: "Eye persists the scoped breach before Devin’s report is ready.",
    links: (data) => [
      ["Incident #" + data.incident + " ↗", data.dashboard + "#signals-panel"],
      ["Trigger and baseline ↗", evidence(data.alert_evidence)]
    ],
    facts(data) {
      const alert = data.alert;
      const bars = miniPanel(reading("Observed", alert.observed), reading("Expected", alert.expected));
      bars.classList.add("bars");
      return [
        bars,
        datum("Evidence window", time(alert.window_start) + "–" + time(alert.window_end) + " UTC"),
        datum("Eye evaluates", time(data.observed_at) + " UTC")
      ];
    }
  },
  {
    id: "recovery",
    title: "Investigate. Track recovery.",
    summary: "Devin chooses bounded reads and cites its conclusion while Eye tracks recovery independently.",
    links: (data) => [
      ["Cited conclusion ↗", evidence(data.conclusion_evidence)],
      ["Recovery checks ↗", "/evidence/recovery"],
      ["Eye incident ↗", data.dashboard + "#signals-panel"]
    ],
    facts(data) {
      const proof = element("div", undefined, "proof");
      const investigator = element("div");
      investigator.append(
        element("strong", data.timeouts.observed + " timeouts"),
        element("span", "≈" + Math.round(data.timeouts.expected) + " expected · cause unverified", "proof-note")
      );
      const recovery = element("div", undefined, "recovered");
      recovery.append(
        element("strong", data.recovery.length + " healthy checks"),
        element("span", time(data.recovered_at) + " UTC" + (data.recovered_pending ? " · report pending" : ""), "proof-note")
      );
      const checks = element("div", undefined, "checks");
      checks.setAttribute("aria-hidden", "true");
      data.recovery.forEach((point, index) => {
        const tick = element("i");
        tick.style.setProperty("--index", index);
        tick.title = time(point.window_start) + " UTC · " + percent.format(point.value);
        checks.append(tick);
      });
      recovery.append(checks);
      proof.append(investigator, recovery);
      return [proof, trace(data.reads)];
    }
  }
];

const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const pipelineLinks = [...document.querySelectorAll(".pipeline a")];
let snapshot;
let activeIndex = -1;
let transition;

function render(focus = false) {
  const selected = steps.findIndex((step) => "#" + step.id === location.hash);
  const index = Math.max(selected, 0);
  const step = steps[index];
  const changed = index !== activeIndex;
  activeIndex = index;
  const title = byId("stop-title");
  title.textContent = step.title;
  byId("position").textContent = (index + 1) + " / " + steps.length;
  byId("stop-summary").textContent = step.summary;
  byId("previous").hidden = index === 0;
  if (index > 0) byId("previous").href = "#" + steps[index - 1].id;
  byId("next").hidden = index === steps.length - 1;
  if (index < steps.length - 1) byId("next").href = "#" + steps[index + 1].id;
  document.querySelector(".pipeline").style.setProperty("--progress", index / (steps.length - 1));
  pipelineLinks.forEach((anchor, position) => {
    if (position === index) anchor.setAttribute("aria-current", "step");
    else anchor.removeAttribute("aria-current");
    anchor.toggleAttribute("data-complete", position < index);
  });
  const facts = byId("facts");
  if (snapshot) {
    facts.hidden = false;
    facts.replaceChildren(...step.facts(snapshot));
    byId("links").replaceChildren(...step.links(snapshot).map(link));
  }
  if (changed && focus) {
    title.focus({ preventScroll: true });
    transition?.cancel();
    if (!reducedMotion.matches) {
      transition = byId("stop").animate(
        [{ opacity: .3, transform: "translateY(7px)" }, { opacity: 1, transform: "translateY(0)" }],
        { duration: 310, easing: "cubic-bezier(.2,.75,.22,1)" }
      );
    }
  }
}

async function loadReplay() {
  try {
    const response = await fetch("/api/replay");
    if (!response.ok) throw new Error();
    snapshot = await response.json();
    byId("replay-status").textContent = "Seed " + snapshot.seed + " · DE / Mobile / Stripe";
    const dashboard = byId("dashboard-link");
    dashboard.href = snapshot.dashboard;
    dashboard.hidden = false;
    render();
  } catch {
    byId("replay-status").textContent = "Recorded replay unavailable";
  }
}

window.addEventListener("hashchange", () => render(true));
render();
loadReplay();
