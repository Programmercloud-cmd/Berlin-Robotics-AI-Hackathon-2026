const alertLoads = new Map();
const alertHistory = new Map();
const activityMap = document.getElementById("activity-map");
const activityOpenedAt = Date.now();
const newAlertDuration = 6000;
const hourMs = 60 * 60 * 1000;
const alertHistoryHours = 7;
let activityKey = "";
let expandedActivityAlert = null;

function readIncidentAlerts(incident) {
  const previous = alertLoads.get(incident.id);
  if (previous?.request && previous.updatedAt === incident.updated_at) return previous.request;
  const entry = { updatedAt: incident.updated_at, alerts: previous?.alerts ?? [] };
  entry.request = requestIncidentEvidence(incident.id, true).then((evidence) => {
    const alerts = new Map();
    const previousAlerts = new Map(entry.alerts.map((alert) => [alert.id, alert]));
    const receivedAt = Date.now();
    for (const { kind, payload } of evidence) {
      if (["anomaly", "related anomaly", "ongoing anomaly"].includes(kind) && payload.id != null && !alerts.has(payload.id)) {
        const detectedLive = Date.parse(payload.detected_at) >= activityOpenedAt - newAlertDuration;
        const newUntil = previousAlerts.get(payload.id)?.newUntil ?? (detectedLive ? receivedAt + newAlertDuration : 0);
        alerts.set(payload.id, { ...payload, kind, newUntil });
      }
    }
    entry.alerts = [...alerts.values()];
    if (alertLoads.get(incident.id) === entry) renderActivityGraph();
    return entry.alerts;
  }).catch((error) => {
    entry.request = null;
    throw error;
  });
  alertLoads.set(incident.id, entry);
  return entry.request;
}

function refreshActivityGraph() {
  const ids = new Set(snapshot.incidents.map((incident) => incident.id));
  for (const id of alertLoads.keys()) if (!ids.has(id)) alertLoads.delete(id);
  for (const incident of snapshot.incidents) {
    const cached = alertLoads.get(incident.id);
    if (cached?.updatedAt === incident.updated_at && cached.request) continue;
    readIncidentAlerts(incident).catch(() => {});
  }
  renderActivityGraph();
}

function renderActivityGraph() {
  const now = Date.now();
  const newestFirst = [...snapshot.incidents].sort((a, b) => Date.parse(b.opened_at) - Date.parse(a.opened_at) || b.id - a.id);
  const investigations = newestFirst.map((incident) => ({
    id: incident.id,
    step: incidentStep(incident),
    opened: date(incident.opened_at) + " UTC",
    recovery: incidentRecoveryStatus(incident),
    alerts: (alertLoads.get(incident.id)?.alerts ?? []).map((alert) => ({
      id: alert.id,
      metricId: alert.metric_id,
      label: name(metricID(alert.metric_id)),
      affected: scope(alert.scope),
      time: date(alert.window_start) + " UTC",
      windowStart: alert.window_start,
      scope: alert.scope,
      status: incident.recovered_at ? "recovered" : alert.newUntil > now ? "new" : "investigating",
      observed: alert.observed,
      expected: alert.expected,
      score: alert.score,
      sampleSize: alert.sample_size,
      relationship: alert.relationship,
      checks: alert.completion_checks,
    })),
  }));
  byId("activity-empty").hidden = Boolean(investigations.length);
  byId("activity-board").hidden = !investigations.length;
  const key = JSON.stringify(investigations);
  if (key === activityKey) return;
  activityKey = key;
  const focused = document.activeElement?.matches(".activity-alerts .alert-node") ? document.activeElement.id : null;
  activityMap.replaceChildren(...investigations.map(renderActivityGroup));
  updateActivitySelection();
  if (focused) byId(focused)?.focus({ preventScroll: true });
}

function renderActivityGroup({ id, step, opened, recovery, alerts }) {
  const group = element("section", null, "activity-group");
  group.dataset.incident = id;
  group.setAttribute("aria-label", `Incident ${id} · ${step}`);
  const alertGrid = element("div", null, "activity-alerts");

  function node(kind, label, caption, badge) {
    const button = element("button", null, "activity-node " + kind);
    button.type = "button";
    const title = element("strong", label);
    if (badge) {
      const line = element("span", null, "activity-node-title");
      line.append(title, badge);
      button.append(line);
    } else button.append(title);
    button.append(element("small", caption));
    return button;
  }

  const alertStatus = { new: "New alert", investigating: "Under investigation", recovered: "Recovered" };
  for (let start = 0; start < alerts.length; start += 4) {
    const row = element("div", null, "activity-alert-row");
    let details = null;
    for (const alert of alerts.slice(start, start + 4)) {
      const { label, affected, time, status } = alert;
      const button = node("alert-node", label, affected);
      button.id = `activity-alert-${id}-${alert.id}`;
      button.dataset.status = status;
      button.append(element("small", time));
      button.title = [label, affected, time, "Incident " + id, alertStatus[status]].join(" · ");
      button.setAttribute("aria-label", button.title);
      button.setAttribute("aria-expanded", "false");
      button.addEventListener("click", () => toggleActivityAlert(button, row, alert));
      if (expandedActivityAlert === button.id) details = renderActivityAlertDetails(button, alert);
      row.append(button);
    }
    alertGrid.append(row);
    if (details) alertGrid.append(details);
  }
  if (alerts.length) group.append(alertGrid);
  const badge = element("span", recovery, "pill" + (recovery === "Recovered" ? " recovered" : ""));
  const incident = node("investigation-node", step, `Incident ${id} · ${opened}`, badge);
  incident.setAttribute("aria-pressed", "false");
  incident.addEventListener("click", () => {
    selectIncident(id === selectedIncident ? null : id);
    if (selectedIncident) byId("incident-" + id)?.scrollIntoView({ block: "nearest" });
  });
  group.append(incident);
  return group;
}

function toggleActivityAlert(button, row, alert) {
  const wasOpen = expandedActivityAlert === button.id;
  if (expandedActivityAlert) {
    const previous = byId(expandedActivityAlert);
    previous?.setAttribute("aria-expanded", "false");
    previous?.removeAttribute("aria-controls");
    byId(expandedActivityAlert + "-details")?.remove();
  }
  expandedActivityAlert = wasOpen ? null : button.id;
  if (!wasOpen) {
    const details = renderActivityAlertDetails(button, alert);
    row.after(details);
    details.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
}

function renderActivityAlertDetails(button, alert) {
  const details = element("section", null, "activity-alert-details");
  details.id = button.id + "-details";
  const heading = element("h3", "Why this alert fired");
  heading.id = details.id + "-title";
  details.setAttribute("aria-labelledby", heading.id);
  button.setAttribute("aria-expanded", "true");
  button.setAttribute("aria-controls", details.id);
  details.append(heading, element("p", [alert.label, alert.affected, alert.time].join(" · "), "panel-meta"));

  const metric = catalog.find((item) => item.id === alert.metricId || item.legacy_ids?.includes(alert.metricId));
  const definition = metric?.definition || nodes.get(metricID(alert.metricId))?.properties.definition;
  if (definition) details.append(element("p", definition));
  const unit = metric?.access.unit;
  const readingLabel = unit && !["count", "ratio"].includes(unit) ? `Observed vs baseline (${unit})` : "Observed vs baseline";
  const facts = element("dl", null, "activity-alert-facts");
  for (const [label, value] of [
    [readingLabel, compareReading(alert, unit)],
    ["Sample size", number(alert.sampleSize)],
    ["Deviation score", number(alert.score) + " · cutoff 4"],
  ]) {
    const fact = element("div");
    fact.append(element("dt", label), element("dd", value));
    facts.append(fact);
  }
  details.append(facts);
  const chart = element("figure", null, "activity-alert-chart");
  chart.hidden = true;
  details.append(chart);
  readAlertHistory(alert).then((points) => {
    if (points.slice(0, -1).filter(({ value }) => Number.isFinite(value)).length < 2) return;
    renderAlertChart(chart, alert, unit, points);
    chart.hidden = false;
  }).catch(() => {});
  details.append(element("p", "Eye compared this window with the historical baseline for the same scope and applied the metric’s alert checks."));
  if (alert.relationship) details.append(element("p", "Investigation relationship · " + words(alert.relationship), "panel-meta"));
  if (alert.checks?.length) {
    const list = element("ul", null, "activity-workflow-checks");
    for (const check of alert.checks) {
      const volumes = check.eligible == null ? "" : ` · ${number(check.completed)} of ${number(check.eligible)} completed`;
      list.append(element("li", `${check.label} · ${check.status} · ${compareReading(check, "ratio")}${volumes}`));
    }
    details.append(element("h3", "Workflow checks"), list);
  }
  return details;
}

function readAlertHistory(alert) {
  const end = Date.parse(alert.windowStart);
  if (!Number.isFinite(end)) return Promise.resolve([]);
  const start = end - alertHistoryHours * hourMs;
  const key = JSON.stringify([alert.metricId, end, alert.scope]);
  if (!alertHistory.has(key)) {
    const request = fetch("/mcp", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
      body: JSON.stringify({
        jsonrpc: "2.0", id: 1, method: "tools/call",
        params: { name: "metric_readings", arguments: {
          metric_id: alert.metricId, scope: alert.scope,
          start: new Date(start).toISOString(), end: new Date(end).toISOString(),
        } },
      }),
    }).then(async (response) => {
      if (!response.ok) throw new Error("Readings unavailable");
      const { result } = await response.json();
      const rows = result?.structuredContent?.data;
      if (result?.isError || !Array.isArray(rows)) throw new Error("Readings unavailable");
      return new Map(rows.map((row) => [Date.parse(row.window_start), row.value]));
    }).catch((error) => {
      alertHistory.delete(key);
      throw error;
    });
    alertHistory.set(key, request);
  }
  return alertHistory.get(key).then((readings) => Array.from({ length: alertHistoryHours + 1 }, (_, i) => {
    const time = start + i * hourMs;
    return { time, value: time === end ? alert.observed : readings.get(time) };
  }));
}

function renderAlertChart(figure, alert, unit, points) {
  const format = unit === "ratio" ? (value) => percent.format(value) : number;
  const data = [...points.map(({ value }) => value), alert.expected].filter(Number.isFinite);
  const min = Math.min(...data);
  const max = Math.max(...data);
  const padding = Math.max((max - min) * .14, Math.abs(max) * .025, .01);
  const low = min >= 0 ? Math.max(0, min - padding) : min - padding;
  const high = unit === "ratio" && max <= 1 ? Math.min(1, max + padding) : max + padding;
  const left = 64, right = 505, top = 13, bottom = 105;
  const x = (i) => left + (right - left) * i / alertHistoryHours;
  const y = (value) => bottom - (bottom - top) * (value - low) / (high - low);

  const svg = (tag, attrs, text) => {
    const item = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) item.setAttribute(key, value);
    if (text) item.textContent = text;
    return item;
  };
  const chart = svg("svg", { viewBox: "0 0 520 130", role: "img",
    "aria-label": `${alert.label} by hour for ${alert.affected}. Alert reading ${format(alert.observed)}. Alert baseline ${format(alert.expected)}.` });
  for (const value of [low, (low + high) / 2, high]) {
    const yy = y(value);
    chart.append(svg("line", { class: "chart-grid", x1: left, x2: right, y1: yy, y2: yy }),
      svg("text", { x: left - 7, y: yy, "text-anchor": "end", "dominant-baseline": "middle" }, format(value)));
  }
  for (const i of [0, 3, alertHistoryHours]) {
    chart.append(svg("text", { x: x(i), y: 123, "text-anchor": i === 0 ? "start" : i === alertHistoryHours ? "end" : "middle" },
      date(new Date(points[i].time).toISOString())));
  }

  const referenceY = y(alert.expected);
  chart.append(svg("line", { class: "chart-baseline", x1: left, x2: right, y1: referenceY, y2: referenceY }));
  const path = points.map(({ value }, i) => Number.isFinite(value) ?
    `${Number.isFinite(points[i - 1]?.value) ? "L" : "M"} ${x(i)} ${y(value)}` : "").join(" ");
  chart.append(svg("path", { class: "chart-series", d: path }));
  points.forEach(({ time, value }, i) => {
    if (!Number.isFinite(value)) return;
    const ring = i === alertHistoryHours;
    const point = svg("circle", { class: ring ? "chart-trigger" : "chart-point", cx: x(i), cy: y(value), r: ring ? 4.5 : 2.7 });
    point.append(svg("title", {}, `${date(new Date(time).toISOString())} UTC · ${format(value)}`));
    chart.append(point);
  });

  const legend = element("div", null, "activity-chart-legend");
  for (const [label, kind] of [["Reading", "series"], ["Baseline used for this alert", "baseline"], ["Alert reading", "trigger"]]) {
    const item = element("span", label, "chart-key " + kind);
    legend.append(item);
  }
  figure.replaceChildren(element("figcaption", "Hourly readings for this scope · UTC"), chart, legend);
}

function updateActivitySelection() {
  for (const group of activityMap.children) {
    const linked = Number(group.dataset.incident) === selectedIncident;
    if (group.classList.contains("is-linked") === linked) continue;
    group.classList.toggle("is-linked", linked);
    group.querySelector(".investigation-node").setAttribute("aria-pressed", linked);
  }
}
