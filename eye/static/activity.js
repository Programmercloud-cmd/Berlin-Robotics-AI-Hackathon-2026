const alertLoads = new Map();
const alertHistory = new Map();
const activityMap = document.getElementById("activity-map");
const activityOpenedAt = Date.now();
const newAlertDuration = 6000;
const hourMs = 60 * 60 * 1000;
const rollingCadenceMs = 5 * 60 * 1000;
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
    opened: date(incidentOpenedAt(incident)) + " UTC",
    recovery: incidentRecoveryStatus(incident),
    alerts: (alertLoads.get(incident.id)?.alerts ?? []).map((alert) => ({
      id: alert.id,
      metricId: alert.metric_id,
      label: name(metricID(alert.metric_id)),
      affected: scope(alert.scope),
      time: date(alert.window_start) + " UTC",
      windowStart: alert.window_start,
      windowEnd: alertWindowEnd(alert.window_start, alert.window_end),
      observedAt: alert.observed_at,
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
  details.append(heading, element("p", [alert.label, alert.affected].join(" · "), "panel-meta"));

  const metric = catalog.find((item) => item.id === alert.metricId || item.legacy_ids?.includes(alert.metricId));
  const definition = metric?.definition || nodes.get(metricID(alert.metricId))?.properties.definition;
  if (definition) details.append(element("p", definition));
  const unit = metric?.access.unit;
  const readingLabel = unit && !["count", "ratio"].includes(unit) ? `Observed vs baseline (${unit})` : "Observed vs baseline";
  const facts = element("dl", null, "activity-alert-facts");
  const fields = [
    ["Observation window", alertWindow(alert.windowStart, alert.windowEnd)],
    [readingLabel, compareReading(alert, unit)],
    ["Sample size", number(alert.sampleSize)],
    ["Deviation score", number(alert.score) + " · cutoff 4"],
  ];
  if (alert.observedAt) fields.splice(1, 0, ["Evaluated at", date(alert.observedAt) + " UTC"]);
  for (const [label, value] of fields) {
    const fact = element("div");
    fact.append(element("dt", label), element("dd", value));
    facts.append(fact);
  }
  details.append(facts);
  const chart = element("figure", null, "activity-alert-chart");
  chart.hidden = true;
  details.append(chart);
  readAlertHistory(alert).then(({ points, aligned }) => {
    if (points.slice(0, -1).filter(({ value }) => Number.isFinite(value)).length < 2) return;
    renderAlertChart(chart, alert, unit, points, aligned);
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

async function readActivityObservations(name, args) {
  const response = await fetch("/mcp", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }),
  });
  if (!response.ok) throw new Error("Readings unavailable");
  const { result } = await response.json();
  const rows = result?.structuredContent?.data;
  if (result?.isError || !Array.isArray(rows)) throw new Error("Readings unavailable");
  return rows;
}

async function loadActivityHistory(alert, start) {
  const args = { metric_id: alert.metricId, scope: alert.scope };
  if (alert.observedAt) {
    try {
      const rows = await readActivityObservations("alert_chart", { ...args, window_start: new Date(start).toISOString() });
      return { aligned: true, points: rows.map((row) => ({ ...row, time: Date.parse(row.window_end) })) };
    } catch {
      // Older running Eye processes can still serve their recorded readings.
    }
  }
  const hours = alert.observedAt ? 2 : 7;
  const rows = await readActivityObservations("metric_readings", {
    ...args, start: new Date(start - hours * hourMs).toISOString(), end: new Date(start).toISOString(),
  });
  return { aligned: false, points: rows.map((row) => ({
    time: Date.parse(alertWindowEnd(row.window_start, row.details?.bucket_end)),
    value: row.value, sample_size: row.sample_size, expected: alert.expected,
  })) };
}

function readAlertHistory(alert) {
  const start = Date.parse(alert.windowStart);
  const end = Date.parse(alert.windowEnd);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return Promise.resolve({ points: [] });
  const key = JSON.stringify([alert.metricId, start, alert.scope, alert.observedAt]);
  if (!alertHistory.has(key)) {
    const request = loadActivityHistory(alert, start).catch((error) => {
      alertHistory.delete(key);
      throw error;
    });
    alertHistory.set(key, request);
  }
  return alertHistory.get(key).then(({ points, aligned }) => ({
    aligned,
    points: [...points.filter(({ time }) => time < end), {
      ...points.find(({ time }) => time === end), time: end,
      value: alert.observed, expected: alert.expected, sample_size: alert.sampleSize,
    }],
  }));
}

function renderAlertChart(figure, alert, unit, points, aligned) {
  const format = unit === "ratio" ? (value) => percent.format(value) : number;
  const data = points.flatMap(({ value, expected, p10, p90 }) => [value, expected, p10, p90]).filter(Number.isFinite);
  const min = Math.min(...data);
  const max = Math.max(...data);
  const padding = Math.max((max - min) * .14, Math.abs(max) * .025, .01);
  const low = min >= 0 ? Math.max(0, min - padding) : min - padding;
  const high = unit === "ratio" && max <= 1 ? Math.min(1, max + padding) : max + padding;
  const left = 64, right = 505, top = 15, bottom = 122;
  const end = points.at(-1).time;
  const start = end - (alert.observedAt ? 2 : 7) * hourMs;
  const cadence = alert.observedAt ? rollingCadenceMs : hourMs;
  const x = (time) => left + (right - left) * (time - start) / (end - start);
  const y = (value) => bottom - (bottom - top) * (value - low) / (high - low);

  const svg = (tag, attrs, text) => {
    const item = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) item.setAttribute(key, value);
    if (text) item.textContent = text;
    return item;
  };
  const chart = svg("svg", { viewBox: "0 0 520 150", role: "img",
    "aria-label": `${alert.label} over time for ${alert.affected}. Alert reading ${format(alert.observed)}. Alert baseline ${format(alert.expected)}.` });
  for (const value of [low, (low + high) / 2, high]) {
    const yy = y(value);
    chart.append(svg("line", { class: "chart-grid", x1: left, x2: right, y1: yy, y2: yy }),
      svg("text", { x: left - 7, y: yy, "text-anchor": "end", "dominant-baseline": "middle" }, format(value)));
  }
  for (const [time, anchor] of [[start, "start"], [(start + end) / 2, "middle"], [end, "end"]]) {
    chart.append(svg("text", { x: x(time), y: 142, "text-anchor": anchor }, date(new Date(time).toISOString())));
  }

  const band = [];
  let segment = [];
  for (const point of [...points, {}]) {
    if ((!Number.isFinite(point.p10) || point.time - segment.at(-1)?.time > cadence) && segment.length) {
      if (segment.length === 1) {
        const { time, p10, p90 } = segment[0];
        const a = Math.max(left, x(time) - 3);
        const b = Math.min(right, x(time) + 3);
        band.push(`M ${a} ${y(p90)} H ${b} V ${y(p10)} H ${a} Z`);
      } else {
        const upper = segment.map(({ time, p90 }) => `${x(time)} ${y(p90)}`);
        const lower = [...segment].reverse().map(({ time, p10 }) => `${x(time)} ${y(p10)}`);
        band.push(`M ${[...upper, ...lower].join(" L ")} Z`);
      }
      segment = [];
    }
    if (Number.isFinite(point.p10)) segment.push(point);
  }
  if (band.length) chart.append(svg("path", { class: "chart-reference-band", d: band.join(" ") }));
  const line = (key) => points.map((point, i) => {
    if (!Number.isFinite(point[key])) return "";
    const previous = points[i - 1];
    const connected = Number.isFinite(previous?.[key]) && point.time - previous.time <= cadence;
    return `${connected ? "L" : "M"} ${x(point.time)} ${y(point[key])}`;
  }).join(" ");
  const expectedLine = line("expected");
  const hasAlignedBaseline = aligned && expectedLine.includes("L ");
  const baseline = hasAlignedBaseline ? expectedLine : `M ${left} ${y(alert.expected)} H ${right}`;
  chart.append(svg("path", { class: "chart-baseline", d: baseline }),
    svg("path", { class: "chart-series", d: line("value") }));
  points.forEach(({ time, value, sample_size, references }, i) => {
    if (!Number.isFinite(value)) return;
    const ring = i === points.length - 1;
    const point = svg("circle", { class: ring ? "chart-trigger" : "chart-point", cx: x(time), cy: y(value), r: ring ? 4.5 : 2.7 });
    const sample = Number.isFinite(sample_size) ? ` · n = ${number(sample_size)}` : "";
    const reference = references ? ` · ${references} reference windows` : "";
    const evaluation = ring && alert.observedAt ? ` · Evaluated ${date(alert.observedAt)} UTC` : "";
    point.append(svg("title", {}, `${date(new Date(time).toISOString())} UTC · ${format(value)}${sample}${reference}${evaluation}`));
    chart.append(point);
  });

  const legend = element("div", null, "activity-chart-legend");
  const keys = [["Observed", "series"], [hasAlignedBaseline ? "Time-aligned baseline" : "Alert baseline", "baseline"], ["Alert reading", "trigger"]];
  if (band.length) keys.splice(2, 0, ["Historical 10th–90th percentile", "band"]);
  for (const [label, kind] of keys) {
    const item = element("span", label, "chart-key " + kind);
    legend.append(item);
  }
  const caption = alert.observedAt ? "Rolling one-hour readings · every 5 minutes" : "Hourly readings";
  figure.replaceChildren(element("figcaption", caption + " · window end in UTC"), chart, legend);
}

function updateActivitySelection() {
  for (const group of activityMap.children) {
    const linked = Number(group.dataset.incident) === selectedIncident;
    if (group.classList.contains("is-linked") === linked) continue;
    group.classList.toggle("is-linked", linked);
    group.querySelector(".investigation-node").setAttribute("aria-pressed", linked);
  }
}
