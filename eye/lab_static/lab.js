"use strict";

const byId = (id) => document.getElementById(id);
const element = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
};
const time = (value) => value ? new Date(value).toISOString().slice(0, 16).replace("T", " ") + " UTC" : "—";
const countries = { DE: "Germany", FR: "France" };
const percent = new Intl.NumberFormat("en-GB", { style: "percent", maximumFractionDigits: 2 });
const segment = (scope) => Object.entries(scope || {}).filter(([, value]) => value != null)
  .map(([key, value]) => key === "country" ? countries[value] || value : key.replaceAll("_", " ") + " " + value)
  .join(" · ") || "Marketplace-wide";
const effects = {
  payment_timeout: ["More payment attempts time out", "timeout probability"],
  payment_success_delay: ["Authorized payment receipts arrive 90 minutes late", "of successful receipts delayed"],
  search_rollout: ["Release faster search to a share of eligible shoppers", "assigned to the release"],
  reduce_completions: ["Fewer successful completions", "of otherwise successful completions prevented"],
};
const transitions = {
  search: "Search request → search results",
  product_view: "Product page request → successful product view",
  cart_add: "Add-to-cart request → successful cart addition",
  payment: "Payment attempt → payment outcome",
};
const phases = {
  postgres: "Starting local PostgreSQL.",
  baseline: "The simulator is generating its historical baseline.",
  discovery: "Eye is discovering workflows and preparing monitoring.",
  ready: "Simulator and Eye are ready.",
  failed: "The lab could not continue."
};
let state = {};
let sending = false;
let messageKey = null;
let runKey = null;
let previewId = null;
let latestPlan = null;
let previewKey = null;
let selectedRunId = null;
let workflowKey = null;
let readingsKey = null;
let readingResult = null;
let readingsUnavailable = false;
let refreshSequence = 0;
let polling;
const active = (run) => ["queued", "publishing"].includes(run.status);

function fields(rows) {
  const list = element("dl");
  for (const [name, value] of rows) list.append(element("dt", name), element("dd", value));
  return list;
}

async function post(path, body) {
  byId("error").hidden = true;
  const options = { method: "POST" };
  if (body != null) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    const data = await response.json();
    throw new Error(typeof data.detail === "string" ? data.detail : "Enter a valid scenario or traffic request.");
  }
  return response.json();
}

function showError(error) {
  byId("error").textContent = error.message;
  byId("error").hidden = false;
}

async function action(path, body) {
  try {
    const result = await post(path, body);
    await refresh();
    return result;
  } catch (error) {
    showError(error);
    return false;
  }
}

function download(scenario) {
  const anchor = element("a");
  const url = URL.createObjectURL(new Blob([JSON.stringify(scenario, null, 2) + "\n"], { type: "application/json" }));
  anchor.href = url;
  anchor.download = "eye-scenario.json";
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function renderChat() {
  const messages = state.messages || [];
  const applied = (state.runs || []).map((run) => run.plan_id);
  const key = JSON.stringify([messages, applied, state.busy, state.phase]);
  if (key === messageKey) return;
  messageKey = key;
  const target = byId("messages");
  target.replaceChildren();
  for (const item of messages) {
    const turn = element("li");
    if (item.prompt) turn.append(element("span", "You", "speaker"), element("p", item.prompt));
    turn.append(element("span", item.origin === "import" ? "Imported plan" : "Devin", "speaker"),
      element("p", item.status === "pending" ? "Interpreting the scenario…" : item.message));
    if (item.scenario) {
      const review = element("button", applied.includes(item.id) ? "Review replay" : "Review plan", "secondary");
      review.addEventListener("click", () => {
        previewId = item.id;
        renderPreview();
        renderWorkflow();
        byId("workflow").scrollIntoView({ block: "start" });
      });
      turn.append(review);
    }
    target.append(turn);
  }
}

function operatorPremise(plan, planId) {
  return state.messages?.find((item) => item.id === planId)?.prompt || plan.premise;
}

function planFields(plan, planId) {
  const transition = plan.transition || (plan.fault_stage === "product_page_load" ? "product_view" : "payment");
  const [effect, severity] = effects[plan.effect || (transition === "payment" ? "payment_timeout" : "reduce_completions")];
  const premise = operatorPremise(plan, planId);
  return [
    ...(premise ? [["Lab premise", premise]] : []),
    ["Simulator effect", effect],
    ["Transition", transitions[transition]],
    ["Affected traffic", segment(plan.scope)],
    ["Severity", percent.format(plan.severity) + " " + severity],
    ["Duration", plan.duration_hours + (plan.duration_hours === 1 ? " virtual hour" : " virtual hours")]
  ];
}

function renderPreview() {
  const target = byId("plan-preview");
  const item = state.messages?.find((message) => message.id === previewId && message.scenario);
  const replay = state.runs?.some((run) => run.plan_id === previewId);
  const key = JSON.stringify([item, replay, state.busy, state.phase]);
  if (key === previewKey) return;
  previewKey = key;
  target.replaceChildren();
  target.hidden = !item;
  if (!item) return;
  const apply = element("button", replay ? "Apply replay to upcoming windows" : "Apply to upcoming windows");
  apply.disabled = state.phase !== "ready" || state.busy;
  apply.addEventListener("click", async () => {
    apply.disabled = true;
    const response = await action("/api/plans/" + item.id + "/apply");
    if (response) {
      selectedRunId = response.id;
      previewId = null;
      renderPreview();
      renderWorkflow();
    } else apply.disabled = false;
  });
  const exportButton = element("button", "Download JSON", "secondary");
  exportButton.addEventListener("click", () => download(item.scenario));
  const close = element("button", "Close preview", "secondary");
  close.addEventListener("click", () => {
    previewId = null;
    renderPreview();
    renderWorkflow();
  });
  const actions = element("div", null, "inline");
  actions.append(apply, exportButton, close);
  target.append(element("h3", replay ? "Replay preview" : "Scenario preview"), fields(planFields(item.scenario, item.id)), actions);
}

function renderObservations(target, readings, run) {
  target.replaceChildren(element("h3", "Observed during the scenario"));
  if (!readings) {
    const message = readingsUnavailable ? "Run readings are unavailable." : "Waiting for published scenario windows.";
    target.append(element("p", message));
    return;
  }
  const { count, rate } = labWorkflow;
  if (readings.transition === "search") {
    const wrapper = element("div", null, "workflow-table");
    const table = element("table");
    const header = element("tr");
    for (const label of ["Cohort", "Search sessions", "Recommendation exposure", "Direct orders",
      "Recommendation-assisted orders", "Total orders"]) {
      const cell = element("th", label);
      cell.scope = "col";
      header.append(cell);
    }
    const head = element("thead");
    head.append(header);
    const body = element("tbody");
    for (const arm of ["rollout", "holdout"]) {
      const row = element("tr");
      row.dataset.cohort = arm;
      const cohort = readings.cohorts[arm];
      const title = element("th", arm === "rollout" ? "Rollout" : "Holdout");
      title.scope = "row";
      const values = cohort ? [
        count(cohort.sessions),
        count(cohort.exposed) + " · " + rate(cohort.exposed, cohort.sessions),
        count(cohort.direct_orders), count(cohort.recommendation_orders), count(cohort.orders),
      ] : Array(5).fill("—");
      row.append(title, ...values.map((value) => element("td", value)));
      body.append(row);
    }
    table.append(head, body);
    wrapper.append(table);
    target.append(wrapper);
    const unclassified = Object.values(readings.cohorts).reduce((sum, cohort) => sum + cohort.unclassified_orders, 0);
    if (unclassified) target.append(element("p", count(unclassified) + " orders have no verified purchase path."));
    return;
  }

  const c = readings.counts;
  if (readings.transition === "payment") {
    target.append(fields([
      ["Payment attempts", count(c.attempted)],
      ["Authorized", count(c.authorized) + " · " + rate(c.authorized, c.attempted)],
      ["Declined / timed out", count(c.declined) + " / " + count(c.timed_out)],
      ["Orders confirmed", count(c.confirmed)],
      ["Receipts at cutoff", count(c.receipts_at_cutoff) + " received · " + count(c.in_transit_at_cutoff) + " in transit"],
      ["Delivery since cutoff", count(c.received_since_cutoff) + " arrived · " + count(c.in_transit_now) + " still in transit"],
    ]));
    target.append(element("p", "Receipt cutoff " + time(readings.effect_cutoff) + " · Observed through " + time(readings.observed_through)));
  } else {
    target.append(fields([
      ["Requests", count(c.requested)],
      ["Successful completions", count(c.completed) + " · " + rate(c.completed, c.requested)],
      ["Failed requests", count(c.failed)],
    ]));
  }
  if (active(run) && run.published < run.scenario.duration_hours) {
    target.append(element("p", "Readings are based on the scenario windows published so far."));
  }
}

function workflowSelection() {
  const draft = state.messages?.find((item) => item.id === previewId && item.scenario);
  if (draft) return { plan: draft.scenario, phase: "preview" };
  const scenarios = (state.runs || []).filter((run) => run.scenario);
  const run = scenarios.find((item) => item.id === selectedRunId) || scenarios.find(active) || scenarios[0];
  if (!run) return { phase: "base" };
  let phase = "results";
  if (run.status === "queued") phase = "queued";
  else if (run.status === "failed") phase = "stopped";
  else if (run.status === "publishing") {
    const clock = Date.parse(run.window_start || run.start_at);
    phase = clock >= Date.parse(run.start_at) && clock < Date.parse(run.end_at) ? "running" : "recovery";
  }
  return { plan: run.scenario, phase, run };
}

async function loadWorkflowReadings(run, key) {
  try {
    const response = await fetch("/api/runs/" + run.id + "/workflow", { cache: "no-store" });
    if (!response.ok) throw new Error();
    const readings = await response.json();
    if (readingsKey !== key) return;
    readingResult = readings;
  } catch {
    if (readingsKey !== key) return;
    readingsUnavailable = true;
  }
  renderWorkflow();
}

function renderWorkflow() {
  const { plan, phase, run } = workflowSelection();
  const key = run?.published ? JSON.stringify([run.id, run.published, state.next_window]) : null;
  if (key !== readingsKey) {
    const sameRun = key && readingsKey && JSON.parse(readingsKey)[0] === run.id;
    readingsKey = key;
    if (!sameRun) readingResult = null;
    readingsUnavailable = false;
    if (key) loadWorkflowReadings(run, key);
  }
  const signature = JSON.stringify([plan, phase, run?.id, run?.published, readingResult, readingsUnavailable]);
  if (signature === workflowKey) return;
  workflowKey = signature;
  const badge = byId("workflow-status");
  const labels = { preview: "Preview", queued: "Queued", running: "Running", recovery: "Recovery",
    results: "Run results", stopped: "Stopped" };
  badge.hidden = !plan;
  badge.textContent = labels[phase] || "";
  badge.dataset.phase = phase;
  byId("workflow-details").hidden = !plan;
  const settings = byId("workflow-plan");
  const observations = byId("workflow-readings");
  settings.hidden = observations.hidden = !run;
  if (run) {
    settings.replaceChildren(element("h3", "Scenario settings"), fields(planFields(plan, run.plan_id)));
    renderObservations(observations, readingResult, run);
  }
  labWorkflow.update(plan, phase, readingResult);
}

function renderTimeline(run) {
  const result = run.evaluation || {};
  const milestones = [
    { label: "Scenario started", at: run.start_at, pending: "Queued" },
    { label: run.published >= run.scenario.duration_hours ? "Scenario ended" : "Scheduled end",
      at: run.end_at, pending: "Awaiting start" },
    { label: "First Eye alert", at: result.alert_at,
      pending: result.windows_checked ? "No alert recorded during the scenario" : "Waiting for Eye" },
    { label: "Recorded recovery", at: result.recovery_declared_at,
      pending: result.first_alert ? "Pending" : "Awaiting an alert" },
  ];
  const ordered = milestones.sort((a, b) => (Date.parse(a.at) || Infinity) - (Date.parse(b.at) || Infinity));
  const list = element("ol", null, "timeline");
  for (const step of ordered) {
    const item = element("li");
    item.append(element("strong", step.label), element("span", step.at ? time(step.at) : step.pending));
    if (step.label === "First Eye alert" && result.first_alert) {
      const alert = result.first_alert;
      const elapsed = result.seconds_to_alert == null ? "" : " · " + result.seconds_to_alert + " real s";
      item.append(element("p", alert.metric_name + " · " + segment(alert.scope) + elapsed));
    }
    list.append(item);
  }
  return list;
}

function renderRuns() {
  const runs = state.runs || [];
  const key = JSON.stringify(runs);
  if (key === runKey) return;
  runKey = key;
  const target = byId("runs");
  if (!runs.length) return;
  target.replaceChildren();
  for (const run of runs) {
    const box = element("article", null, "run");
    box.append(element("h3", run.scenario ? "Scenario timeline" : "Normal traffic"));
    if (run.status === "failed") box.append(element("p", "The simulator stopped publishing this run."));
    else box.append(element("p", run.status === "queued" ? "Queued." :
      run.published + " of " + run.total + " virtual windows published."));
    if (run.scenario) {
      box.append(element("h4", "Applied plan"), fields(planFields(run.scenario, run.plan_id)), renderTimeline(run));
      const result = run.evaluation || {};
      const actions = element("div", null, "inline");
      const map = element("button", "Show on map", "secondary");
      map.type = "button";
      map.addEventListener("click", () => {
        previewId = null;
        selectedRunId = run.id;
        renderPreview();
        renderWorkflow();
        byId("workflow").scrollIntoView({ block: "start" });
      });
      actions.append(map);
      if (result.incident_id && state.eye_url) {
        const incident = element("a", "View Eye incident #" + result.incident_id);
        incident.href = new URL("?incident=" + encodeURIComponent(result.incident_id), state.eye_url);
        actions.append(incident);
      }
      box.append(actions);
    } else if (run.start_at) {
      box.append(fields([["Start", time(run.start_at)], ["End", time(run.end_at)]]));
    }
    target.append(box);
  }
}

async function refresh() {
  const request = ++refreshSequence;
  try {
    const response = await fetch("/api/lab", { cache: "no-store" });
    if (!response.ok) throw new Error("Lab status is temporarily unavailable.");
    const snapshot = await response.json();
    if (request !== refreshSequence) return;
    state = snapshot;
    byId("status").textContent = state.error || phases[state.phase];
    byId("status-dot").classList.toggle("connected", state.phase === "ready");
    byId("eye-link").hidden = !state.eye_url;
    if (state.eye_url) byId("eye-link").href = state.eye_url;
    byId("clock").textContent = state.next_window
      ? "Next window " + time(state.next_window) + " · Day " + state.pattern_day + " of 28 · Seed " + state.seed
      : "Waiting for the baseline.";
    const pending = state.messages.some((item) => item.status === "pending");
    byId("send").disabled = sending || pending || !state.devin_enabled;
    byId("send").textContent = pending ? "Devin is interpreting…" : state.devin_enabled ? "Ask Devin" : "Devin credentials required";
    byId("generate").disabled = state.phase !== "ready" || state.busy;
    const newest = [...state.messages].reverse().find((item) => item.scenario);
    if (newest && newest.id !== latestPlan) {
      latestPlan = newest.id;
      if (!state.runs.some((run) => run.plan_id === newest.id)) previewId = newest.id;
    }
    renderChat();
    renderPreview();
    renderRuns();
    renderWorkflow();
  } catch (error) {
    if (request === refreshSequence) byId("status").textContent = error.message;
  } finally {
    if (request === refreshSequence) {
      clearTimeout(polling);
      polling = setTimeout(refresh, state.busy ? 300 : 1500);
    }
  }
}

byId("chat").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = byId("prompt");
  sending = true;
  byId("send").disabled = true;
  if (await action("/api/chat", { prompt: input.value })) input.value = "";
  sending = false;
  refresh();
});
byId("normal").addEventListener("submit", (event) => {
  event.preventDefault();
  action("/api/normal", { hours: byId("hours").valueAsNumber });
});
byId("replay").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const plan = JSON.parse(await byId("scenario-file").files[0].text());
    const imported = await action("/api/replay", plan);
    if (imported) {
      previewId = imported.id;
      renderPreview();
      renderWorkflow();
      byId("workflow").scrollIntoView({ block: "start" });
    }
  } catch {
    showError(new Error("Choose a valid scenario JSON file."));
  }
});

refresh();
