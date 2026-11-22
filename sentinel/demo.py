"""Screen-recordable demo: record the agent once, replay it in the UI at a comfortable pace.

  python -m sentinel.demo record            run the agent unattended on day one until 17:00 (about 2 min, once)
  python -m sentinel.demo play              replay it in the UI: http://127.0.0.1:8765 (2-3 min, no computation)
  python -m sentinel.demo play --pace 1.5   slower (1.5 x the default seconds per interesting hour)
  python -m sentinel.demo play --email-to you@example.com    e-mail the digest when the replay ends
                                                             (SMTP_* environment variables, see outputs/silent.py)

Recording stores everything the agent produced (state/demo/): the bus events, the incidents with their reports,
the monitored series and the silent-mode digest. Playing serves the same page as the live mode (outputs/ui.html)
and feeds it the recorded events in simulated-time order: the quiet night hours are fast-forwarded, every hour
with something to see is played slowly, and the replay pauses after the alert.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
import warnings
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "state" / "demo"
UNTIL = "2026-10-26T17:00"          # the payment fault starts at 15:00 and is paged at 16:00
PLAY_FROM = "2026-10-26 07:00"      # before this hour the replay fast-forwards (quiet night)
INTERESTING = {"ANOMALY_DETECTED", "INNOCENT_CHANGE", "DATA_ISSUE", "INCIDENT_OPENED", "FINAL_REPORT", "SOFT_ALERT",
               "EARLY_WARNING", "LOCALISATION_CLUE", "DEAD_END", "GRAPH_MUTATION"}


# ---------------------------------------------------------------- record
def record(until: str = UNTIL):
    from types import SimpleNamespace
    from sentinel.outputs.silent import SilentNotifier
    from sentinel.run import LIVE_START, Runtime
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    print("building the graph, validating it and fitting baselines (about 45 s) …", flush=True)
    rt = Runtime(SimpleNamespace(no_meta=False, events_path=str(OUT / "events_live.jsonl")))
    notifier = SilentNotifier(rt.bus, outbox=OUT / "outbox", echo=True)
    print(f"running the agent {LIVE_START} -> {until} …", flush=True)
    rt.run(pd.Timestamp(until), 0.0, None)
    notifier.finish(rt, str(LIVE_START), str(pd.Timestamp(until)))
    (OUT / "events.jsonl").write_text("\n".join(e.to_json() for e in rt.bus.history) + "\n", encoding="utf-8")
    incs = [json.loads(i.model_dump_json()) for i in rt.incidents.incidents.values()]
    (OUT / "incidents.json").write_text(json.dumps(incs, indent=1, default=str), encoding="utf-8")
    series = {f"{m}|{s}": rows for (m, s), rows in rt.monitor.series.items()}
    (OUT / "series.json").write_text(json.dumps(series, default=str), encoding="utf-8")
    print(f"recorded {len(rt.bus.history)} events and {len(incs)} incidents in {time.time() - t0:.0f}s -> {OUT}")


# ---------------------------------------------------------------- play
class Replay:
    def __init__(self, pace: float, hour_s: float, quiet_s: float, pause_s: float):
        self.events = [json.loads(l) for l in (OUT / "events.jsonl").read_text(encoding="utf-8").splitlines() if l]
        self.incidents = json.loads((OUT / "incidents.json").read_text(encoding="utf-8"))
        self.series = json.loads((OUT / "series.json").read_text(encoding="utf-8"))
        self.hour_s, self.quiet_s, self.pause_s = hour_s * pace, quiet_s * pace, pause_s * pace
        self.clock = ""                 # simulated time of the last event sent
        self.sent = 0
        self.finished = False
        # when each incident becomes visible, and when its report is final
        self.opened = {e["incident_id"]: e["sim_ts"] for e in self.events if e["type"] == "INCIDENT_OPENED"}
        self.concluded = {}
        for e in self.events:
            if e["type"] == "THOUGHT" and (e.get("payload") or {}).get("stage") == "conclude":
                self.concluded.setdefault(e["incident_id"], e["seq"])

    def hours(self):
        """Events grouped by simulated hour, in order."""
        cur, group = None, []
        for e in self.events:
            h = (e.get("sim_ts") or "")[:13]
            if h != cur and group:
                yield cur, group
                group = []
            cur = h
            group.append(e)
        if group:
            yield cur, group

    async def stream(self):
        self.finished = False
        for hour, group in self.hours():
            fast = (hour or "") < PLAY_FROM[:13]
            interesting = [e for e in group if e["type"] in INTERESTING]
            budget = 0.0 if fast else (self.hour_s if interesting else self.quiet_s)
            # spend the hour's time on the events worth watching; the rest flows by quickly
            weights = [3.0 if e["type"] in INTERESTING or e["type"] in ("THOUGHT", "HYPOTHESIS", "OBSERVATION")
                       and e.get("incident_id") else 0.15 for e in group]
            total = sum(weights) or 1.0
            for e, w in zip(group, weights):
                self.clock, self.sent = e.get("sim_ts") or self.clock, e["seq"] + 1
                yield f"id: {e['seq']}\ndata: {json.dumps(e)}\n\n"
                if budget:
                    await asyncio.sleep(budget * w / total)
                if e["type"] == "FINAL_REPORT" and (e.get("payload") or {}).get("kind") == "alert" and not fast:
                    await asyncio.sleep(self.pause_s)      # let the audience read the alert
        self.finished = True
        yield "event: done\ndata: {}\n\n"

    def visible_incidents(self):
        out = []
        for inc in self.incidents:
            ts = self.opened.get(inc["id"])
            if ts is None or ts > self.clock:
                continue
            inc = dict(inc)
            if self.concluded.get(inc["id"], 10 ** 9) >= self.sent:     # still investigating
                inc.update(report_md="", label=None, hypotheses=[], state="investigating")
            out.append(inc)
        return out


def play(port: int, pace: float, hour_s: float, quiet_s: float, pause_s: float, email_to: str | None):
    import os
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from sentinel.outputs.silent import email_configured, masked_recipients
    if not (OUT / "events.jsonl").exists():
        sys.exit("nothing recorded yet: run `python -m sentinel.demo record` first")
    if email_to:
        os.environ["ALERT_EMAIL_TO"] = email_to
    rp = Replay(pace, hour_s, quiet_s, pause_s)
    page = (Path(__file__).parent / "outputs" / "ui.html").read_text(encoding="utf-8")
    app = FastAPI(title="Sentinel demo")
    mailed = {"done": False}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return page

    workflow = (Path(__file__).parent / "outputs" / "workflow.html")

    @app.get("/workflow", response_class=HTMLResponse)
    def workflow_page():
        return workflow.read_text(encoding="utf-8")

    @app.get("/api/events")
    def all_events():
        return JSONResponse(rp.events)

    @app.get("/mail", response_class=HTMLResponse)
    def mail_page():
        return (Path(__file__).parent / "outputs" / "mail.html").read_text(encoding="utf-8")

    @app.get("/api/outbox")
    def outbox():
        from sentinel.outputs.silent import read_outbox
        mails = read_outbox(OUT / "outbox")
        alert_ids = {e["payload"]["id"] for e in rp.events if e["type"] == "FINAL_REPORT" and e["seq"] < rp.sent}
        if not rp.finished:            # during the replay only what has been "sent" so far is in the mailbox
            mails = [m for m in mails if m["kind"] != "digest" and m["name"].split("_")[0] in alert_ids]
        return JSONResponse({"mails": mails, "email": {"configured": email_configured(), "to": masked_recipients()},
                             "finished": rp.finished})

    @app.get("/api/stream")
    async def stream():
        async def gen():
            async for chunk in rp.stream():
                yield chunk
            if not mailed["done"] and email_configured():
                mailed["done"] = True
                threading.Thread(target=send_digest, daemon=True).start()
            while True:
                await asyncio.sleep(30)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/incidents")
    def incidents():
        return JSONResponse(rp.visible_incidents())

    @app.get("/api/series")
    def series(metric: str, segment: str = ""):
        rows = [r for r in rp.series.get(f"{metric}|{segment}", []) if str(r.get("ts", "")) <= rp.clock]
        return JSONResponse(rows[-72:])

    @app.get("/api/run")
    def run_status():
        return JSONResponse({"sim_ts": rp.clock, "finished": rp.finished, "events": rp.sent,
                             "email": {"configured": email_configured(), "to": masked_recipients()}})

    def send_digest():
        from sentinel.outputs.silent import SilentNotifier
        body = (OUT / "outbox" / "digest.md").read_text(encoding="utf-8")
        subject = body.splitlines()[0].lstrip("# ").strip()
        atts = [(p.name, p.read_text(encoding="utf-8")) for p in sorted((OUT / "outbox").glob("*_alert.md"))]
        try:
            from sentinel.outputs.silent import _html
            SilentNotifier._send(subject, body, _html(subject, body.splitlines()), atts)
            print(f"  ✉ digest e-mailed to {masked_recipients()}", flush=True)
        except Exception as ex:                                           # noqa: BLE001
            print(f"  ⚠ digest e-mail failed: {type(ex).__name__}: {ex}", flush=True)

    print(f"demo ready: http://127.0.0.1:{port}  (architecture view)   http://127.0.0.1:{port}/workflow  "
          f"(workflow view); start recording, then reload the page to replay", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main(argv=None):
    warnings.filterwarnings("ignore")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    p = argparse.ArgumentParser(prog="sentinel.demo")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="run the agent once and store everything it produced")
    r.add_argument("--until", default=UNTIL)
    q = sub.add_parser("play", help="replay the recording in the UI")
    q.add_argument("--port", type=int, default=8765)
    q.add_argument("--pace", type=float, default=1.0, help="multiplies every delay (1.5 = 50 %% slower)")
    q.add_argument("--hour-seconds", type=float, default=12.0, help="seconds for an hour with something to see")
    q.add_argument("--quiet-seconds", type=float, default=1.0, help="seconds for an hour with nothing to see")
    q.add_argument("--pause-seconds", type=float, default=6.0, help="pause after the alert")
    q.add_argument("--email-to", default=None, help="e-mail the digest when the replay ends (needs SMTP_*)")
    a = p.parse_args(argv)
    if a.cmd == "record":
        record(a.until)
    else:
        play(a.port, a.pace, a.hour_seconds, a.quiet_seconds, a.pause_seconds, a.email_to)


if __name__ == "__main__":
    main()
