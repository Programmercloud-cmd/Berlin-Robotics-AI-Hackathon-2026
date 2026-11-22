const alertLoads = new Map();
const activityMap = document.getElementById("activity-map");
let activityKey = "";

function alertEpisodes(alerts) {
  const bySignal = new Map();
  const unique = new Map(alerts.map((alert) => [alert.id, alert]));
  for (const alert of [...unique.values()].sort((a, b) => Date.parse(a.window_start) - Date.parse(b.window_start))) {
    const start = Date.parse(alert.window_start);
    const end = Date.parse(alert.window_end) || start + 3600000;
    const key = JSON.stringify([alert.incident_id, alert.metric_id, Object.entries(alert.scope || {}).sort()]);
    const episodes = bySignal.get(key) || [];
    const previous = episodes.at(-1);
    if (previous && start <= previous.end) {
      previous.end = Math.max(previous.end, end);
      previous.members.push(alert);
    } else episodes.push({ start, end, members: [alert] });
    bySignal.set(key, episodes);
  }
  return [...bySignal.values()].flat().sort((a, b) => b.end - a.end);
}

function readIncidentAlerts(incident) {
  const previous = alertLoads.get(incident.id);
  if (previous?.request && previous.updatedAt === incident.updated_at) return previous.request;
  const entry = { updatedAt: incident.updated_at, alerts: previous?.alerts ?? [] };
  entry.request = requestIncidentEvidence(incident.id, true).then((evidence) => {
    const alerts = new Map();
    for (const { kind, payload } of evidence) {
      if (["anomaly", "related anomaly", "ongoing anomaly"].includes(kind) && payload.id != null && !alerts.has(payload.id)) {
        alerts.set(payload.id, { ...payload, kind });
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
  const ids = new Set(snapshot.incidents.map((incident) => incident.id));
  const newestFirst = snapshot.incidents.filter((incident) => incident.id === incident.group_id || !ids.has(incident.group_id))
    .sort((a, b) => Date.parse(b.opened_at) - Date.parse(a.opened_at) || b.id - a.id);
  const owners = new Map();
  for (const incident of newestFirst) {
    for (const alert of alertLoads.get(incident.id)?.alerts ?? []) {
      const owner = owners.get(alert.id);
      if (!owner || (alert.kind === "anomaly" && owner.kind !== "anomaly")) owners.set(alert.id, { id: incident.id, kind: alert.kind });
    }
  }
  const investigations = newestFirst.map((incident) => ({
    id: incident.id,
    step: incidentStep(incident),
    opened: date(incidentOpenedAt(incident)) + " UTC",
    recovery: incidentRecoveryStatus(incident),
    alerts: alertEpisodes((alertLoads.get(incident.id)?.alerts ?? []).filter((alert) => owners.get(alert.id)?.id === incident.id))
      .map(({ start, end, members: [alert, ...rest] }) => ({
      id: alert.id,
      metricId: alert.metric_id,
      label: name(metricID(alert.metric_id)),
      affected: scope(alert.scope),
      time: alertWindow(new Date(start).toISOString(), new Date(end).toISOString()),
      windows: 1 + rest.length,
      window_start: alert.window_start,
      scope: alert.scope,
      status: incident.recovered_at ? "recovered" :
        incident.investigation_pending && incident.report?.deep_investigation === "pending" ? "new" : "investigating",
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
    for (const alert of alerts.slice(start, start + 4)) {
      const { label, affected, time, status, windows } = alert;
      const button = node("alert-node", label, affected);
      button.id = `activity-alert-${id}-${alert.id}`;
      button.dataset.status = status;
      button.append(element("small", time));
      if (windows > 1) button.append(element("small", `${windows} alert windows`));
      button.title = [label, affected, time, `${windows} alert ${windows === 1 ? "window" : "windows"}`, "Incident " + id, alertStatus[status]].join(" · ");
      button.setAttribute("aria-label", button.title);
      button.setAttribute("aria-haspopup", "dialog");
      button.addEventListener("click", () => openMetricInsights(alert.metricId, alert));
      row.append(button);
    }
    alertGrid.append(row);
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

function updateActivitySelection() {
  const selectedGroup = snapshot.incidents.find((incident) => incident.id === selectedIncident)?.group_id ?? selectedIncident;
  for (const group of activityMap.children) {
    const linked = Number(group.dataset.incident) === selectedGroup;
    if (group.classList.contains("is-linked") === linked) continue;
    group.classList.toggle("is-linked", linked);
    group.querySelector(".investigation-node").setAttribute("aria-pressed", linked);
  }
}
