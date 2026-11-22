const byId = (id) => document.getElementById(id);
const runId = new URLSearchParams(location.search).get("run");
const runButton = byId("benchmark-start");
const sortButtons = [...byId("benchmark-scoreboard").querySelectorAll("button[data-sort]")];
const sortText = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
const label = (value) => value.replaceAll("_", " ");
const minutes = (value) => value == null ? "—" : `${value} min`;
const scope = (values) => Object.entries(values || {}).map(([key, value]) => `${label(key)} ${value}`).join(" · ") || "Marketplace";
let resultKey = "";
let starting = false;
let scoreboardRuns = [];
let sortKey = "started_at";
let descending = true;

function element(tag, content) {
  const node = document.createElement(tag);
  if (content != null) node.textContent = content;
  return node;
}

function link(text, href) {
  const anchor = element("a", text);
  anchor.href = href;
  return anchor;
}

function model(run) {
  return run.models.eye_model || run.models.eye_provider;
}

function suite(run) {
  return `v${run.version} · ${run.seed}`;
}

function sortValue(run) {
  if (sortKey === "started_at") return Date.parse(run.started_at);
  if (sortKey === "model") return model(run);
  if (sortKey === "suite") return suite(run);
  return run[sortKey];
}

function compareRuns(a, b) {
  const left = sortValue(a), right = sortValue(b);
  const newestFirst = () => Date.parse(b.started_at) - Date.parse(a.started_at);
  if (left == null || right == null) {
    return Number(left == null) - Number(right == null) || newestFirst();
  }
  const comparison = typeof left === "number" ? left - right : sortText.compare(left, right);
  return (descending ? -comparison : comparison) || newestFirst();
}

function renderScoreboard() {
  const rows = [...scoreboardRuns].sort(compareRuns).map((run) => {
    const row = element("tr");
    const title = element("th");
    title.scope = "row";
    title.append(link(new Date(run.started_at).toLocaleString(), `?run=${run.id}#benchmark-results`),
      element("div", label(run.status)));
    const description = run.description || run.commit_subject;
    if (description) {
      const note = element("div", description);
      note.className = "benchmark-run-note";
      title.append(note);
    }
    const commit = element("td");
    const commitLink = link(run.sha.slice(0, 8), `/api/benchmarks/commits/${run.sha}`);
    commitLink.title = run.sha;
    commit.append(commitLink);
    row.append(title, commit, element("td", suite(run)), element("td", model(run)),
      ...[`${run.completed}/${run.case_count}`, run.detected, run.false_incidents, run.incidents, run.reviewed,
        minutes(run.median_detection_minutes), minutes(run.median_recovery_minutes)]
        .map((value) => element("td", value)));
    return row;
  });
  if (!rows.length) {
    const row = element("tr");
    const cell = element("td", "No benchmark runs yet.");
    cell.colSpan = sortButtons.length;
    row.append(cell);
    rows.push(row);
  }
  byId("benchmark-runs").replaceChildren(...rows);
}

for (const button of sortButtons) {
  button.addEventListener("click", () => {
    descending = sortKey === button.dataset.sort ? !descending : button.hasAttribute("data-descending");
    sortKey = button.dataset.sort;
    byId("benchmark-scoreboard").querySelector("th[aria-sort]")?.removeAttribute("aria-sort");
    button.closest("th").setAttribute("aria-sort", descending ? "descending" : "ascending");
    renderScoreboard();
  });
}

function fields(values) {
  const list = element("dl");
  for (const [name, value] of values) list.append(element("dt", name), element("dd", value ?? "—"));
  return list;
}

function renderMethodology(run) {
  const settings = run.methodology;
  const values = [["Run description", run.description || "—"], ["Commit subject", run.commit_subject || "—"]];
  if (settings) {
    values.push(
      ["Replay", `Each case starts from the same seeded baseline, with ${settings.effect_hours} hour of case traffic and ${settings.follow_up_hours} hours of normal follow-up.`],
      ["Detection", `Time from case start to the first alert. Eye checks a trailing ${settings.window_minutes}-minute window every ${settings.evaluation_minutes} virtual minutes after a ${settings.grace_minutes}-minute ingestion grace period.`],
      ["Recovery", `Time from fault end until the last incident recovers. Each incident needs ${settings.recovery_evaluations} consecutive healthy evaluations.`],
      ["Benign cases", "False incidents count every incident opened during normal traffic or the traffic surge."],
      ["Report review", (run.review?.rubric_version ?? 1) >= 2
        ? "A separate Devin session reviews the initial report from its captured telemetry and uses it as context for what deeper findings add or revise."
        : "A separate Devin session reviews initial reports and deeper findings using each phase’s captured telemetry."],
    );
  }
  byId("benchmark-methodology-fields").replaceChildren(fields(values));
}

function renderCase(result) {
  const panel = element("details");
  panel.id = "case-" + result.id;
  panel.className = "benchmark-case";
  panel.append(element("summary", result.name));
  if (result.error) {
    panel.append(element("p", result.error));
    return panel;
  }
  const alert = result.first_alert;
  panel.append(fields([
    ["First alert", alert ? `${label(alert.metric_id)} · ${scope(alert.scope)}` : "None"],
    ["Detection time", minutes(result.detection_minutes)], ["False incidents", result.false_incidents],
    ["Incidents", result.incident_count], ["Recovered incidents", result.recovered_incidents],
    ["Recovery after case ended", minutes(result.recovery_minutes)],
  ]));
  if (result.incidents.length) {
    const list = element("ul");
    for (const incident of result.incidents) {
      const recovered = incident.recovered_at ? ` · recovered ${new Date(incident.recovered_at).toISOString().slice(11, 16)} UTC` : " · open";
      list.append(element("li", `Incident ${incident.id} · ${incident.step || label(incident.metric_id)} · ${scope(incident.scope)}${recovered}`));
    }
    panel.append(list);
  }
  if (!result.reports.length) panel.append(element("p", "No reports to review."));
  if (result.pending_reports) panel.append(element("p", `${result.pending_reports} report(s) still pending at the end of replay.`));
  for (const snapshot of result.reports) {
    const block = element("details");
    block.id = `${panel.id}-${snapshot.incident_id}-${snapshot.phase}`;
    block.className = "benchmark-review";
    const phase = snapshot.phase === "initial" ? "Initial report" : "Deeper findings";
    block.append(element("summary", `${phase} · Incident ${snapshot.incident_id}`));
    const review = result.reviews?.find((row) => row.id === `${result.id}/${snapshot.incident_id}/${snapshot.phase}`);
    if (review) {
      block.append(fields([["Observable break", review.observable_break], ["Scope", review.scope], ["Evidence", review.evidence],
        ["Unsupported claims", review.unsupported_claims.join(" · ") || "None"]]), element("p", review.notes));
    } else {
      block.append(element("p", "Review unavailable."));
    }
    const report = element("details");
    report.id = block.id + "-telemetry";
    report.append(element("summary", "Report and captured telemetry"), element("pre", JSON.stringify(snapshot, null, 2)));
    block.append(report);
    panel.append(block);
  }
  return panel;
}

function renderPlots(run) {
  const kinds = [];
  if (run.finished_at && run.cases.some((item) => !item.error)) {
    kinds.push(["outcomes", "Timing and incidents", "First alert, recovery time and incident count for each case"]);
  }
  if (run.finished_at && run.cases.some((item) => item.reviews?.length)) {
    kinds.push(["verdicts", "Report review verdicts", "Devin verdict counts by phase and review dimension"]);
  }
  return kinds.map(([kind, title, description]) => {
    const panel = element("details");
    panel.id = "benchmark-plot-" + kind;
    panel.className = "benchmark-plot";
    const url = `/api/benchmarks/${encodeURIComponent(run.id)}/plots/${kind}.png`;
    const image = element("img");
    image.src = url;
    image.alt = description;
    image.loading = "lazy";
    const caption = element("figcaption");
    caption.append(link("Open full-size PNG", url));
    const figure = element("figure");
    figure.append(image, caption);
    image.addEventListener("error", () => {
      figure.replaceChildren(element("p", "Plot unavailable. Install the local lab requirements to render it."));
    });
    panel.append(element("summary", title), figure);
    return panel;
  });
}

async function request(path, options) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Benchmark unavailable.");
  return data;
}

function showError(message) {
  const error = byId("benchmark-error");
  error.textContent = message;
  error.hidden = !message;
}

async function refresh() {
  try {
    const { checkout, runs } = await request("/api/benchmarks");
    scoreboardRuns = runs;
    renderScoreboard();
    const active = runs.find((run) => !run.finished_at);
    runButton.disabled = starting || Boolean(active) || !checkout.clean || !checkout.devin_enabled;
    byId("benchmark-status").textContent = active ? `${label(active.status)} · ${active.completed}/${active.case_count} cases`
      : !checkout.clean ? "Commit your changes and use a clean checkout to start a run."
      : !checkout.devin_enabled ? "Configure Devin and relaunch the lab to score reports."
      : `Ready to run ${checkout.sha.slice(0, 8)}`;
    if (!runId) return;
    const run = await request(`/api/benchmarks/${encodeURIComponent(runId)}`);
    const key = JSON.stringify(run);
    if (key === resultKey) return;
    resultKey = key;
    byId("benchmark-results").hidden = false;
    byId("benchmark-run-meta").textContent = [`Suite v${run.version}`, `seed ${run.seed}`, model(run),
      `Devin review (${run.models.devin_mode})`, run.review && `rubric v${run.review.rubric_version ?? 1}`,
      label(run.status), run.error].filter(Boolean).join(" · ");
    const open = [...document.querySelectorAll("#benchmark-results details[open]")].map((panel) => panel.id);
    renderMethodology(run);
    byId("benchmark-plots").replaceChildren(...renderPlots(run));
    byId("benchmark-cases").replaceChildren(...run.cases.map(renderCase));
    for (const id of open) if (byId(id)) byId(id).open = true;
  } catch (error) {
    showError(error.message);
  } finally {
    setTimeout(refresh, 2500);
  }
}

byId("benchmark-launch").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (runButton.disabled) return;
  starting = true;
  runButton.disabled = true;
  showError("");
  try {
    const run = await request("/api/benchmarks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description: byId("benchmark-description").value.trim() }),
    });
    location.href = `?run=${run.id}#benchmark-results`;
  } catch (error) {
    showError(error.message);
  } finally {
    starting = false;
  }
});

refresh();
