"""Serve the local walkthrough and evidence from a completed Devin-led replay."""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import comb, fsum, sqrt
from pathlib import Path
from statistics import pstdev
from urllib.parse import urlencode, urlsplit

HERE = Path(__file__).resolve().parent
EYE_PORT = 8010
LAB_PORT = 8012
REPLAY_EYE = "http://127.0.0.1:8047"
PREVIEW_LAB = "http://127.0.0.1:8042"
REPLAY_FILE = HERE.parent / ".runtime" / "feature-replay.json"
FILES = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/methods.js": ("methods.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
    "/stripe-timeout.json": ("stripe-timeout.json", "application/json"),
}


def evidence_id(row):
    return f"E{row['id']}"


def detection_details(alert_context):
    alert = alert_context["alert"]
    reference = alert["detection_reference"]
    history = [
        {"start": row["window_start"], "value": row["value"],
         "numerator": row["details"]["numerator"], "denominator": row["details"]["denominator"]}
        for row in alert_context["historical_readings"]
    ]
    observed, attempts = reference["numerator"], reference["denominator"]
    smoothed = (reference["baseline_numerator"] + .5) / (reference["baseline_denominator"] + 1)
    tail = fsum(comb(attempts, count) * smoothed ** count * (1 - smoothed) ** (attempts - count)
                 for count in range(observed + 1))
    return {
        "history": history,
        "coverage": alert_context["coverage"],
        "population_sd": pstdev(row["value"] for row in history),
        "sampling_noise": sqrt(alert["expected"] * (1 - alert["expected"]) / attempts),
        "smoothed_probability": smoothed,
        "lower_tail": tail,
    }


def replay_summary(replay):
    incident = replay["final"]["incident"]
    report = incident["report"]
    rows = replay["final"]["evidence"]
    contexts = {row["payload"]["tool"]: row for row in rows if row["kind"] == "investigation context"}
    reads = [row for row in rows if row["kind"] == "investigation read"]
    cited = set(report["evidence_ids"] + report["alternative"]["evidence_ids"])
    alert_context = contexts["alert"]["payload"]["result"]
    trigger = alert_context["alert"]
    graph = contexts["graph_neighbors"]["payload"]["result"]
    labels = {item["id"]: item["label"] for item in graph["neighbors"]}
    scoped_mix = next(row for row in reads if row["payload"]["tool"] == "activity_mix"
                      and row["payload"]["result"]["scope"] == incident["scope"])
    timeouts = next(item for item in scoped_mix["payload"]["result"]["activities"]
                    if item["activity"] == "payment_timed_out")
    sample_read = next(row for row in reads if row["payload"]["tool"] == "supporting_events")
    sample = next(event for event in reversed(sample_read["payload"]["result"])
                  if event["activity"] == "payment_timed_out")
    workflow = next(row for row in reads if row["payload"]["tool"] == "graph_neighbors")
    workflow_edge = next(edge for edge in workflow["payload"]["result"]["edges"]
                         if edge["source"] == "activity:payment_attempted"
                         and edge["target"] == "activity:payment_timed_out" and edge["kind"] == "precedes")
    recovery = [
        {"id": evidence_id(row), "window_start": row["window_start"], "value": row["payload"]["value"],
         "expected": row["payload"]["expected"], "sample_size": row["payload"]["sample_size"]}
        for row in rows if row["kind"] == "recovery observation"
    ][-incident["healthy_windows"]:]
    conclusion = next(row for row in rows if row["kind"] == "investigation conclusion")
    query = urlencode({
        "start": trigger["window_start"], "end": trigger["window_end"],
        "as_of": trigger["observed_at"], "scope": json.dumps(incident["scope"]),
    })
    return {
        "seed": replay["seed"],
        "incident": incident["id"],
        "observed_at": trigger["observed_at"],
        "opened_pending": replay["at_detection"]["pending"],
        "recovered_pending": replay["at_recovery"]["pending"],
        "recovered_at": report["recovery_observed_at"],
        "dashboard": f"{REPLAY_EYE}/?incident={incident['id']}",
        "lab_preview": PREVIEW_LAB + "/#chat-title",
        "readings_url": f"{REPLAY_EYE}/api/catalog/{incident['metric_id']}/readings?{query}",
        "alert": trigger,
        "detection": detection_details(alert_context),
        "grouping": [
            {"id": evidence_id(row), **row["payload"]["grouping_evidence"]}
            for row in replay["at_detection"]["evidence"] if row["kind"] == "related anomaly"
            and isinstance(row["payload"].get("grouping_evidence"), dict)
        ],
        "metric": alert_context["metric_definition"],
        "alert_evidence": evidence_id(contexts["alert"]),
        "graph_evidence": evidence_id(contexts["graph_neighbors"]),
        "workflow_evidence": evidence_id(workflow),
        "workflow_edge": workflow_edge,
        "graph_links": [{**edge, "label": labels.get(edge["target"], edge["target"])} for edge in graph["edges"]],
        "sample": {**sample, "evidence_id": evidence_id(sample_read)},
        "timeouts": {**timeouts, "evidence_id": evidence_id(scoped_mix)},
        "recovery": recovery,
        "reads": [{
            "id": evidence_id(row), "tool": row["payload"]["tool"], "title": row["payload"]["title"],
            "params": row["payload"]["params"], "reason": row["payload"]["interpretation"],
            "result": row["payload"]["result"],
            "cited": evidence_id(row) in cited,
            "available": bool(row["payload"].get("result"))
            and not (isinstance(row["payload"]["result"], dict)
                     and (row["payload"]["result"].get("available") is False or "error" in row["payload"]["result"])),
        } for row in reads],
        "conclusion_evidence": evidence_id(conclusion),
        "continuations": report["continuations"],
        "session_count": len(report["reasoning_sessions"]),
        "citations": report["evidence_ids"],
        "alternative": report["alternative"],
        "stop_reason": report["stop_reason"],
        "report_url": f"{REPLAY_EYE}/api/incidents/{incident['id']}/original-report",
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path in FILES:
            name, content_type = FILES[path]
            self.reply((HERE / name).read_bytes(), content_type)
            return
        try:
            replay = json.loads(REPLAY_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.send_error(503, "Recorded replay unavailable")
            return
        if path == "/api/replay":
            self.reply_json(replay_summary(replay))
            return
        if path == "/evidence/recovery":
            self.reply_json({
                "report_pending_at_recovery": replay["at_recovery"]["pending"],
                "last_observed_at": replay["at_recovery"]["last_observed_at"],
                "checks": [row for row in replay["at_recovery"]["evidence"] if row["kind"] == "recovery observation"],
            })
            return
        if match := re.fullmatch(r"/evidence/(E\d+)", path):
            row = next((row for row in replay["final"]["evidence"] if evidence_id(row) == match[1]), None)
            if row:
                self.reply_json(row)
                return
        self.send_error(404)

    def reply_json(self, data):
        self.reply(json.dumps(data, indent=2).encode(), "application/json")

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
