"""Serve the local Eye of the System rehearsal and its read-only evidence."""

import json
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
EYE = "http://127.0.0.1:8000"
LAB = "http://127.0.0.1:8002"
PLAN = json.loads((HERE / "stripe-timeout.json").read_text())
SCOPE = {"country": "DE", "platform": "mobile", "payment_provider": "Stripe"}
METRIC = "payment_timeout_rate"
METRIC_NODE = f"metric:{METRIC}"
RUNTIME = HERE.parent / ".runtime"
FILES = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
    "/stripe-timeout.json": ("stripe-timeout.json", "application/json"),
}


def read_json(base, path):
    try:
        with urlopen(base + path, timeout=5) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None


def replay_run(lab):
    if not lab or lab["seed"] != 42:
        return None
    for run in lab["runs"]:
        plan = run.get("scenario") or {}
        if run["status"] != "failed" and all(
            plan.get(key) == value for key, value in PLAN.items() if key != "premise"
        ):
            return run
    return None


def matching_incident(incidents, run):
    if not run or not run.get("start_at"):
        return None
    start, end = (datetime.fromisoformat(run[key]) for key in ("start_at", "end_at"))
    matches = []
    for incident in incidents:
        if incident["metric_id"] != "payment_success_rate" or incident["scope"] != SCOPE:
            continue
        observed = incident["report"].get("observation_window", {}).get("window_end")
        if observed and start < datetime.fromisoformat(observed) <= end:
            matches.append(incident)
    return min(matches, key=lambda incident: incident["id"], default=None)


def graph_details(graph):
    labels = {node["id"]: node["label"] for node in graph["nodes"]}
    edges = graph["edges"]
    workflow = next((
        edge for edge in edges
        if edge["source"] == "activity:payment_attempted"
        and edge["target"] == "activity:payment_succeeded"
        and edge["provenance"] == "pm4py.ocdfg"
    ), None)
    metric_links = [
        {**edge, "target_label": labels.get(edge["target"], edge["target"])}
        for edge in edges if edge["source"] == METRIC_NODE and edge["provenance"] != "pearson"
    ]
    return {"workflow": workflow, "registered": METRIC_NODE in labels, "metric_links": metric_links}


def replay_capture(name, run):
    try:
        capture = json.loads((RUNTIME / name).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return capture if capture.get("run_id") == run["id"] else {}


def incident_details(incident, run):
    identifier = incident["id"]
    report = incident["report"]
    path = f"/api/incidents/{identifier}"
    rows = read_json(EYE, path + "/evidence") or []
    original = read_json(EYE, path + "/original-report") if not incident["investigation_pending"] else None
    matched_events = [
        event for row in rows if row["kind"] == "observed events" and isinstance(row["payload"], list)
        for event in row["payload"] if event["activity"] == "payment_timed_out"
        and all(event["attrs"].get(key) == value for key, value in SCOPE.items())
    ]
    alert = next((
        row["payload"] for row in rows
        if row["kind"] == "anomaly" and row["payload"].get("metric_id") == "payment_success_rate"
        and row["payload"].get("scope") == SCOPE
    ), None)
    recovered = incident["recovered_at"]
    final_checks = []
    if recovered and incident["healthy_windows"]:
        recovered_start = datetime.fromisoformat(recovered)
        final_checks = sorted((
            row for row in rows
            if row["kind"] == "recovery observation" and row["node_id"] == "metric:payment_success_rate"
            and row["payload"].get("scope") == SCOPE
            and datetime.fromisoformat(row["window_start"]) <= recovered_start
        ), key=lambda row: datetime.fromisoformat(row["window_start"]))[-incident["healthy_windows"]:]
    receipt = replay_capture("registration.json", run)
    if receipt.get("incident_id") != identifier:
        receipt = {}
    return {
        "id": identifier, "break": report.get("break"), "detection": report.get("observation_window"),
        "trigger": alert,
        "alert_written_at": (alert or {}).get("detected_at"),
        "report_written_at": (original or {}).get("written_at"),
        "report_pending": incident["investigation_pending"],
        "reasoning_provider": report.get("reasoning_provider"), "reasoning": report.get("reasoning"),
        "reasoning_session_id": report.get("reasoning_session_id"),
        "branches": report.get("evidence_branches", []),
        "uncertainty": report.get("uncertainty"),
        "recovered_at": incident.get("recovery_observed_at") or recovered,
        "healthy_windows": incident["healthy_windows"], "recovery": report.get("recovery"),
        "final_checks": [{"window_start": row["window_start"], **row["payload"]} for row in final_checks],
        "event": matched_events[0] if matched_events else None,
        "registration": {
            key: receipt[key] for key in ("captured_at", "registered_before", "registered_at", "payment_report_pending")
            if key in receipt
        },
    }


def snapshot():
    lab = read_json(LAB, "/api/lab")
    state = read_json(EYE, "/api/state")
    run = replay_run(lab)
    incidents = state["incidents"] if state else []
    if run and state and (identifier := run.get("evaluation", {}).get("incident_id")):
        if all(incident["id"] != identifier for incident in incidents):
            focused = read_json(EYE, f"/api/state?incident={identifier}")
            if focused:
                incidents = focused["incidents"]
    incident = matching_incident(incidents, run)
    result = {
        "lab_status": lab["phase"] if lab else "offline", "eye_online": state is not None,
        "graph": graph_details(state["graph"]) if state else None,
        "run": {key: run.get(key) for key in ("id", "status", "start_at", "end_at", "published", "evaluation")}
        if run else None,
        "incident": incident_details(incident, run) if incident else None,
    }
    if run and run.get("published"):
        workflow = read_json(LAB, f"/api/runs/{run['id']}/workflow")
        result["counts"] = (workflow or {}).get("counts")
        result["event_capture"] = replay_capture("replay-event.json", run)
    if run and run.get("start_at") and result["graph"] and result["graph"]["registered"]:
        as_of = datetime.fromisoformat(run["end_at"]) + timedelta(minutes=15)
        query = urlencode({
            "start": run["start_at"], "end": run["end_at"], "as_of": as_of.isoformat(), "scope": json.dumps(SCOPE),
        })
        result["readings_url"] = f"{EYE}/api/catalog/{METRIC}/readings?{query}"
    return result


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/replay":
            self.reply(json.dumps(snapshot()).encode(), "application/json")
        elif self.path == "/api/replay/trigger":
            incident = snapshot()["incident"]
            if not incident or not incident["trigger"]:
                self.send_error(404, "Replay trigger pending")
                return
            source = f"{EYE}/api/incidents/{incident['id']}/evidence?alerts_only=true"
            self.reply(json.dumps({"source": source, "trigger": incident["trigger"]}, indent=2).encode(), "application/json")
        elif self.path in FILES:
            name, content_type = FILES[self.path]
            self.reply((HERE / name).read_bytes(), content_type)
        else:
            self.send_error(404)

    def reply(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", 8003), Handler) as server:
        print("Eye rehearsal · http://127.0.0.1:8003/", flush=True)
        server.serve_forever()
