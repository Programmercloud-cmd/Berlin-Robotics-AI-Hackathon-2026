import json
import unittest
from unittest.mock import patch

import httpx

from eye.devin import DevinReasoner


class DevinReasonerTest(unittest.TestCase):
    def test_structured_output_is_accepted_before_session_finishes(self):
        client = httpx.Client
        requested_schemas = []
        outputs = {
            "devin-investigation": {
                "summary": "Checkout payments are timing out.",
                "likely_causes": ["The payment attempt telemetry shows gateway timeouts."],
                "impact": "Checkout completion dropped in the affected segment.",
                "uncertainty": "The origin of the timeouts is unknown.",
            },
            "devin-curation": {
                "cadence_minutes": 5,
                "relationships": [
                    {"target": "process:payment", "kind": "measures", "reason": "Measures payment attempts."}
                ],
            },
        }

        def respond(request):
            self.assertEqual(request.headers["Authorization"], "Bearer test-service-user")
            if request.method == "POST":
                payload = json.loads(request.content)
                self.assertTrue(payload["structured_output_required"])
                self.assertEqual(payload["devin_mode"], "lite")
                schema = payload["structured_output_schema"]
                requested_schemas.append(set(schema["required"]))
                role = "curation" if "curation" in payload["title"] else "investigation"
                return httpx.Response(200, json={"session_id": f"devin-{role}"})
            session_id = request.url.path.rsplit("/", 1)[1]
            if request.method == "DELETE":
                self.assertIn(session_id, outputs)
                return httpx.Response(204)
            self.assertEqual(request.method, "GET")
            return httpx.Response(200, json={
                "status": "running", "status_detail": "waiting_for_user",
                "structured_output": outputs[session_id],
            })

        with patch("eye.devin.httpx.Client",
                   side_effect=lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs)):
            reasoner = DevinReasoner("test-service-user", "org-test", mode="lite")
            for role in ("investigation", "curation"):
                output, provenance = reasoner.reason(role, "Use observed data.", {})
                self.assertEqual(output, outputs[f"devin-{role}"])
                self.assertEqual(provenance["reasoning_provider"], "devin")
                self.assertEqual(provenance["reasoning_session_id"], f"devin-{role}")
        self.assertEqual(requested_schemas, [set(outputs["devin-investigation"]), set(outputs["devin-curation"])])
