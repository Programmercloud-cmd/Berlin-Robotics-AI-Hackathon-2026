const byId = (id) => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat("en-GB", {
  month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "UTC",
});
const numberFormat = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 3 });
const date = (value) => value && Number.isFinite(Date.parse(value)) ? dateFormat.format(new Date(value)) : "—";
const alertWindowEnd = (start, end) => end || (Number.isFinite(Date.parse(start))
  ? new Date(Date.parse(start) + 3600000).toISOString() : null);
const alertWindow = (start, end) => date(start) + " – " + date(alertWindowEnd(start, end)) + " UTC";
const incidentOpenedAt = (incident) => incident.report?.observation_window?.observed_at || incident.opened_at;
const number = (value) => value == null ? "—" : numberFormat.format(value);
const percent = new Intl.NumberFormat("en-GB", { style: "percent", maximumFractionDigits: 1 });
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
  { selector: "node.journey-node", style: { width: 116, height: 59, "text-max-width": 110, "font-size": 18 } },
  { selector: "node.unrelated", style: { opacity: .79, "background-opacity": .34, "border-opacity": .42 } },
  { selector: "edge.unrelated", style: { opacity: .2 } },
  { selector: "node.path", style: {
    "border-color": tone("--incident"), "border-width": 3, "background-color": tint("--incident", .13),
  } },
  { selector: "edge.path", style: {
    "line-color": tone("--incident"), "target-arrow-color": tone("--incident"), opacity: 1, width: 3.7, "z-index": 2,
  } },
  { selector: "node.inspected", style: {
    "border-color": "#fff", "border-width": 1.5, "underlay-color": tone("--muted"),
    "underlay-opacity": .7, "underlay-padding": 1.3,
  } },
  { selector: "node.inspection-root", style: { "border-width": 2.5 } },
  { selector: "edge.inspected", style: {
    "line-color": "#fff", "target-arrow-color": "#fff", width: 1.6, opacity: 1, "z-index": 3,
    "underlay-color": tone("--muted"), "underlay-opacity": .7, "underlay-padding": 1.3,
  } },
  { selector: "node.path.inspected", style: { "underlay-color": tone("--incident") } },
  { selector: ".hidden", style: { display: "none" } },
];

const cy = cytoscape({ container: byId("graph"), elements: [], style: graphStyles(), minZoom: .25, maxZoom: 2.5, autounselectify: true });
let snapshot = { graph: { nodes: [], edges: [] }, incidents: [] };
let nodes = new Map();
let graphKey = "";
let catalog = [];
let catalogKey = "";
let selectedIncident = null;
const incidentRows = new Map();
let graphEvidenceIncident = null;
let graphEvidenceKey = "";
let graphEvidence = [];
const evidenceRequests = new Map();
const incidentLink = Number(new URLSearchParams(location.search).get("incident"));
let requestedIncident = Number.isSafeInteger(incidentLink) && incidentLink > 0 ? incidentLink : null;
const stateURL = requestedIncident ? "/api/state?incident=" + requestedIncident : "/api/state";
let eventKey = null;
let selectedNode = null;
let selectedEdge = null;
let mode = "overview";
let layoutKey = "";
let fullPositions = new Map();
let journeyView = journeyLayout(snapshot.graph);
let journeyKey = "";
let focusKey = "";
let incidentFocus = incidentGraphFocus(snapshot.graph, null, []);

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
  const ids = [...nodes.keys(), ...graph.edges.map(edgeID)];
  const key = JSON.stringify(ids);
  if (key === graphKey) return;
  const first = !cy.nodes().length;
  graphKey = key;
  const visibleIDs = new Set(ids);
  const positions = fullPositions = graphPositions(graph);
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
  if (selectedNode && !nodes.has(selectedNode)) selectedNode = null;
  if (!byId("node-results").hidden) showNodeResults();
  byId("graph-empty").hidden = Boolean(graph.nodes.length);
  applyView();
  if (first && graph.nodes.length) fit();
}

function requestIncidentEvidence(id, alertsOnly = false) {
  const url = "/api/incidents/" + id + "/evidence" + (alertsOnly ? "?alerts_only=true" : "");
  if (!evidenceRequests.has(url)) {
    const request = fetch(url, { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(response.status);
        return response.json();
      })
      .finally(() => evidenceRequests.delete(url));
    evidenceRequests.set(url, request);
  }
  return evidenceRequests.get(url);
}

function refreshGraphEvidence(incident) {
  const key = incident ? JSON.stringify([incident.id, incident.updated_at, incident.investigation_pending,
    incident.report?.evidence_branches?.map((branch) => branch.evidence_id)]) : "";
  if (key === graphEvidenceKey) return;
  graphEvidenceKey = key;
  if ((incident?.id ?? null) !== graphEvidenceIncident) {
    graphEvidenceIncident = incident?.id ?? null;
    graphEvidence = [];
    focusKey = "";
  }
  if (incident) requestIncidentEvidence(incident.id).then((rows) => {
    if (graphEvidenceKey !== key) return;
    graphEvidence = rows;
    focusKey = "";
    applyView();
  }).catch(() => {
    if (graphEvidenceKey === key) graphEvidenceKey = "";
  });
}

function updateJourneyLabels() {
  const target = byId("journey-lanes");
  target.hidden = mode !== "journey" || !journeyView.lanes.length;
  if (target.hidden) return;
  const signature = JSON.stringify(journeyView.lanes);
  if (target.dataset.signature !== signature) {
    target.dataset.signature = signature;
    target.replaceChildren(...journeyView.lanes.map((lane) => element("span", lane.label)));
  }
  const { x, y } = cy.pan();
  const zoom = cy.zoom();
  journeyView.lanes.forEach((lane, index) => {
    const label = target.children[index];
    label.style.left = Math.max(8, x + ((lane.x ?? 0) - 57) * zoom) + "px";
    label.style.top = y + (lane.y - 41) * zoom + "px";
  });
}

function applyView() {
  const incident = snapshot.incidents.find((item) => item.id === selectedIncident);
  const report = incident?.report;
  const completion = report?.break;
  refreshGraphEvidence(incident);
  const selectedKey = JSON.stringify([graphKey, incident?.id, completion]);
  if (selectedKey !== focusKey) {
    focusKey = selectedKey;
    incidentFocus = incidentGraphFocus(snapshot.graph, incident, graphEvidence);
    renderCatalog();
  }
  const focus = incidentFocus;
  const nodeRelationships = cy.getElementById(selectedNode || "").connectedEdges();
  const inspectedEdges = incident ? cy.collection() : nodeRelationships;
  const inspectedNodes = new Set(selectedNode ? [selectedNode, ...inspectedEdges.connectedNodes().map((node) => node.id())] : []);
  const supporting = new Set([...focus.supporting, ...inspectedNodes]);
  const journeySignature = JSON.stringify([graphKey, [...focus.supporting], [...inspectedNodes]]);
  if (journeySignature !== journeyKey) {
    journeyKey = journeySignature;
    journeyView = journeyLayout(snapshot.graph, supporting);
    const extraY = Math.max(0, ...[...journeyView.positions.values()].map(({ y }) => y)) + 82;
    [...inspectedNodes].filter((id) => !journeyView.positions.has(id)).forEach((id, i) => {
      journeyView.positions.set(id, { x: i % 6 * 142, y: extraY + Math.floor(i / 6) * 70 });
    });
  }
  const main = journey.filter((id) => nodes.has(id));
  const stages = new Set(main);
  const originalExtras = new Set([...focus.supporting, ...(completion?.activities || []).map(activityID)]);
  const overviewExtra = [...new Set([...originalExtras, ...focus.upstream, ...inspectedNodes])]
    .filter((id) => !stages.has(id) && nodes.has(id));
  const overviewVisible = new Set([...stages, ...overviewExtra]);
  const pairs = new Set(main.slice(1).map((id, index) => main[index] + "|" + id));
  const overviewMode = mode === "overview";
  const journeyMode = mode === "journey";
  const extra = overviewMode ? overviewExtra : journeyMode ? journeySignature : mode === "workflow" ? [...supporting] : null;
  const key = JSON.stringify([graphKey, mode, extra]);
  const reflow = key !== layoutKey;
  layoutKey = key;
  const overview = new Map();
  if (overviewMode && reflow) {
    main.forEach((id, i) => {
      const column = Math.floor(i / 3);
      const row = i % 3;
      overview.set(id, { x: column * 166, y: (column % 2 ? 2 - row : row) * 72 });
    });
    overviewExtra.forEach((id, i) => {
      overview.set(id, { x: i % 3 * 166, y: 240 + Math.floor(i / 3) * 58 });
    });
  }
  const visibleNodes = new Set();
  let visibleEdges = 0;
  cy.batch(() => {
    cy.nodes().forEach((node) => {
      const id = node.id();
      const kind = node.data("kind");
      const inspected = inspectedNodes.has(id);
      const isPath = focus.nodes.has(id);
      const visible = inspected || mode === "all" || (mode === "workflow" ? ["activity", "process"].includes(kind) || focus.supporting.has(id) :
        journeyMode ? journeyView.positions.has(id) : overviewVisible.has(id));
      if (visible) visibleNodes.add(id);
      node.toggleClass("hidden", !visible);
      node.toggleClass("journey-node", journeyMode);
      node.toggleClass("path", isPath);
      node.toggleClass("inspected", inspected);
      node.toggleClass("inspection-root", selectedNode === id);
      node.toggleClass("unrelated", Boolean(incident || selectedNode) && !isPath && !inspected);
      if (reflow && visible) node.position((overviewMode ? overview : journeyMode ? journeyView.positions : fullPositions).get(id));
    });
    cy.edges().forEach((edge) => {
      const { kind, source, target } = edge.data();
      const endpointsVisible = visibleNodes.has(source) && visibleNodes.has(target);
      const isPath = focus.connections.has(edge.id());
      const inspected = inspectedEdges.contains(edge);
      const journeyEdge = journeyView.connections.has(edge.id()) || (isPath && kind === "precedes");
      const overviewEdge = (kind === "precedes" && pairs.has(source + "|" + target)) ||
        (kind === "measures" && originalExtras.has(source) && (stages.has(target) || overviewVisible.has(target)));
      const visible = endpointsVisible && (inspected || mode === "all" || (journeyMode ? journeyEdge :
        isPath || (overviewMode ? overviewEdge : ["precedes", "part_of"].includes(kind))));
      if (visible) visibleEdges += 1;
      edge.toggleClass("hidden", !visible);
      edge.toggleClass("path", isPath);
      edge.toggleClass("inspected", inspected);
      edge.toggleClass("unrelated", Boolean(incident || selectedNode) && !isPath && !inspected);
    });
  });
  const context = byId("graph-incident");
  context.hidden = !incident;
  const scopeText = incident ?
    "Incident " + incident.id + " · Scope " + words(scope(report?.affected_scope || incident.scope)) : "";
  const scopeLabel = byId("graph-incident-scope");
  if (scopeLabel.textContent !== scopeText) scopeLabel.textContent = scopeText;
  const count = visibleNodes.size + " nodes · " + visibleEdges + " relationships";
  byId("graph-count").textContent = !nodes.size ? "" :
    overviewMode ? main.length + " observed workflow steps" : journeyMode ? "Journey · " + count : count;
  renderInspector(nodeRelationships.length);
  updateJourneyLabels();
  if (reflow && (overviewMode || journeyMode)) requestAnimationFrame(fit);
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
  applyView();
  if (center && id) fit();
}

function renderInspector(relationships) {
  const inspector = byId("inspector");
  const properties = byId("graph-edge-properties");
  properties.hidden = !Object.keys(selectedEdge?.properties || {}).length;
  if (!properties.hidden) {
    const json = JSON.stringify(selectedEdge.properties, null, 2);
    if (properties.textContent !== json) properties.textContent = json;
  }
  const node = nodes.get(selectedNode);
  inspector.hidden = !(node || selectedEdge);
  if (inspector.hidden) return;
  const title = node?.label || words(selectedEdge.kind);
  const detail = node ? words(node.kind) + " · " + relationships + (relationships === 1 ? " relationship" : " relationships") :
    name(selectedEdge.source) + " → " + name(selectedEdge.target) + " · " + selectedEdge.provenance;
  if (inspector.firstElementChild?.textContent !== title || inspector.lastElementChild?.textContent !== detail) {
    inspector.replaceChildren(element("strong", title), element("p", detail, "panel-meta"));
  }
}

function selectIncident(id) {
  selectedIncident = id;
  renderIncidents();
  updateActivitySelection();
  applyView();
}

byId("clear-incident").addEventListener("click", () => {
  selectIncident(null);
  byId("mode-toggle").focus();
});

function compareReading({ observed, expected }, unit) {
  const format = unit === "ratio" ? (value) => value == null ? "—" : percent.format(value) : number;
  return format(observed) + " vs " + format(expected) + " expected";
}

function createIncidentRow(id) {
  const entry = element("div", null, "incident-entry");
  const button = element("button", null, "incident-card");
  button.type = "button";
  button.id = "incident-" + id;
  const report = element("div", null, "report");
  report.id = "report-" + id;
  report.setAttribute("role", "region");
  report.setAttribute("aria-labelledby", button.id);
  button.setAttribute("aria-controls", report.id);
  button.addEventListener("click", () => selectIncident(selectedIncident === id ? null : id));
  entry.append(button, report);
  return { entry, button, report };
}

function renderIncidentRow(row, incident) {
  const completion = incident.report?.break;
  const measurement = completion || incident.report?.measurement;
  const step = incidentStep(incident);
  const affected = scope(incident.report?.affected_scope || incident.scope) + " · " + date(incidentOpenedAt(incident)) + " UTC";
  const recovery = incidentRecoveryStatus(incident);
  const comparison = measurement ? compareReading(measurement, completion ? "ratio" : measurement.unit) : "";
  const summary = [step, affected, recovery, comparison, incident.investigation_pending];
  const key = JSON.stringify(summary);
  if (key !== row.summaryKey) {
    row.summaryKey = key;
    const line = element("span", null, "incident-line");
    const chevron = element("span", null, "incident-chevron");
    chevron.setAttribute("aria-hidden", "true");
    line.append(element("span", step, "incident-step"),
      element("span", recovery, "pill" + (incident.recovered_at ? " recovered" : "")), chevron);
    row.button.replaceChildren(line, element("span", affected, "incident-scope"));
    if (comparison) row.button.append(element("span", comparison, "incident-comparison"));
    if (incident.investigation_pending) row.button.append(element("span", "Investigation pending", "incident-scope"));
  }
  const open = selectedIncident === incident.id;
  row.button.setAttribute("aria-expanded", open);
  row.report.hidden = !open;
  if (open) {
    const key = JSON.stringify(incident);
    if (key !== row.reportKey) {
      row.reportKey = key;
      renderReport(row.report, incident);
    }
  }
}

function incidentStep(incident) {
  return incident.report?.break?.label || name(metricID(incident.metric_id));
}

function incidentRecoveryStatus(incident) {
  return incident.recovered_at ? "Recovered" : incident.healthy_windows ? "Recovering" : "Open";
}

function renderIncidents() {
  const { incidents } = snapshot;
  const ids = new Set(incidents.map((item) => item.id));
  if (ids.has(requestedIncident)) {
    selectedIncident = requestedIncident;
    requestedIncident = null;
  }
  if (!ids.has(selectedIncident)) {
    selectedIncident = incidents.find((item) => item.grouping_decisions?.some((decision) =>
      decision.incident_id === selectedIncident && decision.status === "linked"))?.id ?? null;
  }
  const list = byId("incident-list");
  for (const [id, row] of incidentRows) {
    if (ids.has(id)) continue;
    row.entry.remove();
    incidentRows.delete(id);
  }
  for (const [index, incident] of incidents.entries()) {
    const row = incidentRows.get(incident.id) || createIncidentRow(incident.id);
    incidentRows.set(incident.id, row);
    renderIncidentRow(row, incident);
    if (list.children[index] !== row.entry) list.insertBefore(row.entry, list.children[index] || null);
  }
  byId("incident-count").textContent = incidents.length ? incidents.length + " tracked" : "";
  byId("incident-empty").hidden = incidents.length > 0;
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
  if (typeof value !== "object" || value === null) return String(value ?? "—").replace(/\b\d+:event-\d+\b/g, "[event in evidence]");
  if (Array.isArray(value)) return value.map(describe).join(", ");
  return Object.entries(value).map(([key, item]) => words(key) + " · " + describe(item)).join(" · ");
}

function renderEvidenceBranches(target, report, openBranches) {
  const branches = report.evidence_branches || [];
  const pending = report.deep_investigation === "pending";
  if (!pending && !branches.length) return;
  const section = element("div", null, "report-section evidence-branches");
  section.append(element("h3", "Evidence branches"));
  if (pending || report.branch_waits_for_receipts) {
    const notice = pending ? "Checking upstream evidence…" : "Watching for late receipts…";
    const status = element("p", notice, "panel-meta");
    status.setAttribute("role", "status");
    section.append(status);
  }
  const categories = [["supports", "Supports"], ["weakens", "Weakens"], ["unresolved", "Unresolved"]];
  for (const [index, branch] of branches.entries()) {
    const details = element("details", null, "evidence-branch");
    details.dataset.title = branch.title;
    details.open = openBranches.get(branch.title) ?? index === 0;
    details.append(element("summary", describe(branch.title)));
    const findings = element("dl", null, "branch-findings");
    for (const [key, label] of categories) {
      if (!branch[key]?.length) continue;
      const heading = element("dt", label, "branch-" + key);
      const body = element("dd");
      const list = element("ul");
      for (const finding of branch[key]) list.append(element("li", describe(finding)));
      body.append(list);
      findings.append(heading, body);
    }
    details.append(findings);
    section.append(details);
  }
  target.append(section);
}

function renderReport(target, incident) {
  const previousEvidence = target.querySelector(".evidence-list");
  const previousAlerts = target.querySelector(".related-alerts");
  const openBranches = new Map(Array.from(target.querySelectorAll(".evidence-branch"),
    (branch) => [branch.dataset.title, branch.open]));
  const focused = target.contains(document.activeElement) ? document.activeElement : null;
  target.replaceChildren();
  const report = incident.report || {};
  const heading = element("div", null, "report-heading");
  heading.append(element("h3", "Incident " + incident.id), element("span", date(incidentOpenedAt(incident)) + " UTC", "panel-meta"));
  target.append(heading, element("p", describe(report.summary || "Collecting evidence.")));
  if (incident.investigation_pending) {
    const pending = element("p", "Investigation pending", "panel-meta");
    pending.setAttribute("role", "status");
    target.append(pending);
  }
  reportSection(target, "First observable break", report.first_observable_break);
  reportSection(target, "Likely causes", report.likely_causes);
  reportSection(target, "Business impact", report.impact);
  reportSection(target, "Uncertainty", report.uncertainty);
  if (report.uncertain_relationships?.length) {
    reportSection(target, "Uncertain downstream relationships",
      "Too little evidence to attribute these changes: " + report.uncertain_relationships.join(", ") + ".");
  }
  const recovery = incident.recovered_at ? "Recovered " + date(incident.recovery_observed_at || report.recovery_observed_at || incident.recovered_at) + " UTC."
    : "Tracking recovery · " + incident.healthy_windows + " healthy evaluations observed.";
  reportSection(target, "Recovery", report.recovery ? recovery + " " + describe(report.recovery) : recovery);
  if (incident.grouping_decisions?.length) {
    const section = element("div", null, "report-section");
    section.append(element("h3", "Incident grouping"));
    for (const decision of incident.grouping_decisions) {
      const other = decision.incident_id === incident.id ? decision.parent_id : decision.incident_id;
      const row = element("p");
      const link = element("a", "Incident " + other);
      link.href = "?incident=" + other;
      row.append(link, document.createTextNode(` · ${words(decision.status)}. ${decision.reason}`));
      section.append(row);
    }
    target.append(section);
  }
  renderEvidenceBranches(target, report, openBranches);
  const path = report.break?.metrics?.map((id) => "metric:" + id) || [];
  if (path.length) {
    const section = element("div", null, "report-section");
    const list = element("div", null, "path-list");
    section.append(element("h3", "Supporting metrics"), list);
    for (const id of path) {
      const button = element("button", name(id), "path-step");
      button.type = "button";
      button.addEventListener("click", () => inspectNode(id, true));
      list.append(button);
    }
    target.append(section);
  }
  reportDisclosure(target, previousAlerts, "Related alerts", "related-alerts", (body) => loadRelatedAlerts(incident.id, body));
  reportDisclosure(target, previousEvidence, "View evidence", "evidence-list", (body) => loadEvidence(incident.id, body));
  if (focused?.isConnected) focused.focus({ preventScroll: true });
}

function reportDisclosure(target, previous, title, className, load) {
  const details = previous || element("details", null, "report-section " + className);
  if (!previous) {
    const body = element("div");
    details.append(element("summary", title), body);
    details.addEventListener("toggle", () => {
      if (details.open) load(body);
    });
  }
  target.append(details);
  if (details.open) load(details.lastElementChild);
}

async function loadEvidence(id, target) {
  if (target.dataset.loading) {
    target.dataset.refresh = "true";
    return;
  }
  target.dataset.loading = "true";
  if (!target.children.length) target.append(element("p", "Loading evidence…", "placeholder"));
  try {
    const rows = await requestIncidentEvidence(id);
    const previous = Array.from(target.querySelectorAll("details[data-evidence-id]"));
    const byRowId = new Map(previous.map((details) => [details.dataset.evidenceId, details]));
    const entries = rows.map((row) => {
      const id = String(row.id);
      if (byRowId.has(id)) return byRowId.get(id);
      const details = element("details");
      details.dataset.evidenceId = id;
      const title = [words(row.kind), row.payload?.title || (row.node_id && name(row.node_id)),
        date(row.window_start) + " UTC"].filter(Boolean).join(" · ");
      details.append(element("summary", title), element("pre", JSON.stringify(row.payload, null, 2)));
      return details;
    });
    if (!entries.length) target.replaceChildren(element("p", "Evidence is being collected.", "placeholder"));
    else if (entries.length !== previous.length || entries.some((entry, index) => entry !== previous[index])) {
      target.replaceChildren(...entries);
    }
  } catch {
    if (!target.querySelector("details")) target.replaceChildren(element("p", "Evidence is temporarily unavailable.", "placeholder"));
  } finally {
    delete target.dataset.loading;
    if (target.dataset.refresh) {
      delete target.dataset.refresh;
      if (target.isConnected) loadEvidence(id, target);
    }
  }
}

async function loadRelatedAlerts(id, target) {
  if (!target.children.length) target.append(element("p", "Loading alerts…", "placeholder"));
  try {
    const incident = snapshot.incidents.find((item) => item.id === id);
    const alerts = await readIncidentAlerts(incident);
    const key = alerts.map((alert) => alert.id).join(",") || "empty";
    if (key === target.dataset.alerts) return;
    target.dataset.alerts = key;
    const summary = target.previousElementSibling;
    summary.textContent = alerts.length ? `Related alerts (${alerts.length})` : "Related alerts";
    if (!alerts.length) {
      target.replaceChildren(element("p", "No individual alerts recorded for this investigation.", "placeholder"));
      return;
    }
    const list = element("ul");
    for (const alert of alerts) {
      const metric = metricID(alert.metric_id);
      const button = element("button", name(metric), "anomaly-metric");
      button.type = "button";
      button.addEventListener("click", () => inspectNode(metric, true));
      const unit = catalog.find((item) => item.id === alert.metric_id)?.access.unit;
      const relationship = alert.kind === "anomaly" ? "Initial alert" : alert.relationship;
      const score = alert.score == null ? null : "score " + number(alert.score);
      const context = [alertWindow(alert.window_start, alert.window_end), score, relationship].filter(Boolean).join(" · ");
      const item = element("li");
      item.append(button, element("span", scope(alert.scope)),
        element("span", compareReading(alert, unit), "incident-comparison"), element("span", context));
      list.append(item);
    }
    target.replaceChildren(list);
  } catch {
    if (!target.dataset.alerts) target.replaceChildren(element("p", "Alerts are temporarily unavailable.", "placeholder"));
  }
}

function renderCatalog() {
  const filter = byId("catalog-filter").value.trim().toLowerCase();
  const primary = snapshot.incidents.find((item) => item.id === selectedIncident)?.metric_id;
  const relevant = (metric) => metric.id === primary || incidentFocus.nodes.has(metricID(metric.id));
  const priority = (metric) => metric.id === primary ? 0 : relevant(metric) ? 1 : 2;
  const key = JSON.stringify([catalog, filter, primary, catalog.filter(relevant).map((metric) => metric.id)]);
  if (key === catalogKey) return;
  catalogKey = key;
  const shown = catalog.filter((metric) => [metric.id, metric.name, metric.access.source]
    .join(" ").toLowerCase().includes(filter)).sort((a, b) => priority(a) - priority(b));
  const count = catalog.filter((metric) => metric.readings_available).length;
  byId("catalog-count").textContent = catalog.length ? count + " of " + catalog.length + " with readings" : "Loading catalog";
  const rows = byId("catalog-rows");
  rows.replaceChildren();
  for (const metric of shown) {
    const tr = element("tr");
    tr.dataset.metricId = metric.id;
    tr.classList.toggle("incident-metric", relevant(metric));
    const title = element("th");
    title.scope = "row";
    if (metric.registered) {
      const link = element("a", metric.name);
      link.href = "/api/catalog/" + encodeURIComponent(metric.id) + "/readings";
      title.append(link);
    } else title.append(element("span", metric.name));
    if (relevant(metric)) title.append(element("span", " · Selected incident metric", "visually-hidden"));
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
    refreshActivityGraph();
    updateActivitySelection();
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
const graphLayoutObserver = new ResizeObserver(() => cy.resize());
// The header and incident banner can move the canvas without resizing it.
for (const region of [byId("graph"), graphPanel.querySelector(".company-header"), byId("graph-incident")]) {
  graphLayoutObserver.observe(region);
}
new MutationObserver(() => cy.style(graphStyles()))
  .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

cy.on("pan zoom resize", updateJourneyLabels);

const modeToggle = byId("mode-toggle");
function showModes(open) {
  const menu = byId("mode-menu");
  if (open) menu.style.setProperty("--mode-drop", byId("graph-modes").offsetHeight + 8 + "px");
  menu.classList.toggle("open", open);
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

const nodePicker = byId("node-picker");
const nodeResults = byId("node-results");
let activeNodeOption = -1;

function showNodeResults() {
  const query = nodePicker.value.toLowerCase().trim();
  const matches = [...nodes.values()].filter(({ label, kind }) => `${label} ${kind}`.toLowerCase().includes(query))
    .sort((a, b) => a.label.localeCompare(b.label));
  nodeResults.replaceChildren(...matches.map((node, index) => {
    const option = element("li", node.label);
    option.id = "node-option-" + index;
    option.dataset.nodeId = node.id;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    return option;
  }));
  if (!matches.length) nodeResults.append(element("li", "No matching nodes", "placeholder"));
  nodeResults.hidden = false;
  nodeResults.scrollTop = 0;
  nodePicker.setAttribute("aria-expanded", "true");
  nodePicker.removeAttribute("aria-activedescendant");
  activeNodeOption = -1;
}

function closeNodeResults() {
  nodeResults.hidden = true;
  nodePicker.value = "";
  nodePicker.setAttribute("aria-expanded", "false");
  nodePicker.removeAttribute("aria-activedescendant");
}

nodePicker.addEventListener("focus", showNodeResults);
nodePicker.addEventListener("click", () => { if (nodeResults.hidden) showNodeResults(); });
nodePicker.addEventListener("input", showNodeResults);
nodePicker.addEventListener("blur", closeNodeResults);
nodeResults.addEventListener("pointerdown", (event) => {
  if (event.target.closest("[role=option]")) event.preventDefault();
});
nodeResults.addEventListener("click", (event) => {
  const option = event.target.closest("[role=option]");
  if (!option) return;
  inspectNode(option.dataset.nodeId, true);
  closeNodeResults();
});
nodePicker.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !nodeResults.hidden) {
    event.stopPropagation();
    closeNodeResults();
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    if (nodeResults.hidden) showNodeResults();
    const options = [...nodeResults.querySelectorAll("[role=option]")];
    if (!options.length) return;
    options[activeNodeOption]?.setAttribute("aria-selected", "false");
    const step = event.key === "ArrowDown" ? 1 : -1;
    activeNodeOption = activeNodeOption === -1 && step === -1 ? options.length - 1 :
      (activeNodeOption + step + options.length) % options.length;
    const option = options[activeNodeOption];
    option.setAttribute("aria-selected", "true");
    nodePicker.setAttribute("aria-activedescendant", option.id);
    option.scrollIntoView({ block: "nearest" });
  } else if (event.key === "Enter" && !nodeResults.hidden) {
    const option = nodeResults.querySelector("[aria-selected=true], [role=option]:only-child");
    if (option) {
      event.preventDefault();
      option.click();
    }
  }
});

byId("fit").addEventListener("click", fit);
function zoomBy(factor) {
  cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
}
byId("zoom-in").addEventListener("click", () => zoomBy(1.2));
byId("zoom-out").addEventListener("click", () => zoomBy(1 / 1.2));
cy.on("tap", "node", (event) => inspectNode(selectedNode === event.target.id() ? null : event.target.id()));
cy.on("tap", (event) => { if (event.target === cy) inspectNode(null); });
cy.on("tap", "edge", (event) => {
  selectedEdge = snapshot.graph.edges.find((edge) =>
    edge.source === event.target.data("source") && edge.target === event.target.data("target")
    && edge.kind === event.target.data("kind") && edge.provenance === event.target.data("provenance"));
  selectedNode = null;
  applyView();
});
byId("catalog-filter").addEventListener("input", renderCatalog);
refreshCatalog();
refreshEvents();
refresh();
