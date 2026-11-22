"use strict";

(() => {
  const tabs = [...document.querySelectorAll(".company-tabs [role=tab]")];
  const caseButtons = [...document.querySelectorAll("[data-support-preview]")];
  const phases = {
    postgres: "Starting support PostgreSQL.",
    baseline: "Generating the support ticket baseline.",
    discovery: "Eye is discovering the support workflow.",
    registration: "Registering the resolution metric with Eye.",
    ready: "Support simulator and Eye are ready.",
    failed: "The support simulator could not continue.",
  };
  let current = { cases: {}, runs: [] };
  let draft = null;
  let posting = false;
  let timelineKey;

  function showCompany(company, push = false) {
    for (const tab of tabs) {
      const selected = tab.id === "company-" + company;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      byId(tab.getAttribute("aria-controls")).hidden = !selected;
    }
    document.body.dataset.company = company;
    if (push) {
      const url = new URL(location.href);
      if (company === "support") url.searchParams.set("company", company);
      else url.searchParams.delete("company");
      if (url.href !== location.href) history.pushState(null, "", url);
    }
  }

  const companyFromURL = () => new URLSearchParams(location.search).get("company") === "support" ? "support" : "marketplace";
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => showCompany(tab.id.slice("company-".length), true));
    tab.addEventListener("keydown", (event) => {
      const other = { ArrowLeft: 1 - index, ArrowRight: 1 - index, Home: 0, End: 1 }[event.key];
      if (other === undefined) return;
      event.preventDefault();
      tabs[other].focus();
      tabs[other].click();
    });
  });
  window.addEventListener("popstate", () => showCompany(companyFromURL()));
  showCompany(companyFromURL());

  const scopeName = (scope) => scope.queue ? scope.queue[0].toUpperCase() + scope.queue.slice(1) + " queue" : "Both queues";
  const hours = (value) => value + " virtual " + (value === 1 ? "hour" : "hours");

  function renderPreview() {
    byId("support-preview").hidden = !draft;
    byId("support-apply").disabled = posting || current.busy || current.phase !== "ready";
    if (!draft) return;
    const plan = current.cases[draft.caseId];
    byId("support-preview-title").textContent = (draft.replay ? "Replay preview · " : "Scenario preview · ") + plan.name;
    byId("support-apply").textContent = draft.replay ? "Apply replay to upcoming windows" : "Apply to upcoming windows";
    const details = fields([
      ["Effect", plan.effect], ["Affected traffic", scopeName(plan.scope)],
      ["Intake", "+" + percent.format(plan.volume - 1) + " across both queues"],
      ["Conditional resolution", percent.format(plan.resolution_probability) + " target"],
      ["Duration", hours(plan.duration_hours)],
    ]);
    if (plan.lead_in_hours) {
      details.append(element("dt", "Healthy lead-in"), element("dd", hours(plan.lead_in_hours)));
    }
    byId("support-plan").replaceChildren(details);
    byId("support-error").hidden = true;
  }

  function preview(caseId, replay) {
    draft = { caseId, replay };
    renderPreview();
    renderMap();
    byId("support-apply").focus();
  }

  function renderMap() {
    const running = current.runs.find((run) => run.status === "publishing" &&
      run.published >= run.scenario.lead_in_hours &&
      run.published < run.scenario.lead_in_hours + run.scenario.duration_hours);
    const phase = draft ? "preview" : running ? "running" : "";
    const plan = draft ? current.cases[draft.caseId] : running?.scenario;
    byId("support-map").dataset.phase = phase;
    const badge = byId("support-effect");
    badge.hidden = !plan;
    badge.dataset.phase = phase;
    badge.textContent = phase === "preview" ? "Preview" : "Running";
    for (const step of document.querySelectorAll("#support-map [data-step]")) {
      const affected = plan && (plan.regression ? step.dataset.step !== "received" : step.dataset.step === "received");
      step.toggleAttribute("data-affected", Boolean(affected));
    }
  }

  function outcomes(rows) {
    const wrapper = element("div", null, "workflow-table");
    const table = element("table");
    table.setAttribute("aria-label", "Observed outcomes in the scenario hour");
    const head = table.createTHead().insertRow();
    for (const label of ["Queue", "Received", "Eligible", "Resolved", "Retries", "Resolution rate"]) {
      const th = element("th", label);
      th.scope = "col";
      head.append(th);
    }
    const body = table.createTBody();
    for (const row of rows) {
      const line = body.insertRow();
      const label = element("th", scopeName({ queue: row.queue }));
      label.scope = "row";
      line.append(label);
      for (const value of [row.received, row.eligible, row.resolved, row.retries,
        row.eligible ? percent.format(row.resolved / row.eligible) : "—"]) line.append(element("td", value));
    }
    wrapper.append(table);
    return wrapper;
  }

  function renderTimeline() {
    const key = JSON.stringify([current.eye_url, current.runs]);
    if (key === timelineKey) return;
    timelineKey = key;
    const runs = current.runs.map((run) => {
      const article = element("article", null, "run");
      article.dataset.run = run.id;
      const evaluation = run.evaluation || {};
      article.append(element("h3", run.scenario.name));
      if (run.start_at) {
        article.append(element("p", time(run.start_at) + " → " + time(run.end_at), "support-run-meta"));
      }
      const status = { queued: "Queued", publishing: "Publishing", published: "Published", failed: "Publishing failed" };
      article.append(element("p", status[run.status] + (run.total ? ` · ${run.published}/${run.total} windows` : "")));
      if (evaluation.outcomes?.length) article.append(outcomes(evaluation.outcomes));
      if (evaluation.first_alert) {
        const alert = evaluation.first_alert;
        article.append(element("p", `Eye alert · ${alert.metric_name} · ${scopeName(alert.scope)} · ${time(evaluation.alert_at)}`));
        if (evaluation.incident_id && current.eye_url) {
          const link = element("a", "Open incident #" + evaluation.incident_id);
          link.href = current.eye_url + "?incident=" + evaluation.incident_id;
          article.append(link);
          if (evaluation.investigation_pending) article.append(element("p", "Investigation pending"));
          else if (evaluation.reasoning_provider) article.append(element("p", "Devin report available"));
        }
        if (evaluation.recovery_declared_at) {
          article.append(element("p", "Recovery recorded · " + time(evaluation.recovery_declared_at)));
        }
      } else if (evaluation.windows_checked) {
        article.append(element("p", "No resolution alert", run.scenario.regression ? null : "support-verification"));
      } else if (run.start_at && run.status !== "failed") {
        article.append(element("p", "Eye is checking the scenario windows."));
      }
      const replay = element("button", "Preview replay", "secondary");
      replay.type = "button";
      replay.addEventListener("click", () => preview(run.scenario.case, true));
      article.append(replay);
      return article;
    });
    byId("support-runs").replaceChildren(...(runs.length ? runs : [element("p", "Applied support cases appear here.")]));
  }

  for (const button of caseButtons) button.addEventListener("click", () => preview(button.dataset.supportPreview, false));
  byId("support-close-preview").addEventListener("click", () => {
    const opener = caseButtons.find((button) => button.dataset.supportPreview === draft?.caseId);
    draft = null;
    renderPreview();
    renderMap();
    opener?.focus();
  });
  byId("support-apply").addEventListener("click", async () => {
    if (!draft || posting) return;
    posting = true;
    byId("support-apply").disabled = true;
    const selected = draft;
    try {
      await post("/api/support/cases/" + selected.caseId);
      if (draft === selected) draft = null;
      byId("support-preview").hidden = !draft;
    } catch (error) {
      const banner = byId("support-error");
      banner.textContent = error.message;
      banner.hidden = false;
    } finally {
      posting = false;
      byId("support-apply").disabled = current.busy || current.phase !== "ready";
      renderMap();
    }
  });

  async function refreshSupport() {
    try {
      const response = await fetch("/api/support", { cache: "no-store" });
      if (!response.ok) throw new Error("Support status is temporarily unavailable.");
      current = await response.json();
      byId("support-status").textContent = current.error || phases[current.phase];
      byId("support-status-dot").classList.toggle("connected", current.phase === "ready");
      const eye = byId("support-eye");
      eye.hidden = !current.eye_url;
      if (current.eye_url) eye.href = current.eye_url;
      byId("support-clock").textContent = current.next_window
        ? "Next window " + time(current.next_window) + " · Seed " + current.seed : "Waiting for the baseline.";
      for (const button of caseButtons) button.disabled = !current.cases[button.dataset.supportPreview];
      byId("support-apply").disabled = posting || current.busy || current.phase !== "ready";
      renderMap();
      renderTimeline();
    } catch (error) {
      byId("support-status").textContent = error.message;
    } finally {
      setTimeout(refreshSupport, current.busy ? 500 : 1500);
    }
  }

  refreshSupport();
})();
