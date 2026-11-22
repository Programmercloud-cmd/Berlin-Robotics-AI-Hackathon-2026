"""5a. Silent mode: no screen; the agent works in the background and reports by e-mail.

During the run every FINAL_REPORT is written to outbox/<incident>_<kind>.md (and, with --email-each, mailed
at once). When the run ends, one digest e-mail goes out: what the agent watched, what it paged, every alert
with its first cause, segment, label and impact, and the full reports as attachments.

Nothing is sent unless it is configured (environment variables; credentials never live in the repository):
  SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD   the mail server (STARTTLS when a user is set;
                                                         SMTP_SSL=1 for implicit TLS, e.g. port 465)
  ALERT_EMAIL_FROM                                       sender address (defaults to SMTP_USER)
  ALERT_EMAIL_TO                                         recipient(s), comma-separated (or --email-to)
  SLACK_WEBHOOK_URL                                      optional Slack incoming webhook
Every delivery is also published on the bus (NOTIFY) so the live UI can show it.
"""
from __future__ import annotations

import html
import json
import os
import smtplib
import ssl
import urllib.request
from collections import Counter
from email.message import EmailMessage
from pathlib import Path

from sentinel.bus import BusEvent, EventBus

LABEL_TEXT = {"supported_cause": "Supported cause", "hypothesis": "Likely cause", "explained": "Explained",
              "data_issue": "Data issue", "unexplained": "Unexplained"}


def read_outbox(folder: str | Path) -> list[dict]:
    """The mails silent mode produced (digest first, then alerts), for the mailbox view."""
    out = []
    for f in sorted(Path(folder).glob("*.md")):
        text = f.read_text(encoding="utf-8")
        kind = "digest" if f.name == "digest.md" else ("alert" if f.name.endswith("_alert.md") else "report")
        first = text.splitlines()[0].lstrip("# ").strip() if text else f.name
        sim = next((l.strip("_ ").replace("simulated time ", "") for l in text.splitlines()[:4] if "simulated time" in l), "")
        out.append({"name": f.name, "kind": kind, "subject": first, "sim_ts": sim, "body": text})
    out.sort(key=lambda m: (m["kind"] != "digest", m["name"]))
    return out


def email_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("ALERT_EMAIL_TO"))


def masked_recipients() -> str:
    out = []
    for a in (os.environ.get("ALERT_EMAIL_TO") or "").split(","):
        a = a.strip()
        if "@" in a:
            name, dom = a.split("@", 1)
            out.append(f"{name[:2]}***@{dom}")
    return ", ".join(out)


class SilentNotifier:
    def __init__(self, bus: EventBus, outbox: str | Path = "outbox", echo: bool = True, email_each: bool = False):
        self.bus = bus
        self.outbox = Path(outbox)
        self.outbox.mkdir(parents=True, exist_ok=True)
        for f in self.outbox.glob("*.md"):
            f.unlink()
        self.echo, self.email_each = echo, email_each
        self.delivered: list[dict] = []
        self.reports: list[dict] = []
        bus.subscribe(self.on_report, types=["FINAL_REPORT"])

    # ------------------------------------------------------------------ during the run
    def on_report(self, ev: BusEvent):
        p = ev.payload
        title = (f"[{p.get('kind', 'report').upper()}] {p['id']} {LABEL_TEXT.get(p.get('label'), p.get('label', ''))}: "
                 f"{p.get('first_cause')} in {p.get('segment') or 'all traffic'}")
        path = self.outbox / f"{p['id']}_{p.get('kind', 'report')}.md"
        path.write_text(f"# {title}\n\n_simulated time {ev.sim_ts}_\n\n{p.get('report_md', '')}\n", encoding="utf-8")
        self.reports.append({**p, "sim_ts": ev.sim_ts, "title": title, "path": str(path)})
        channels = ["file"]
        if os.environ.get("SLACK_WEBHOOK_URL") and p.get("kind") == "alert":
            self._try(self._slack, "slack", channels, title, p.get("report_md", ""))
        if self.email_each and email_configured() and p.get("kind") == "alert":
            self._try(self._send, "email", channels, title, p.get("report_md", ""), None, [])
        self.delivered.append({"title": title, "path": str(path), "channels": channels})
        self.bus.publish("NOTIFY", "silent", {"what": "report", "id": p["id"], "kind": p.get("kind"),
                                              "title": title, "channels": channels,
                                              "to": masked_recipients() if "email" in channels else ""}, p["id"])
        if self.echo:
            print(f"  📣 {ev.sim_ts} {title}  -> {', '.join(channels)} ({path})")

    def _try(self, fn, name, channels, *args):
        try:
            fn(*args)
            channels.append(name)
        except Exception as ex:                                       # noqa: BLE001 - never stop the agent
            self.bus.publish("NOTIFY", "silent", {"what": "error", "channel": name, "error": f"{type(ex).__name__}: {ex}"[:300]})
            if self.echo:
                print(f"  ⚠ {name} delivery failed: {type(ex).__name__}: {ex}")

    # ------------------------------------------------------------------ after the run: the digest
    def finish(self, runtime, started: str, ended: str) -> dict:
        """One e-mail with the whole run: summary, every alert, full reports attached."""
        incs = list(runtime.incidents.incidents.values())
        alerts = [r for r in self.reports if r.get("kind") == "alert"]
        labels = Counter(i.label for i in incs if i.label)
        ticks = [e for e in runtime.bus.history if e.type == "MONITOR_TICK"]
        watched = sum(e.payload.get("hours_evaluated", 0) for e in ticks)
        anomalies = sum(1 for e in runtime.bus.history if e.type == "ANOMALY_DETECTED")
        mutations = sum(1 for e in runtime.bus.history if e.type == "GRAPH_MUTATION")
        subject = (f"Sentinel run report {started[:16]} - {ended[:16]}: {len(alerts)} alert(s), "
                   f"{len(incs)} incident(s)")
        lines = [f"# {subject}", "",
                 f"- simulated window: {started} -> {ended}",
                 f"- series-hours evaluated: {watched:,}; anomalies: {anomalies}; incidents: {len(incs)}; "
                 f"graph mutations: {mutations}",
                 "- outcome: " + ", ".join(f"{LABEL_TEXT.get(k, k)} {v}" for k, v in labels.most_common()), "",
                 "## Alerts", ""]
        for r in alerts:
            lines.append(f"- **{r['id']}** {r['sim_ts'][:16]} · {LABEL_TEXT.get(r.get('label'), r.get('label'))} · "
                         f"first cause `{r.get('first_cause')}` in {r.get('segment') or 'all traffic'} · "
                         f"{r.get('impact_eur_per_h', 0):,.0f} EUR/h")
        if not alerts:
            lines.append("- none: nothing harmful enough to page")
        quiet = [i for i in incs if i.id not in {r["id"] for r in alerts}]
        if quiet:
            lines += ["", "## Investigated without paging", ""]
            for i in quiet[:30]:
                lines.append(f"- {i.id} {LABEL_TEXT.get(i.label, i.label)} · {i.first_cause} in "
                             f"{i.segment or 'all traffic'} · {i.state}")
        body = "\n".join(lines) + "\n"
        (self.outbox / "digest.md").write_text(body, encoding="utf-8")
        attachments = [(Path(r["path"]).name, Path(r["path"]).read_text(encoding="utf-8")) for r in alerts]
        channels = ["file"]
        if email_configured():
            self._try(self._send, "email", channels, subject, body, _html(subject, lines), attachments)
        self.bus.publish("NOTIFY", "silent", {"what": "digest", "title": subject, "channels": channels,
                                              "alerts": len(alerts), "incidents": len(incs),
                                              "to": masked_recipients() if "email" in channels else ""})
        if self.echo:
            print(f"  ✉ digest: {subject} -> {', '.join(channels)}"
                  + (f" ({masked_recipients()})" if "email" in channels else
                     "" if email_configured() else "  (e-mail not configured: set SMTP_HOST and ALERT_EMAIL_TO)"))
        return {"subject": subject, "channels": channels, "path": str(self.outbox / "digest.md")}

    # ------------------------------------------------------------------ transports
    @staticmethod
    def _slack(title: str, body: str):
        data = json.dumps({"text": f"*{title}*\n{body[:3500]}"}).encode()
        req = urllib.request.Request(os.environ["SLACK_WEBHOOK_URL"], data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()

    @staticmethod
    def _send(subject: str, text: str, html_body: str | None, attachments: list[tuple[str, str]]):
        msg = EmailMessage()
        sender = os.environ.get("ALERT_EMAIL_FROM") or os.environ.get("SMTP_USER") or "sentinel@localhost"
        msg["Subject"], msg["From"], msg["To"] = subject, sender, os.environ["ALERT_EMAIL_TO"]
        msg.set_content(text)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        for name, content in attachments:
            msg.add_attachment(content.encode("utf-8"), maintype="text", subtype="markdown", filename=name)
        host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
        if os.environ.get("SMTP_SSL") == "1":
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as s:
                if os.environ.get("SMTP_USER"):
                    s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
                s.send_message(msg)
            return
        with smtplib.SMTP(host, port, timeout=20) as s:
            if os.environ.get("SMTP_USER"):
                s.starttls(context=ssl.create_default_context())
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)


def _html(subject: str, lines: list[str]) -> str:
    """A small, readable HTML version of the digest (inline styles: mail clients ignore style sheets)."""
    items, out = [], []
    for ln in lines[1:]:
        if ln.startswith("## "):
            if items:
                out.append("<ul style='padding-left:18px;margin:6px 0 14px'>" + "".join(items) + "</ul>")
                items = []
            out.append(f"<h3 style='font:600 14px system-ui;margin:16px 0 4px;color:#1f2937'>{html.escape(ln[3:])}</h3>")
        elif ln.startswith("- "):
            t = html.escape(ln[2:])
            t = t.replace("**", "")
            items.append(f"<li style='margin:3px 0'>{t}</li>")
    if items:
        out.append("<ul style='padding-left:18px;margin:6px 0 14px'>" + "".join(items) + "</ul>")
    return (f"<div style='font:14px/1.5 system-ui,Segoe UI,sans-serif;color:#111827;max-width:720px'>"
            f"<div style='font:700 18px system-ui;margin:0 0 6px'>{html.escape(subject)}</div>"
            f"<div style='color:#6b7280;font-size:12px;margin-bottom:10px'>Sentinel · silent mode · "
            f"full reports attached</div>{''.join(out)}</div>")
