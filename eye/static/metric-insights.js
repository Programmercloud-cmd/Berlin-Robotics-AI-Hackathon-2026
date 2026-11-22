const insightsDialog = document.getElementById("metric-insights");
const insightsPage = 6 * 60 * 60 * 1000;
const insightStatuses = { new: "New", investigating: "Under review", recovered: "Recovered" };
const occurrenceFormat = new Intl.DateTimeFormat("en-GB", {
  day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
});
const referenceWindowFormat = new Intl.DateTimeFormat("en-GB", {
  day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC",
});
let insightSelection, insightData, insightRequest, insightKey, insightOpener;

function historicalReferenceNote(point) {
  const dates = point?.reference_windows?.map((start) => new Date(start));
  if (!dates?.length) return null;
  const range = occurrenceFormat.formatRange(dates[0], dates.at(-1));
  return `Latest historical reference · ${dates.length} comparable windows · ${range} UTC`;
}

function historicalReferenceTooltip(point) {
  const starts = point?.reference_windows;
  return starts?.length ? `Live reference window starts (UTC): ${starts.map((start) => referenceWindowFormat.format(new Date(start))).join("; ")}` : "";
}

function eventReferenceNote(reading) {
  const occurrence = new Date(reading?.history?.[0]?.occurrence);
  if (!Number.isFinite(occurrence.getTime())) return null;
  const eventYear = new Date(reading.window_start).getUTCFullYear();
  const label = occurrence.getUTCFullYear() === eventYear - 1 ? "Previous-year event reference" : "Prior event reference";
  return `${label} · ${occurrenceFormat.format(occurrence)} UTC`;
}

function openMetricInsights(metric, alert = null) {
  const latest = Date.parse(snapshot.last_window);
  const alertStart = Date.parse(alert?.window_start);
  const anchor = Number.isFinite(alertStart) && alertStart + insightsPage / 6 < latest
    ? new Date(alertStart + insightsPage / 6).toISOString() : null;
  insightSelection = { metric, scope: alert?.scope || {}, alert: alert?.id ?? null, anchor };
  insightData = null;
  insightKey = null;
  byId("metric-insights-title").textContent = name(metricID(metric));
  byId("metric-insights-description").textContent = "Metric Insights";
  byId("metric-insights-chart").replaceChildren();
  byId("metric-insights-alert").replaceChildren();
  byId("metric-insights-point").textContent = "";
  byId("metric-insights-cadence-status").hidden = true;
  for (const id of ["scope", "cadence", "older", "newer", "live"]) byId(`metric-insights-${id}`).disabled = true;
  if (!insightsDialog.open) {
    insightOpener = document.activeElement;
    insightsDialog.showModal();
  }
  refreshMetricInsights();
}

async function refreshMetricInsights() {
  if (!insightsDialog.open || !insightSelection) return;
  const { metric, scope: filters, anchor } = insightSelection;
  const theme = document.documentElement.dataset.theme;
  const key = JSON.stringify([metric, filters, anchor, theme, snapshot.last_window, snapshot.incidents.map((i) =>
    [i.id, i.recovered_at, i.investigation_pending, i.report?.deep_investigation])]);
  if (key === insightKey) return;
  insightKey = key;
  insightRequest?.abort();
  const request = new AbortController();
  insightRequest = request;
  const status = byId("metric-insights-status");
  if (!insightData) status.textContent = "Loading metric history…";
  const options = new URLSearchParams({ scope: JSON.stringify(filters), theme });
  if (anchor) options.set("anchor", anchor);
  try {
    const response = await fetch(`/api/metric-insights/${encodeURIComponent(metric)}?${options}`, { signal: request.signal });
    if (!response.ok) throw new Error("Metric history is unavailable.");
    const data = await response.json();
    if (request.signal.aborted) return;
    insightData = data;
    renderMetricInsights();
  } catch (error) {
    if (request.signal.aborted) return;
    insightKey = null;
    status.textContent = error.message;
  }
}

function renderMetricInsights() {
  const { metric, scope: filters, scopes, points, alerts, plot, anchor } = insightData;
  const target = byId("metric-insights-chart");
  const focused = target.contains(document.activeElement) ? document.activeElement.dataset.alert : null;
  const selector = byId("metric-insights-scope");
  const cadence = byId("metric-insights-cadence");
  const unit = metric.access.unit;
  byId("metric-insights-title").textContent = metric.name;
  byId("metric-insights-description").textContent = metric.definition;
  selector.replaceChildren(...scopes.map((value, index) => new Option(scope(value), index)));
  selector.disabled = false;
  selector.selectedIndex = scopes.findIndex((value) => JSON.stringify(value) === JSON.stringify(filters));
  if (!cadence.options.length) cadence.replaceChildren(...Array.from(cadenceLabels, ([value, label]) => new Option(label, value)));
  if (!cadence.dataset.saving) {
    cadence.disabled = false;
    cadence.value = metric.access.cadence_minutes ?? (metric.direction === "none" ? 60 : 5);
  }
  const live = !insightSelection.anchor;
  byId("metric-insights-live").disabled = live;
  byId("metric-insights-newer").disabled = live;
  byId("metric-insights-older").disabled = !anchor;
  const reference = points.findLast((point) => point.normal != null);
  byId("metric-insights-status").textContent = plot ? `${live ? "Live" : "History"} · ${scope(filters)}` +
    (reference ? ` · ${reference.method} · ${reference.references} reference windows` : " · Building baseline") +
    (reference && metric.direction !== "none" && !points.some((p) => p.live_lower != null || p.live_upper != null)
      ? " · Trigger awaiting more samples" : "") : "No readings in this window.";
  target.replaceChildren();
  byId("metric-insights-point").textContent = "";
  if (plot) {
    const figure = element("div", null, "insights-figure");
    const image = element("img");
    image.src = `data:image/png;base64,${plot}`;
    image.alt = `${metric.name} over time in ${scope(filters)}. Window end in UTC.`;
    figure.append(image);
    const position = (node, point) => {
      node.style.left = `${point.x}%`;
      node.style.top = `${point.y}%`;
    };
    const format = (value) => value == null ? "—" : unit === "ratio" ? percent.format(value) : number(value);
    const pointSummary = (point) => `${date(point.window_end)} UTC · ${format(point.value)} observed · ` +
      `${format(point.normal)} reference` + (point.planned ? ` · ${format(point.planned.adjusted)} planned-event expectation` : "") +
      (point.live_lower != null ? ` · ${format(point.live_lower)} lower score boundary` : "") +
      (point.live_upper != null ? ` · ${format(point.live_upper)} upper score boundary` : "") +
      ` · n ${number(point.sample_size)} · ${words(point.quality || "quality unavailable")}`;
    const latestReading = points.findLast((point) => point.x != null);
    if (latestReading) byId("metric-insights-point").textContent = pointSummary(latestReading);
    for (const point of points.filter((point) => point.x != null)) {
      const hit = element("span", null, "insight-point");
      position(hit, point);
      const message = pointSummary(point);
      hit.title = [message, historicalReferenceTooltip(point)].filter(Boolean).join("\n");
      hit.addEventListener("pointerenter", () => { byId("metric-insights-point").textContent = message; });
      figure.append(hit);
    }
    const pointsByStart = new Map(points.map((point) => [point.window_start, point]));
    for (const { members } of alertEpisodes(alerts.filter((row) => row.x != null))) {
      const alert = members[0];
      const marker = element("button", null, "insight-episode");
      marker.type = "button";
      marker.dataset.alert = alert.id;
      marker.dataset.windows = members.map((row) => row.id).join(",");
      marker.dataset.status = alert.status;
      const left = Math.min(...members.map((row) => row.span_left ?? Math.max(10, row.x - 86 / 72)));
      const right = Math.max(...members.map((row) => row.x));
      marker.style.left = `${left}%`;
      marker.style.width = `${right - left}%`;
      marker.classList.toggle("label-left", right > 72);
      const dates = members.length === 1 ? date(alert.window_end) : `${date(alert.window_end)}–${date(members.at(-1).window_end)}`;
      const label = `${insightStatuses[alert.status]} · ${dates} UTC · ` +
        `${members.length} alert ${members.length === 1 ? "window" : "windows"}`;
      marker.title = [label, historicalReferenceTooltip(pointsByStart.get(alert.window_start))].filter(Boolean).join("\n");
      marker.setAttribute("aria-label", label);
      marker.append(element("span", label, "insight-episode-tag"));
      marker.addEventListener("click", () => {
        if (!members.some((row) => row.id === insightSelection.alert)) insightSelection.alert = alert.id;
        renderInsightAlert();
      });
      figure.append(marker);
    }
    const legend = element("div", null, "activity-chart-legend");
    const plannedReference = points.findLast((point) => point.planned)?.planned;
    for (const [kind, label, present] of [
      ["series", "Observed", true], ["baseline", "Live historical reference", reference],
      ["band", "Historical 10th–90th percentile", points.some((p) => p.p10 != null)],
      ["live-threshold", "Live score trigger boundary", points.some((p) => p.live_lower != null || p.live_upper != null)],
      ["planned", "Planned-event expectation and range", plannedReference],
      ["threshold", "Saved score trigger boundary", alerts.some((alert) => alert.score_boundary != null)],
      ...Object.entries(insightStatuses).map(([status, label]) =>
        [status, label + " breach period", alerts.some((alert) => alert.status === status)]),
    ]) if (present) legend.append(element("span", label, `chart-key ${kind}`));
    target.append(figure, legend);
    const historicalNote = historicalReferenceNote(reference);
    if (historicalNote) target.append(element("p", historicalNote, "panel-meta"));
    const sourceNote = eventReferenceNote(plannedReference);
    if (sourceNote) target.append(element("p", sourceNote, "panel-meta"));
  }
  renderInsightAlert();
  if (focused) target.querySelector(`[data-alert="${focused}"]`)?.focus({ preventScroll: true });
}

function renderInsightAlert() {
  const target = byId("metric-insights-alert");
  const selected = insightData.alerts.find((alert) => alert.id === insightSelection.alert);
  for (const marker of byId("metric-insights-chart").querySelectorAll(".insight-episode")) {
    marker.setAttribute("aria-pressed", String(marker.dataset.windows.split(",").includes(String(selected?.id))));
  }
  target.replaceChildren();
  if (!selected) {
    if (insightData.alerts.length) target.append(element("p", "Select a highlighted breach period to inspect the saved evidence.", "panel-meta"));
    return;
  }
  const saved = selected.detection_reference || {};
  const captured = Object.keys(saved).length > 0;
  const facts = element("dl", null, "activity-alert-facts");
  const reading = { ...selected, ...saved };
  const utcMinute = (stamp) => new Date(stamp).toISOString().slice(0, 16).replace("T", " ");
  const values = [
    ["Observation window", `${utcMinute(selected.window_start)} – ${utcMinute(reading.window_end)} UTC`],
    ["Evaluated at", reading.observed_at ? date(reading.observed_at) + " UTC" : "Unavailable"],
    ["Observed vs saved baseline", compareReading(reading, insightData.metric.access.unit)],
    ["Sample size", number(reading.sample_size)],
    ["Deviation score", number(reading.score) + " · cutoff " + (saved.score_threshold ?? 4)],
    ["Data quality", words(captured ? saved.quality || "not recorded" : selected.observation_details?.quality || "not recorded")],
  ];
  const completeness = captured ? saved.completeness : selected.window_completeness;
  const format = insightData.metric.access.unit === "ratio" ? (value) => percent.format(value) : number;
  if (selected.score_boundary != null) values.push(["Saved score trigger boundary", format(selected.score_boundary)]);
  if (completeness != null) values.push(["Window completeness", percent.format(completeness)]);
  if (saved.method) values.push(["Baseline method", saved.method + ` · ${saved.references} reference windows`]);
  if (saved.p10 != null && saved.p90 != null) {
    values.push(["Saved historical range (10th–90th percentile)", `${format(saved.p10)}–${format(saved.p90)}`]);
  }
  if (saved.baseline_denominator) values.push(["Historical rate counts", `${number(saved.baseline_numerator)} of ${number(saved.baseline_denominator)}`]);
  if (saved.denominator) values.push(["Observed rate counts", `${number(saved.numerator)} of ${number(saved.denominator)}`]);
  for (const [label, value] of values) {
    const field = element("div");
    field.append(element("dt", label), element("dd", value));
    facts.append(field);
  }
  const detail = element("section", null, "activity-alert-details");
  detail.append(element("h3", "Why this alert fired"), element("p", `${insightStatuses[selected.status]} · ${scope(selected.scope)}`, "panel-meta"));
  if (selected.incident_id) {
    const link = element("a", `Incident ${selected.incident_id}`);
    link.href = `?incident=${selected.incident_id}`;
    detail.append(link);
  }
  detail.append(facts);
  detail.append(element("p", captured ? "Detection reference saved when the alert fired." :
    "The alert stores its original observed and expected values. Data quality is listed from the retained observation.", "panel-meta"));
  if (selected.score_boundary != null && ["ratio", "share"].includes(insightData.metric.op)) {
    detail.append(element("p", "Rate alerts also have to pass the detector’s binomial check.", "panel-meta"));
  }
  if (saved.planned_event) {
    detail.append(element("p", eventComparison(saved.planned_event)));
    const sourceNote = eventReferenceNote(saved.planned_event);
    if (sourceNote) detail.append(element("p", sourceNote, "panel-meta"));
  }
  if (selected.relationship) detail.append(element("p", "Investigation relationship · " + words(selected.relationship)));
  if (selected.checks?.length) {
    const list = element("ul", null, "activity-workflow-checks");
    for (const check of selected.checks) {
      const counts = check.eligible == null ? "" : ` · ${number(check.completed)} of ${number(check.eligible)} completed`;
      list.append(element("li", `${check.label} · ${check.status} · ${compareReading(check, "ratio")}${counts}`));
    }
    detail.append(element("h3", "Workflow checks"), list);
  }
  target.append(detail);
}

byId("metric-insights-scope").addEventListener("change", (event) => {
  insightSelection.scope = insightData.scopes[event.target.selectedIndex];
  insightSelection.alert = null;
  refreshMetricInsights();
});
byId("metric-insights-cadence").addEventListener("change", async ({ target }) => {
  const metric = insightSelection.metric;
  const status = byId("metric-insights-cadence-status");
  target.dataset.saving = "true";
  target.disabled = true;
  status.hidden = false;
  status.textContent = "Saving check cadence…";
  try {
    const response = await fetch(`/api/metrics/${encodeURIComponent(metric)}/cadence`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cadence_minutes: Number(target.value) }),
    });
    if (!response.ok) throw new Error("Could not save the check cadence.");
    if (insightSelection.metric === metric) status.textContent = "Check cadence saved.";
  } catch (error) {
    if (insightSelection.metric === metric) status.textContent = error.message;
  } finally {
    delete target.dataset.saving;
    insightKey = null;
    refreshMetricInsights();
    refreshCatalog(false);
  }
});
function moveInsights(direction) {
  if (!insightData?.anchor) return;
  const next = Date.parse(insightData.anchor) + (insightData.page_minutes * 60000 || insightsPage) * direction;
  insightSelection.anchor = direction === 0 || next >= Date.parse(insightData.latest) ? null : new Date(next).toISOString();
  insightSelection.alert = null;
  refreshMetricInsights();
}
byId("metric-insights-older").addEventListener("click", () => moveInsights(-1));
byId("metric-insights-newer").addEventListener("click", () => moveInsights(1));
byId("metric-insights-live").addEventListener("click", () => moveInsights(0));
insightsDialog.addEventListener("close", () => {
  if (insightsDialog.open) return;
  insightRequest?.abort();
  if (insightOpener?.isConnected) insightOpener.focus();
});
new MutationObserver(refreshMetricInsights).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
