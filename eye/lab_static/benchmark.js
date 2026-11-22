const byId = (id) => document.getElementById(id);
const runId = new URLSearchParams(location.search).get("run");
const runButton = byId("benchmark-start");
const label = (value) => value.replaceAll("_", " ");
const minutes = (value) => value == null ? "—" : `${value} min`;
const scope = (values) => Object.entries(values || {}).map(([key, value]) => `${label(key)} ${value}`).join(" · ") || "Marketplace";
let resultKey = "";
let starting = false;

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

function renderScoreboard(runs) {
  const rows = runs.map((run) => {
    const row = element("tr");
    const title = element("th");
    title.scope = "row";
    title.append(link(new Date(run.started_at).toLocaleString(), `?run=${run.id}#benchmark-results`),
      element("div", label(run.status)));
    const commit = element("td");
    const commitLink = link(run.sha.slice(0, 8), `/api/benchmarks/commits/${run.sha}`);
    commitLink.title = run.sha;
    commit.append(commitLink);
    row.append(title, commit, element("td", `v${run.version} · ${run.seed}`), element("td", model(run)),
      ...[`${run.completed}/${run.case_count}`, run.detected, run.false_incidents, run.incidents, run.reviewed]
        .map((value) => element("td", value)));
    return row;
  });
  if (!rows.length) {
    const row = element("tr");
    const cell = element("td", "No benchmark runs yet.");
    cell.colSpan = 9;
    row.append(cell);
    rows.push(row);
  }
  byId("benchmark-runs").replaceChildren(...rows);
}

function fields(values) {
  const list = element("dl");
  for (const [name, value] of values) list.append(element("dt", name), element("dd", value ?? "—"));
  return list;
}

function renderCase(result) {
  const article = element("article");
  article.id = "case-" + result.id;
  article.append(element("h3", result.name));
  if (result.error) {
    article.append(element("p", result.error));
    return article;
  }
  const alert = result.first_alert;
  article.append(fields([
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
    article.append(list);
  }
  if (!result.reports.length) article.append(element("p", "No reports to review."));
  if (result.pending_reports) article.append(element("p", `${result.pending_reports} report(s) still pending at the end of replay.`));
  for (const snapshot of result.reports) {
    const block = element("div");
    block.className = "benchmark-review";
    const phase = snapshot.phase === "initial" ? "Initial report" : "Deeper findings";
    block.append(element("h4", `${phase} · Incident ${snapshot.incident_id}`));
    const review = result.reviews?.find((row) => row.id === `${result.id}/${snapshot.incident_id}/${snapshot.phase}`);
    if (review) {
      block.append(fields([["Observable break", review.observable_break], ["Scope", review.scope], ["Evidence", review.evidence],
        ["Unsupported claims", review.unsupported_claims.join(" · ") || "None"]]), element("p", review.notes));
    } else {
      block.append(element("p", "Review unavailable."));
    }
    const report = element("details");
    report.append(element("summary", "Report and captured telemetry"), element("pre", JSON.stringify(snapshot, null, 2)));
    block.append(report);
    article.append(block);
  }
  return article;
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
    renderScoreboard(runs);
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
      `Devin review (${run.models.devin_mode})`, label(run.status), run.error].filter(Boolean).join(" · ");
    byId("benchmark-cases").replaceChildren(...run.cases.map(renderCase));
  } catch (error) {
    showError(error.message);
  } finally {
    setTimeout(refresh, 2500);
  }
}

runButton.addEventListener("click", async () => {
  starting = true;
  runButton.disabled = true;
  showError("");
  try {
    const run = await request("/api/benchmarks", { method: "POST" });
    location.href = `?run=${run.id}#benchmark-results`;
  } catch (error) {
    showError(error.message);
  } finally {
    starting = false;
  }
});

refresh();
