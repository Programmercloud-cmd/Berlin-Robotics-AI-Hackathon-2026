"""Run one disposable Devin API v3 session per reasoning request."""

import json
import logging
import time
from urllib.parse import quote

import httpx

from eye.metric_policy import CADENCES
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
        "cadence_minutes": {"type": "integer", "enum": list(CADENCES)},
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

    def investigate(self, system, schema, context, respond):
        """Run up to three structured turns, passing local reads back to Devin."""
        paths = []
        continuations = []
        turns = []
        path = None
        with httpx.Client(base_url="https://api.devin.ai",
                          headers={"Authorization": f"Bearer {self.key}"}, timeout=15) as client:
            def start(content):
                request = {"title": "Eye of the System · graph investigation",
                           "prompt": system + "\n" + content, "structured_output_schema": schema,
                           "structured_output_required": True, "repos": [],
                           "memory_enabled": False, "resumable": True}
                if self.mode:
                    request["devin_mode"] = self.mode
                created = client.post(self.sessions, json=request)
                created.raise_for_status()
                paths.append(created.json()["session_id"])
                log.info("Devin graph investigation session started")
                return f"{self.sessions}/{quote(paths[-1], safe='')}"

            try:
                for turn in range(1, 4):
                    content = json.dumps({"turn": turn, **context, "previous_turns": turns}, default=str)
                    reused = False
                    if path:
                        followup = client.post(path + "/messages", json={"message": system + "\n" + content})
                        if followup.status_code in {401, 403, 404, 409, 410}:
                            log.info("Devin investigation follow-up returned HTTP %s; opening a continuation.",
                                     followup.status_code)
                            continuations.append(f"new session (HTTP {followup.status_code})")
                            path = None
                        else:
                            followup.raise_for_status()
                            reused = True
                            continuations.append("same session")
                    if not path:
                        path = start(content)
                    answer = self._investigation_turn(client, path, turn)
                    if not answer and reused:
                        continuations[-1] = "new session (no follow-up output)"
                        path = start(content)
                        answer = self._investigation_turn(client, path, turn)
                    if not answer:
                        break
                    log.info("Devin graph investigation returned turn %s", turn)
                    results = respond(answer, turn)
                    if results is None:
                        return answer, {"reasoning_provider": "devin", "reasoning_session_id": paths[-1],
                                        "reasoning_sessions": paths, "continuations": continuations}
                    turns.append({"request": answer, "evidence": results})
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                log.warning("Devin graph investigation failed (%s)", type(exc).__name__)
            finally:
                for identifier in paths:
                    try:
                        client.delete(f"{self.sessions}/{quote(identifier, safe='')}")
                    except httpx.HTTPError:
                        log.warning("Devin graph investigation session cleanup was unavailable")
        return {}, {}

    def _investigation_turn(self, client, path, turn):
        deadline = time.monotonic() + SESSION_TIMEOUT
        while time.monotonic() < deadline:
            response = client.get(path)
            response.raise_for_status()
            session = response.json()
            answer = session.get("structured_output")
            if isinstance(answer, dict) and answer.get("turn") == turn:
                return answer
            if session.get("status") in {"exit", "error"}:
                return None
            time.sleep(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))
        log.warning("Devin investigation turn %s timed out", turn)
        return None

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
