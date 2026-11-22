import { evidenceRail, renderMethods } from "./methods.js";

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

const steps = [
  {
    id: "telemetry",
    label: "Telemetry",
    title: "Private plan. Observed events.",
    summary: "The simulator applies the fault and publishes payment events to Eye.",
    links: (data) => [
      ["Observed event ↗", evidence(data.sample.evidence_id)],
      ["Replay plan ↓", "/stripe-timeout.json", true]
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
    label: "Discovery",
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
    label: "Metric registration",
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
    label: "Detection",
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
    label: "Investigation & recovery",
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
      return [proof, datum("Devin requested", data.reads.length + " evidence reads")];
    }
  }
];

const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
let chapters = [byId("landing")];
let viewportFrame = 0;

function renderChapter(step, data) {
  const chapter = element("section", undefined, "chapter");
  chapter.id = step.id;
  const heading = element("h2", step.title);
  heading.id = step.id + "-title";
  heading.tabIndex = -1;
  chapter.setAttribute("aria-labelledby", heading.id);
  const shell = element("div", undefined, "shell chapter-shell");
  const intro = element("div", undefined, "story-row chapter-intro");
  const copy = element("div", undefined, "story-copy");
  const facts = element("div", undefined, "facts");
  facts.append(...step.facts(data));
  copy.append(element("p", step.label, "eyebrow"), heading, element("p", step.summary, "summary"), facts);
  intro.append(copy, evidenceRail(step.links(data), step.label));
  shell.append(intro, renderMethods(step.id, data));
  chapter.append(shell);
  return chapter;
}

function chapterIndex() {
  const line = document.querySelector(".bar").offsetHeight + 60;
  return Math.max(0, chapters.findLastIndex((chapter) => chapter.getBoundingClientRect().top <= line));
}

function updateViewport() {
  viewportFrame = 0;
  const maximum = document.documentElement.scrollHeight - innerHeight;
  document.documentElement.style.setProperty("--page-progress", maximum > 0 ? scrollY / maximum : 0);
  const index = chapterIndex();
  const label = index ? steps[index - 1].label : "Devin-led investigation";
  if (byId("current-chapter").textContent !== label) byId("current-chapter").textContent = label;
  const cue = innerHeight * .47;
  for (const chapter of chapters.slice(1)) {
    const timeline = chapter.querySelector(".method-timeline");
    const bounds = timeline.getBoundingClientRect();
    const progress = Math.min(1, Math.max(0, (cue - bounds.top) / bounds.height));
    timeline.style.setProperty("--trace-progress", progress);
  }
}

function scheduleViewport() {
  if (!viewportFrame) viewportFrame = requestAnimationFrame(updateViewport);
}

function goTo(target, smooth = false) {
  if (!target) return;
  target.scrollIntoView({ block: "start", behavior: smooth && !reducedMotion.matches ? "smooth" : "instant" });
  target.querySelector("h1, h2, h3, h4")?.focus({ preventScroll: true });
}

function showHash() {
  goTo(byId(location.hash.slice(1)));
}

function handleChapterKey(event) {
  if (event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey ||
      (event.target instanceof Element && event.target.closest("button, input, select, textarea, [contenteditable]"))) return;
  const forward = event.key === "ArrowDown" || (event.code === "Space" && !event.shiftKey);
  const backward = event.key === "ArrowUp" || (event.code === "Space" && event.shiftKey);
  if (!forward && !backward) return;
  // Horizontal evidence tables keep native arrow scrolling when focused.
  if (event.target instanceof Element && event.target.closest(".method-table")) return;
  const target = chapters[chapterIndex() + (forward ? 1 : -1)];
  if (!target) return;
  event.preventDefault();
  if (event.repeat) return;
  if (location.hash !== "#" + target.id) history.pushState(null, "", "#" + target.id);
  goTo(target, true);
}

async function loadReplay() {
  try {
    const response = await fetch("/api/replay");
    if (!response.ok) throw new Error();
    const data = await response.json();
    const rendered = steps.map((step) => renderChapter(step, data));
    byId("story").append(...rendered);
    chapters = [byId("landing"), ...rendered];
    byId("replay-status").textContent = "Seed " + data.seed + " · DE / Mobile / Stripe";
    byId("begin").hidden = false;
    byId("keyboard-guide").hidden = false;
    if (location.hash) requestAnimationFrame(showHash);
    scheduleViewport();
  } catch {
    byId("replay-status").textContent = "Recorded replay unavailable";
  }
}

window.addEventListener("hashchange", showHash);
window.addEventListener("scroll", scheduleViewport, { passive: true });
window.addEventListener("resize", scheduleViewport);
document.addEventListener("keydown", handleChapterKey);
loadReplay();
