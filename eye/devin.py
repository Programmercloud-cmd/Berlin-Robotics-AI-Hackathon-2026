"""Run one disposable Devin API v3 session per reasoning request."""

import json
import logging
import time
from urllib.parse import quote

import httpx

from eye.scenario import ScenarioReply

log = logging.getLogger(__name__)
SESSION_TIMEOUT = 480
POLL_INTERVAL = 10

TEXT = {"type": "string", "minLength": 1}
REPORT_FIELDS = {
    "summary": TEXT, "likely_causes": {"type": "array", "items": TEXT, "minItems": 1},
    "impact": TEXT, "uncertainty": TEXT,
}
RELATION_FIELDS = {
    "target": TEXT, "kind": {"enum": ["measures", "depends_on", "may_influence"]}, "reason": TEXT,
}
VERDICT = {"enum": ["supported", "partial", "unsupported", "insufficient"]}


def object_schema(fields):
    return {
        "type": "object", "properties": fields, "required": list(fields),
        "additionalProperties": False,
    }


ROLES = {
    "scenario": ("test lab scenario", ScenarioReply.model_json_schema()),
    "investigation": ("incident investigation", object_schema(REPORT_FIELDS)),
    "benchmark_review": ("benchmark report review", object_schema({
        "reviews": {"type": "array", "items": object_schema({
            "id": TEXT, "observable_break": VERDICT, "scope": VERDICT, "evidence": VERDICT,
            "unsupported_claims": {"type": "array", "items": TEXT}, "notes": TEXT,
        })},
    })),
    "curation": ("graph curation", object_schema({
        "relationships": {
            "type": "array", "maxItems": 8, "items": object_schema(RELATION_FIELDS),
        },
    })),
}


class DevinReasoner:
    def __init__(self, key, org_id, mode=None):
        self.key = key
        self.sessions = f"/v3/organizations/{quote(org_id, safe='')}/sessions"
        self.mode = mode

    def reason(self, role, system, context):
        label, schema = ROLES[role]
        prompt = (
            f"{system}\nComplete this one task from the supplied JSON. "
            "Use only the supplied evidence. Do not access repositories, browse, modify files, "
            "or ask for input. Submit the result with the structured output tool and mark it final.\n"
            f"{json.dumps(context, default=str)}"
        )
        request = {
            "title": f"Eye of the System · {label}",
            "prompt": prompt,
            "structured_output_schema": schema,
            "structured_output_required": True,
            "repos": [],
            "memory_enabled": False,
            "resumable": False,
        }
        if self.mode:
            request["devin_mode"] = self.mode

        deadline = time.monotonic() + SESSION_TIMEOUT
        with httpx.Client(base_url="https://api.devin.ai",
                          headers={"Authorization": f"Bearer {self.key}"}, timeout=12) as client:
            return self._session(client, label, request, deadline)

    def _session(self, client, label, request, deadline):
        session_path = None
        done = False
        try:
            response = client.post(self.sessions, json=request)
            response.raise_for_status()
            session_id = response.json()["session_id"]
            session_path = f"{self.sessions}/{quote(session_id, safe='')}"
            log.info("Devin %s session started", label)
            while time.monotonic() < deadline:
                response = client.get(session_path)
                response.raise_for_status()
                session = response.json()
                status, detail = session.get("status"), session.get("status_detail")
                output = session.get("structured_output")
                if isinstance(output, dict) and output:
                    # Devin can publish the validated result before its lifecycle changes to finished.
                    done = status in {"exit", "error", "suspended"}
                    log.info("Devin %s structured output received", label)
                    return output, {"reasoning_provider": "devin", "reasoning_session_id": session_id}
                if detail == "finished" or status == "exit":
                    done = True
                    log.warning("Devin %s finished without structured output", label)
                    break
                if status in {"error", "suspended"} or detail in {"waiting_for_user", "waiting_for_approval"}:
                    done = status in {"error", "suspended"}
                    log.warning("Devin %s session stopped before completing the task", label)
                    break
                time.sleep(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))
            else:
                log.warning("Devin %s session timed out", label)
        except httpx.HTTPStatusError as exc:
            log.warning("Devin %s request failed (HTTP %d)", label, exc.response.status_code)
        except (httpx.RequestError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("Devin %s request failed (%s)", label, type(exc).__name__)
        finally:
            if session_path and not done:
                try:
                    client.delete(session_path).raise_for_status()
                except httpx.HTTPError:
                    log.warning("Devin %s session could not be terminated", label)
        return {}, {}
