const byId = (id) => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat("en-GB", {
  month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "UTC",
});
const numberFormat = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 3 });
const date = (value) => value && Number.isFinite(Date.parse(value)) ? dateFormat.format(new Date(value)) : "—";
const number = (value) => value == null ? "—" : numberFormat.format(value);
const words = (value) => String(value ?? "").replaceAll("_", " ");
const scope = (value) => Object.entries(value || {}).map(([key, val]) => words(key) + " " + val).join(" · ") || "Marketplace";

function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content != null) node.textContent = content;
  if (className) node.className = className;
  return node;
}

const theme = getComputedStyle(document.documentElement);
const tone = (token) => theme.getPropertyValue(token).trim();
function tint(token, amount) {
  const surface = tone("--surface").slice(1);
  const color = tone(token).slice(1);
  return "#" + [0, 2, 4].map((offset) => {
    const from = parseInt(surface.slice(offset, offset + 2), 16);
    const to = parseInt(color.slice(offset, offset + 2), 16);
    return Math.round(from + (to - from) * amount).toString(16).padStart(2, "0");
  }).join("");
}

const graphStyles = () => [
  { selector: "node", style: {
    label: "data(label)", width: 110, height: 39, shape: "round-rectangle",
    "text-wrap": "wrap", "text-max-width": 99, "text-valign": "center",
    "font-size": 13, "font-family": "system-ui, sans-serif", color: tone("--text"),
    "border-color": tone("--accent"), "border-width": 1, "background-color": tint("--accent", .10),
  } },
  { selector: "node[kind = 'metric']", style: { "background-color": tint("--focus", .12), "border-color": tone("--focus") } },
  { selector: "node[kind = 'process']", style: { "background-color": tint("--green", .13), "border-color": tone("--green") } },
  { selector: "node[kind = 'source']", style: { "background-color": tint("--muted", .09), "border-color": tone("--muted") } },
  { selector: "edge", style: {
    "curve-style": "bezier", "line-color": tone("--muted"), "target-arrow-color": tone("--muted"),
    "target-arrow-shape": "triangle", width: 1.25, opacity: .8, "arrow-scale": .7, "loop-direction": "-35deg",
  } },
  { selector: "edge[kind = 'precedes']", style: { "line-color": tone("--accent"), "target-arrow-color": tone("--accent"), width: 2 } },
  { selector: "edge[kind = 'measures']", style: { "line-color": tone("--focus"), "target-arrow-color": tone("--focus") } },
  { selector: "edge[kind = 'calculated_from']", style: { "line-color": tone("--green"), "target-arrow-color": tone("--green"), "line-style": "dotted" } },
  { selector: "edge[kind = 'part_of'], edge[kind = 'sourced_from']", style: { "line-style": "dashed", opacity: .47 } },
  { selector: "edge[kind = 'may_influence'], edge[kind = 'associated_with'], edge[kind = 'depends_on']", style: { "line-style": "dotted" } },
  { selector: "edge[kind = 'associated_with']", style: { "target-arrow-shape": "none" } },
  { selector: "node.path", style: { "border-color": tone("--accent"), "border-width": 2.5 } },
  { selector: "edge.path", style: { "line-color": tone("--accent"), "target-arrow-color": tone("--accent"), opacity: 1, width: 2.75, "z-index": 2 } },
  { selector: "node:selected", style: { "border-color": tone("--text"), "border-width": 2.5 } },
  { selector: ".hidden", style: { display: "none" } },
];

const cy = cytoscape({ container: byId("graph"), elements: [], style: graphStyles(), minZoom: .25, maxZoom: 2.5 });
let snapshot = { graph: { nodes: [], edges: [] }, anomalies: [], incidents: [] };
let nodes = new Map();
let graphKey = "";
let listKey = "";
let reportKey = "";
let anomalyKey = "";
let catalog = [];
let catalogKey = "";
let selectedIncident = null;
const incidentLink = Number(new URLSearchParams(location.search).get("incident"));
let requestedIncident = Number.isSafeInteger(incidentLink) && incidentLink > 0 ? incidentLink : null;
const stateURL = requestedIncident ? "/api/state?incident=" + requestedIncident : "/api/state";
let eventKey = null;
let selectedNode = null;
let selectedEdge = null;
let mode = "overview";

function graphPositions(graph) {
  const activities = graph.nodes.filter((node) => node.kind === "activity");
  const processes = graph.nodes.filter((node) => node.kind === "process");
  const primary = [...processes].sort((a, b) => {
    const size = (process) => graph.edges.filter((edge) => edge.kind === "part_of" && edge.target === process.id).length;
    return size(b) - size(a);
  })[0];
  const depth = new Map();
  const queue = (primary?.properties.start_activities || []).map((activity) => "activity:" + activity);
  for (const start of queue) depth.set(start, 0);
  for (let i = 0; i < queue.length; i++) {
    for (const edge of graph.edges) {
      if (edge.kind !== "precedes" || edge.source !== queue[i] || depth.has(edge.target)) continue;
      if (primary && !edge.properties.object_types?.[primary.properties.object_type]) continue;
      depth.set(edge.target, depth.get(edge.source) + 1);
      queue.push(edge.target);
    }
  }
  activities.sort((a, b) => (depth.get(a.id) ?? Infinity) - (depth.get(b.id) ?? Infinity) || a.label.localeCompare(b.label));
  const positions = new Map();
  const columns = 6;
  const place = (items, y) => items.forEach((node, i) => {
    const rowSize = Math.min(columns, items.length - Math.floor(i / columns) * columns);
    positions.set(node.id, { x: (i % columns - (rowSize - 1) / 2) * 138, y: y + Math.floor(i / columns) * 58 });
  });
  place(processes, 0);
  const activityTop = Math.ceil(processes.length / columns) * 58 + 15;
  place(activities, activityTop);
  const metrics = graph.nodes.filter((node) => node.kind === "metric");
  const measureX = (node) => graph.edges.find((edge) => edge.kind === "measures" && edge.source === node.id && positions.has(edge.target));
  metrics.sort((a, b) => (positions.get(measureX(a)?.target)?.x ?? Infinity) -
    (positions.get(measureX(b)?.target)?.x ?? Infinity) || a.label.localeCompare(b.label));
  const metricTop = activityTop + Math.ceil(activities.length / columns) * 58 + 25;
  place(metrics, metricTop);
  place(graph.nodes.filter((node) => node.kind === "source"), metricTop + Math.ceil(metrics.length / columns) * 58 + 15);
  return positions;
}

function syncGraph(graph) {
  nodes = new Map(graph.nodes.map((node) => [node.id, node]));
  const edgeID = (edge) => JSON.stringify([edge.source, edge.target, edge.kind, edge.provenance]);
  const ids = [...nodes.keys(), ...graph.edges.map(edgeID)];
  const key = JSON.stringify(ids);
  if (key === graphKey) return;
  const first = !cy.nodes().length;
  graphKey = key;
  const visibleIDs = new Set(ids);
  const positions = graphPositions(graph);
  const existingPositions = new Set(cy.nodes().map((node) => {
    const position = node.position();
    return position.x + "/" + position.y;
  }));
  cy.batch(() => {
    cy.elements().filter((item) => !visibleIDs.has(item.id())).remove();
    for (const node of graph.nodes) {
      if (cy.getElementById(node.id).length) continue;
      const position = { ...positions.get(node.id) };
      while (existingPositions.has(position.x + "/" + position.y)) position.y += 58;
      existingPositions.add(position.x + "/" + position.y);
      cy.add({ group: "nodes", data: node, position });
    }
    for (const edge of graph.edges) {
      const id = edgeID(edge);
      if (!cy.getElementById(id).length) cy.add({ group: "edges", data: { ...edge, id } });
    }
  });
  const picker = byId("node-picker");
  picker.replaceChildren(new Option("Inspect a node", ""));
  for (const node of [...nodes.values()].sort((a, b) => a.label.localeCompare(b.label))) picker.add(new Option(node.label, node.id));
  picker.value = selectedNode || "";
  byId("graph-empty").hidden = Boolean(graph.nodes.length);
  byId("graph-count").textContent = graph.nodes.length ? graph.nodes.length + " nodes · " + graph.edges.length + " relationships" : "";
  applyView();
  if (first && graph.nodes.length) fit();
}

function applyView() {
  const report = snapshot.incidents.find((item) => item.id === selectedIncident)?.report;
  const path = readPath(report);
  const pathNodes = new Set(path);
  cy.batch(() => {
    cy.nodes().forEach((node) => {
      const kind = node.data("kind");
      node.toggleClass("path", pathNodes.has(node.id()));
      node.toggleClass("hidden", mode === "workflow" ? !["activity", "process"].includes(kind) : mode !== "all" && kind === "source");
    });
    cy.edges().forEach((edge) => {
      const kind = edge.data("kind");
      const source = edge.data("source");
      const target = edge.data("target");
      const isPath = pathNodes.has(source) && pathNodes.has(target);
      edge.toggleClass("path", isPath);
      edge.toggleClass("hidden", mode === "overview" && !isPath && !["precedes", "measures", "calculated_from"].includes(kind));
    });
  });
}

function fit() {
  const shown = cy.elements(":visible");
  if (shown.length) cy.fit(shown, 34);
}

function changeMode(next, refit = true) {
  mode = next;
  for (const button of document.querySelectorAll(".mode")) {
    button.classList.toggle("is-active", button.dataset.mode === mode);
    button.setAttribute("aria-pressed", button.dataset.mode === mode);
  }
  applyView();
  if (refit) fit();
}

function readPath(report) {
  if (!Array.isArray(report?.path)) return [];
  const ids = report.path.flatMap((step) => {
    if (typeof step === "string") return [step];
    return step ? [step.node_id, step.id, step.node, step.source, step.target] : [];
  });
  return [...new Set(ids.filter((id) => nodes.has(id)))];
}

function name(id) {
  return nodes.get(id)?.label || words(id.replace(/^(metric|activity|process|source):/, ""));
}

function inspectNode(id, center = false) {
  if (center && byId("graph-view").hidden) {
    selectTab(byId("graph-tab"), false);
    cy.resize();
  }
  selectedNode = id || null;
  selectedEdge = null;
  const target = cy.getElementById(id);
  if (target.length && center && target.hidden()) changeMode("all", false);
  cy.nodes().unselect();
  if (target.length) {
    target.select();
    if (center) cy.center(target);
  }
  byId("node-picker").value = id;
  renderInspector();
}

function renderInspector() {
  const inspector = byId("inspector");
  const node = nodes.get(selectedNode);
  inspector.hidden = !(node || selectedEdge);
  if (selectedEdge) {
    const edge = selectedEdge;
    inspector.replaceChildren(element("h3", words(edge.kind)),
      element("p", name(edge.source) + " · " + name(edge.target)),
      element("p", "Provenance · " + edge.provenance, "panel-meta"));
    if (Object.keys(edge.properties || {}).length) inspector.append(element("pre", JSON.stringify(edge.properties, null, 2)));
    return;
  }
  if (!node) return;
  inspector.replaceChildren(element("h3", node.label), element("p", words(node.kind), "panel-meta"));
  if (node.properties.definition) inspector.append(element("p", node.properties.definition));
  if (node.properties.inductive_tree) {
    const details = element("details");
    details.append(element("summary", "Inductive Miner process tree"), element("pre", node.properties.inductive_tree));
    inspector.append(details);
  }
  const relations = snapshot.graph.edges.filter((edge) => edge.source === node.id || edge.target === node.id);
  if (!relations.length) return;
  const details = element("details");
  const list = element("ul");
  for (const edge of relations) {
    const other = edge.source === node.id ? edge.target : edge.source;
    const link = element("button", name(other), "anomaly-metric");
    link.type = "button";
    link.addEventListener("click", () => inspectNode(other, true));
    const line = element("li");
    line.append(link, document.createTextNode(" · " + words(edge.kind) + " · " + edge.provenance));
    list.append(line);
  }
  details.append(element("summary", relations.length + " relationships"), list);
  inspector.append(details);
}

function renderIncidents() {
  const ids = snapshot.incidents.map((item) => item.id);
  if (ids.includes(requestedIncident)) {
    selectedIncident = requestedIncident;
    requestedIncident = null;
  }
  if (!ids.includes(selectedIncident)) selectedIncident = ids[0] ?? null;
  const key = JSON.stringify([selectedIncident, snapshot.incidents.map((item) =>
    [item.id, item.metric_id, item.scope, item.recovered_at, item.investigation_pending])]);
  if (key !== listKey) {
    listKey = key;
    const list = byId("incident-list");
    list.replaceChildren();
    for (const incident of snapshot.incidents) {
      const current = incident.id === selectedIncident;
      const button = element("button", null, "incident-card" + (current ? " selected" : ""));
      button.type = "button";
      button.setAttribute("aria-pressed", current);
      const line = element("span", null, "incident-line");
      line.append(element("span", name("metric:" + incident.metric_id)), element("span",
        incident.recovered_at ? "Recovered" : "Open", "pill" + (incident.recovered_at ? " recovered" : "")));
      button.append(line, element("span", scope(incident.report?.affected_scope || incident.scope), "incident-scope"));
      if (incident.investigation_pending) button.append(element("span", "Investigation pending", "incident-scope"));
      button.addEventListener("click", () => {
        selectedIncident = incident.id;
        renderIncidents();
        applyView();
      });
      list.append(button);
    }
  }
  byId("incident-count").textContent = snapshot.incidents.length ? snapshot.incidents.length + " tracked" : "";
  const current = snapshot.incidents.find((item) => item.id === selectedIncident);
  const keyForReport = JSON.stringify(current || null);
  if (keyForReport !== reportKey) {
    reportKey = keyForReport;
    renderReport(current);
  }
}

function reportSection(container, label, content) {
  if (content == null || content === "" || (typeof content === "object" && !Object.keys(content).length)) return;
  const section = element("div", null, "report-section");
  section.append(element("h3", label));
  if (Array.isArray(content)) {
    const list = element("ul");
    for (const item of content) list.append(element("li", describe(item)));
    section.append(list);
  } else {
    section.append(element("p", describe(content)));
  }
  container.append(section);
}

function describe(value) {
  if (typeof value !== "object" || value === null) return String(value ?? "—");
  if (Array.isArray(value)) return value.map(describe).join(", ");
  return Object.entries(value).map(([key, item]) => words(key) + " · " + describe(item)).join(" · ");
}

function renderReport(incident) {
  const target = byId("report");
  const scroll = target.scrollTop;
  const incidentId = String(incident?.id ?? "");
  const wasOpen = target.dataset.incidentId === incidentId ? target.querySelector(".evidence-list")?.open : undefined;
  target.dataset.incidentId = incidentId;
  target.replaceChildren();
  if (!incident) {
    target.append(element("p", "Reports appear here when the monitor detects a change.", "placeholder"));
    return;
  }
  const report = incident.report || {};
  const heading = element("div", null, "report-heading");
  heading.append(element("h3", "Incident " + incident.id), element("span", date(incident.opened_at) + " UTC", "panel-meta"));
  target.append(heading, element("p", report.summary || "Collecting evidence.", ""));
  if (incident.investigation_pending) {
    const pending = element("p", "Investigation pending", "panel-meta");
    pending.setAttribute("role", "status");
    target.append(pending);
  }
  reportSection(target, "First observable break", report.first_observable_break);
  reportSection(target, "Likely causes", report.likely_causes);
  reportSection(target, "Business impact", report.impact);
  reportSection(target, "Uncertainty", report.uncertainty);
  const recovery = incident.recovered_at ? "Recovered " + date(incident.recovered_at) + " UTC."
    : "Tracking recovery · " + incident.healthy_windows + " healthy windows observed.";
  reportSection(target, "Recovery", report.recovery ? recovery + " " + describe(report.recovery) : recovery);
  const path = readPath(report);
  if (path.length) {
    const section = element("div", null, "report-section");
    const list = element("div", null, "path-list");
    section.append(element("h3", "Investigation path"), list);
    for (const id of path) {
      const button = element("button", name(id), "path-step");
      button.type = "button";
      button.addEventListener("click", () => inspectNode(id, true));
      list.append(button);
    }
    target.append(section);
  }
  const evidence = element("details", null, "report-section evidence-list");
  const body = element("div");
  evidence.append(element("summary", "Supporting evidence"), body);
  evidence.open = wasOpen ?? Boolean(incident.investigation_pending);
  evidence.addEventListener("toggle", () => {
    if (evidence.open && !body.children.length) loadEvidence(incident.id, body);
  });
  target.append(evidence);
  if (evidence.open) loadEvidence(incident.id, body);
  target.scrollTop = scroll;
}

async function loadEvidence(id, target) {
  target.replaceChildren(element("p", "Loading evidence…", "placeholder"));
  try {
    const response = await fetch("/api/incidents/" + id + "/evidence", { cache: "no-store" });
    if (!response.ok) throw new Error(response.status);
    const rows = await response.json();
    target.replaceChildren();
    for (const row of rows) {
      const details = element("details");
      const title = [words(row.kind), row.node_id && name(row.node_id), date(row.window_start) + " UTC"].filter(Boolean).join(" · ");
      details.append(element("summary", title), element("pre", JSON.stringify(row.payload, null, 2)));
      target.append(details);
    }
    if (!rows.length) target.append(element("p", "Evidence is being collected.", "placeholder"));
  } catch {
    target.append(element("p", "Evidence is temporarily unavailable.", "placeholder"));
  }
}

function renderAnomalies() {
  const key = JSON.stringify(snapshot.anomalies);
  if (key === anomalyKey) return;
  anomalyKey = key;
  byId("anomaly-count").textContent = snapshot.anomalies.length + " signals";
  const body = byId("anomaly-rows");
  body.replaceChildren();
  for (const row of snapshot.anomalies.slice(0, 20)) {
    const id = "metric:" + row.metric_id;
    const button = element("button", name(id), "anomaly-metric");
    button.type = "button";
    button.addEventListener("click", () => inspectNode(id, true));
    const metric = element("td");
    metric.append(button, element("small", scope(row.scope)),
      element("small", date(row.window_start) + " UTC · score " + number(row.score)));
    const tr = element("tr");
    tr.append(metric, element("td", number(row.observed)), element("td", number(row.expected)));
    body.append(tr);
  }
  if (!snapshot.anomalies.length) {
    const row = element("tr");
    const cell = element("td", "No anomalies observed", "placeholder");
    cell.colSpan = 3;
    row.append(cell);
    body.append(row);
  }
}

function renderCatalog() {
  const filter = byId("catalog-filter").value.trim().toLowerCase();
  const key = JSON.stringify([catalog, filter]);
  if (key === catalogKey) return;
  catalogKey = key;
  const shown = catalog.filter((metric) => [metric.id, metric.name, metric.access.source]
    .join(" ").toLowerCase().includes(filter));
  const count = catalog.filter((metric) => metric.readings_available).length;
  byId("catalog-count").textContent = catalog.length ? count + " of " + catalog.length + " with readings" : "Loading catalog";
  const rows = byId("catalog-rows");
  rows.replaceChildren();
  for (const metric of shown) {
    const tr = element("tr");
    const title = element("th");
    title.scope = "row";
    if (metric.registered) {
      const link = element("a", metric.name);
      link.href = "/api/catalog/" + encodeURIComponent(metric.id) + "/readings";
      title.append(link);
    } else title.append(element("span", metric.name));
    title.append(element("small", metric.definition));
    const dims = element("td", metric.access.unit);
    dims.append(element("small", metric.dimensions.map(words).join(" · ")));
    const reading = element("td", metric.readings_available ? "Available" :
      metric.registered ? "Unavailable" : "Awaiting registration");
    if (metric.readings_available) {
      const value = metric.access.unit === "ratio"
        ? new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 2 }).format(metric.latest.value)
        : new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(metric.latest.value);
      reading.append(element("small", value + " · " + date(metric.latest.window_start) + " UTC"));
    } else if (metric.latest?.details?.quality) reading.append(element("small", words(metric.latest.details.quality)));
    tr.append(title, element("td", metric.access.source, "catalog-source"), dims, reading,
      element("td", metric.alerts_enabled ? "Enabled" : "Disabled"));
    rows.append(tr);
  }
  if (!shown.length) {
    const tr = element("tr");
    const td = element("td", catalog.length ? "No matching metrics." : "Waiting for catalog.", "placeholder");
    td.colSpan = 5;
    tr.append(td);
    rows.append(tr);
  }
}

async function refreshCatalog() {
  try {
    const response = await fetch("/api/catalog", { cache: "no-store" });
    if (response.ok) {
      catalog = await response.json();
      renderCatalog();
    } else if (!catalog.length) {
      byId("catalog-count").textContent = "Catalog temporarily unavailable";
    }
  } catch {
    if (!catalog.length) byId("catalog-count").textContent = "Catalog temporarily unavailable";
  } finally {
    setTimeout(refreshCatalog, 3000);
  }
}

async function refresh() {
  try {
    const response = await fetch(stateURL, { cache: "no-store" });
    if (!response.ok) throw new Error(response.status);
    snapshot = await response.json();
    byId("status-dot").classList.add("connected");
    byId("status").textContent = snapshot.last_window ? "Monitoring" : "Waiting for baseline";
    byId("window").textContent = snapshot.last_window ? date(snapshot.last_window) + " UTC" : "—";
    syncGraph(snapshot.graph);
    renderIncidents();
    renderAnomalies();
    applyView();
  } catch (error) {
    byId("status-dot").classList.remove("connected");
    byId("status").textContent = "Reconnecting";
    console.error(error);
  } finally {
    setTimeout(refresh, 1500);
  }
}

async function refreshEvents() {
  const target = byId("event-rows");
  try {
    const response = await fetch("/api/events", { cache: "no-store" });
    if (!response.ok) throw new Error(response.status);
    const rows = await response.json();
    const key = rows.map((row) => row.id).join("|");
    if (key === eventKey) return;
    eventKey = key;
    const expanded = new Set([...target.querySelectorAll("details[open]")].map((row) => row.dataset.id));
    target.replaceChildren();
    for (const row of rows) {
      const entry = element("details", null, "event-entry");
      entry.dataset.id = row.id;
      entry.open = expanded.has(row.id);
      const summary = element("summary");
      const stamp = element("time", date(row.occurred_at) + " UTC");
      stamp.dateTime = row.occurred_at;
      summary.append(element("strong", words(row.activity)), stamp);
      entry.append(summary, element("pre", JSON.stringify({ id: row.id, ingested_at: row.ingested_at, ...row.attrs }, null, 2)));
      target.append(entry);
    }
    if (!rows.length) target.append(element("p", "Waiting for observed events.", "placeholder"));
  } catch {
    if (eventKey === null) target.replaceChildren(element("p", "Event log is reconnecting.", "placeholder"));
  } finally {
    setTimeout(refreshEvents, 3000);
  }
}

const graphPanel = byId("graph-panel");
const fullscreenButton = byId("graph-fullscreen");
function syncFullscreen() {
  const expanded = document.fullscreenElement === graphPanel || graphPanel.classList.contains("graph-expanded");
  document.body.classList.toggle("graph-fullscreen", expanded);
  fullscreenButton.textContent = expanded ? "Exit fullscreen" : "Fullscreen";
  fullscreenButton.setAttribute("aria-pressed", expanded);
  requestAnimationFrame(() => { cy.resize(); fit(); });
}
fullscreenButton.addEventListener("click", async () => {
  if (document.fullscreenElement === graphPanel) {
    await document.exitFullscreen();
    return;
  }
  if (graphPanel.classList.contains("graph-expanded")) {
    graphPanel.classList.remove("graph-expanded");
  } else {
    try {
      if (!graphPanel.requestFullscreen) throw new Error();
      await graphPanel.requestFullscreen();
    } catch {
      graphPanel.classList.add("graph-expanded");
    }
  }
  syncFullscreen();
});
document.addEventListener("fullscreenchange", syncFullscreen);
new ResizeObserver(() => cy.resize()).observe(byId("graph"));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => cy.style(graphStyles()));

const modeToggle = byId("mode-toggle");
function showModes(open) {
  byId("mode-menu").classList.toggle("open", open);
  modeToggle.setAttribute("aria-expanded", open);
  byId("graph-modes").inert = !open;
}
modeToggle.addEventListener("click", () => showModes(modeToggle.getAttribute("aria-expanded") !== "true"));
for (const button of document.querySelectorAll(".mode")) button.addEventListener("click", () => {
  changeMode(button.dataset.mode);
  showModes(false);
  modeToggle.focus();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (modeToggle.getAttribute("aria-expanded") === "true") {
    showModes(false);
    modeToggle.focus();
  }
  if (graphPanel.classList.contains("graph-expanded")) {
    graphPanel.classList.remove("graph-expanded");
    syncFullscreen();
  }
});

const tabs = [byId("graph-tab"), byId("table-tab")];
function selectTab(tab, refit = true) {
  for (const item of tabs) {
    const active = item === tab;
    item.setAttribute("aria-selected", active);
    item.tabIndex = active ? 0 : -1;
    byId(item.getAttribute("aria-controls")).hidden = !active;
  }
  if (tab === tabs[0] && refit) requestAnimationFrame(() => { cy.resize(); fit(); });
}
for (const [index, tab] of tabs.entries()) {
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = tabs[event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index];
    selectTab(next);
    next.focus();
  });
}
for (const [name, id] of [["events", "event-panel"], ["signals", "signals-panel"]]) {
  const panel = byId(id);
  const hide = byId("hide-" + name);
  const show = byId("show-" + name);
  function collapse(closed) {
    panel.hidden = closed;
    show.hidden = !closed;
    byId("workspace").classList.toggle(name + "-collapsed", closed);
    (closed ? show : hide).focus();
  }
  hide.addEventListener("click", () => collapse(true));
  show.addEventListener("click", () => collapse(false));
}

byId("node-picker").addEventListener("change", (event) => inspectNode(event.target.value, true));
byId("fit").addEventListener("click", fit);
function zoomBy(factor) {
  cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
}
byId("zoom-in").addEventListener("click", () => zoomBy(1.2));
byId("zoom-out").addEventListener("click", () => zoomBy(1 / 1.2));
cy.on("tap", "node", (event) => inspectNode(event.target.id()));
cy.on("tap", "edge", (event) => {
  selectedEdge = snapshot.graph.edges.find((edge) =>
    edge.source === event.target.data("source") && edge.target === event.target.data("target")
    && edge.kind === event.target.data("kind") && edge.provenance === event.target.data("provenance"));
  selectedNode = null;
  byId("node-picker").value = "";
  renderInspector();
});
byId("catalog-filter").addEventListener("input", renderCatalog);
refreshCatalog();
refreshEvents();
refresh();
