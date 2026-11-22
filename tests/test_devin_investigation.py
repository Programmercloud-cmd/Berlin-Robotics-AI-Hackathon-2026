import json
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from pgserver import get_server
from psycopg.types.json import Jsonb

from eye.db import connect, set_state, setup
from eye.devin import DevinReasoner
from eye.devin_investigation import Turn, decompose_metric
from eye.ingestion import publish_window
from eye.investigation import matching_measurement
from eye.intervals import DAY, GRACE, HOUR
from eye.observation import observation_tool


class InvestigationTest(unittest.TestCase):
    def test_tool_result_continues_in_same_session_or_fresh_session(self):
        for followup_status in (200, 403):
            with self.subTest(followup_status=followup_status):
                starts, messages = [], []
                turn = 1
                client = httpx.Client

                def respond(request):
                    nonlocal turn
                    if request.method == "POST":
                        payload = json.loads(request.content)
                        if request.url.path.endswith("/messages"):
                            messages.append(payload["message"])
                            if followup_status == 403:
                                return httpx.Response(403)
                            turn = 2
                            return httpx.Response(200, json={})
                        starts.append(payload["prompt"])
                        turn = len(starts)
                        return httpx.Response(200, json={"session_id": f"devin-{turn}"})
                    if request.method == "DELETE":
                        return httpx.Response(204)
                    return httpx.Response(200, json={"status": "running", "structured_output": {"turn": turn}})

                with patch("eye.devin.httpx.Client", side_effect=lambda **kwargs:
                           client(transport=httpx.MockTransport(respond), **kwargs)):
                    output, provenance = DevinReasoner("key", "org").investigate(
                        "Test", Turn.model_json_schema(), {"alert": "a"},
                        lambda answer, turn: [{"id": "E27", "result": 14}] if turn == 1 else None,
                    )
                self.assertEqual(output["turn"], 2)
                self.assertIn('"id": "E27"', messages[0])
                self.assertEqual(len(starts), 1 if followup_status == 200 else 2)
                if followup_status == 403:
                    self.assertIn('"result": 14', starts[1])
                self.assertEqual(len(provenance["reasoning_sessions"]), len(starts))

    def test_event_mix_uses_matching_times_scopes_and_ingestion_maturity(self):
        now = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            server = get_server(directory + "/data")
            try:
                with connect(server.get_uri()) as db:
                    setup(db)
                    set_state(db, "baseline_through", (now - DAY).isoformat())
                    # Two measured reference days. The missing day is not a zero-volume reference.
                    for day in (3, 1, 0):
                        start = now - day * DAY
                        def event(index, team, lag):
                            return (f"{day}-{index}", "task_done", start + timedelta(minutes=5),
                                    start + timedelta(minutes=lag), start, Jsonb({"team": team}))
                        events = [event(0, "team-a", 6), event(1, "team-a", 100), event(2, "team-b", 6)]
                        if day == 0:
                            events.append(event(3, "team-a", 6))
                        publish_window(db, start, [], events, [], Counter({(start, "tasks", "task_done"): len(events)}))
                    tool = observation_tool(db)
                    result = tool("activity_mix", window_start=now, activities=["task_done"], scope={"team": "team-a"},
                                  as_of=now + HOUR + GRACE)
                    self.assertEqual(result["historical_windows"], 2)
                    self.assertEqual(result["ingestion_maturity_minutes"], 15)
                    self.assertEqual(result["activities"][0],
                                     {"activity": "task_done", "observed": 2, "expected": 1.0,
                                      "historical_windows": 2, "historical_counts": [1, 1]})
            finally:
                server.cleanup()

    def test_grouping_requires_matching_scope_and_eligible_volume(self):
        definitions = {
            "count": {"calculation": {"op": "count", "activity": "work_done"}},
            "rate": {"calculation": {"op": "ratio", "activity": "work_done", "denominator_activity": "eligible"}},
        }
        count = {"metric_id": "count", "observed": 40, "expected": 90, "scope": {"team": "red"}}
        rate = {"metric_id": "rate", "observed": .4, "expected": .9, "sample_size": 100, "scope": count["scope"],
                "detection_reference": {"baseline_denominator": 400, "references": 4}}
        self.assertIn("eligible volume", matching_measurement(count, rate, definitions))
        self.assertIsNone(matching_measurement(count, {**rate, "scope": {"team": "blue"}}, definitions))
        self.assertIsNone(matching_measurement(count, {**rate, "sample_size": 40}, definitions))

    def test_nested_scope_leaves_independent_degradation_separate(self):
        definitions = {"rate": {"op": "ratio", "calculation": {"op": "ratio", "activity": "done"}}}
        narrow = {"metric_id": "rate", "observed": .4, "expected": .9, "sample_size": 100, "scope": {"team": "red"}}
        broad = {**narrow, "observed": .65, "sample_size": 200, "scope": {}}
        evidence = matching_measurement(broad, narrow, definitions)
        self.assertAlmostEqual(evidence["other_observed_rate"], .9)
        self.assertAlmostEqual(evidence["unexplained_outcomes"], 0)
        self.assertIsNone(matching_measurement({**broad, "observed": .53}, narrow, definitions))

    def test_ratio_decomposition_reconstructs_the_observed_change(self):
        formula = {"op": "ratio", "activity": "resolved", "denominator_activity": "eligible"}
        current = [{"value": .5, "sample_size": 120, "details": {}}]
        history = [{"value": .9, "sample_size": 100, "details": {}}] * 3
        recent = [{"window_start": "nearby", "value": .9, "sample_size": 118, "details": {}}]
        result = decompose_metric(formula, current, history, recent)
        self.assertEqual(result["historical_windows"], 3)
        self.assertAlmostEqual(result["rate_change"], -.4)
        self.assertLess(result["numerator"]["rate_contribution"], 0)
        self.assertLess(result["denominator"]["rate_contribution"], 0)
        self.assertAlmostEqual(result["residual"], 0)
        self.assertAlmostEqual(result["outcome_gap_at_observed_eligibility"], -48)
        self.assertEqual(result["recent_checks_within_10_percent_of_eligible_volume"][0]["eligible"], 118)
