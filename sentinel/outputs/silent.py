"""5a. Silent mode: subscribe to FINAL_REPORT only and deliver it.

Every report is written to outbox/<incident>_<kind>.md. Delivery to Slack or e-mail happens only when
configured through environment variables (nothing is sent by default):
  SLACK_WEBHOOK_URL                               -> Slack incoming webhook
  SMTP_HOST, SMTP_PORT, ALERT_EMAIL_FROM, ALERT_EMAIL_TO, (SMTP_USER, SMTP_PASSWORD) -> e-mail
"""
from __future__ import annotations

import json
import os
import smtplib
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from sentinel.bus import BusEvent, EventBus


class SilentNotifier:
    def __init__(self, bus: EventBus, outbox: str | Path = "outbox", echo: bool = True):
        self.outbox = Path(outbox)
        self.outbox.mkdir(parents=True, exist_ok=True)
        for f in self.outbox.glob("*.md"):
            f.unlink()
        self.echo = echo
        self.delivered: list[dict] = []
        bus.subscribe(self.on_report, types=["FINAL_REPORT"])

    def on_report(self, ev: BusEvent):
        p = ev.payload
        title = (f"[{p.get('kind', 'report').upper()}] {p['id']} {p.get('label', '')}: {p.get('first_cause')} "
                 f"in {p.get('segment') or 'all traffic'}")
        path = self.outbox / f"{p['id']}_{p.get('kind', 'report')}.md"
        path.write_text(f"# {title}\n\n_simulated time {ev.sim_ts}_\n\n{p.get('report_md', '')}\n", encoding="utf-8")
        channels = ["file"]
        if os.environ.get("SLACK_WEBHOOK_URL"):
            self._slack(title, p.get("report_md", ""))
            channels.append("slack")
        if os.environ.get("SMTP_HOST") and os.environ.get("ALERT_EMAIL_TO"):
            self._email(title, p.get("report_md", ""))
            channels.append("email")
        self.delivered.append({"title": title, "path": str(path), "channels": channels})
        if self.echo:
            print(f"  📣 {ev.sim_ts} {title}  -> {', '.join(channels)} ({path})")

    @staticmethod
    def _slack(title: str, body: str):
        data = json.dumps({"text": f"*{title}*\n{body[:3500]}"}).encode()
        req = urllib.request.Request(os.environ["SLACK_WEBHOOK_URL"], data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()

    @staticmethod
    def _email(title: str, body: str):
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, os.environ.get("ALERT_EMAIL_FROM", "sentinel"), os.environ["ALERT_EMAIL_TO"]
        msg.set_content(body)
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=15) as s:
            if os.environ.get("SMTP_USER"):
                s.starttls()
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
