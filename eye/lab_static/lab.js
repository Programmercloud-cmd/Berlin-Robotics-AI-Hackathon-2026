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
  reduce_completions: ["Fewer successful completions", "of otherwise successful completions prevented"],
};
const transitions = {
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
        byId("plan-preview").scrollIntoView({ block: "nearest" });
      });
      turn.append(review);
    }
    target.append(turn);
  }
}

function planFields(plan) {
  const transition = plan.transition || (plan.fault_stage === "product_page_load" ? "product_view" : "payment");
  const [effect, severity] = effects[plan.effect || (transition === "payment" ? "payment_timeout" : "reduce_completions")];
  return [
    ...(plan.premise ? [["Lab premise", plan.premise]] : []),
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
    if (await action("/api/plans/" + item.id + "/apply")) {
      previewId = null;
      renderPreview();
    } else apply.disabled = false;
  });
  const exportButton = element("button", "Download JSON", "secondary");
  exportButton.addEventListener("click", () => download(item.scenario));
  const close = element("button", "Close preview", "secondary");
  close.addEventListener("click", () => {
    previewId = null;
    renderPreview();
  });
  const actions = element("div", null, "inline");
  actions.append(apply, exportButton, close);
  target.append(element("h3", replay ? "Replay preview" : "Scenario preview"), fields(planFields(item.scenario)), actions);
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
      box.append(element("h4", "Applied plan"), fields(planFields(run.scenario)), renderTimeline(run));
      const result = run.evaluation || {};
      if (result.incident_id && state.eye_url) {
        const incident = element("a", "View Eye incident #" + result.incident_id);
        incident.href = new URL("?incident=" + encodeURIComponent(result.incident_id), state.eye_url);
        box.append(incident);
      }
    } else if (run.start_at) {
      box.append(fields([["Start", time(run.start_at)], ["End", time(run.end_at)]]));
    }
    target.append(box);
  }
}

async function refresh() {
  try {
    const response = await fetch("/api/lab", { cache: "no-store" });
    if (!response.ok) throw new Error("Lab status is temporarily unavailable.");
    state = await response.json();
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
      previewId = newest.id;
    }
    renderChat();
    renderPreview();
    renderRuns();
  } catch (error) {
    byId("status").textContent = error.message;
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
      byId("plan-preview").scrollIntoView({ block: "nearest" });
    }
  } catch {
    showError(new Error("Choose a valid scenario JSON file."));
  }
});

refresh();
setInterval(refresh, 1500);
