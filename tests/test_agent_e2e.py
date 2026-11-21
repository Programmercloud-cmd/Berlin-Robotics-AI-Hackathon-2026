"""End-to-end: the agent runs unattended over the first live day and must, on its own,
  * flag the payment fault, localise it to DE x Android, name the payments-android release with
    supporting evidence, and reject the iOS release by scope;
  * not alert on the planned campaign (explained) or on the late partition (held as a data issue);
  * report each problem once;
  * incorporate a contributor's metric and discover its formula without being told where it belongs.
"""
import argparse
import warnings

import pandas as pd
import pytest

from sentinel.run import CONTRIBUTOR_METRIC, Runtime

warnings.filterwarnings("ignore")


@pytest.fixture(scope="module")
def rt():
    r = Runtime(argparse.Namespace(no_meta=False))
    r.run(pd.Timestamp("2026-10-26T19:00"), 0.0, pd.Timestamp("2026-10-26T18:00"))
    return r


def test_payment_fault_found_and_explained(rt):
    alerts = [e for e in rt.bus.of_type("FINAL_REPORT") if e.payload["kind"] == "alert"]
    pay = [e for e in alerts if e.payload["first_cause"] == "payment_success_rate"]
    assert len(pay) == 1, [a.payload for a in alerts]
    p = pay[0]
    assert p.sim_ts == "2026-10-26 16:00:00"                       # first hour the data could show it
    assert p.payload["segment"] == "country=de|platform=android"
    assert p.payload["label"] == "supported_cause"
    inc = rt.incidents.incidents[p.incident_id]
    stmts = {h.statement: h for h in inc.hypotheses}
    pay_h = next(h for s, h in stmts.items() if "payments-android" in s)
    ios_h = next(h for s, h in stmts.items() if "app-ios" in s)
    assert pay_h.status == "supported" and pay_h.posterior >= 0.8
    assert ios_h.status == "rejected"
    assert "### Evidence" in inc.report_md and "### What remains uncertain" in inc.report_md


def test_innocent_campaign_not_alerted(rt):
    for inc in rt.incidents.incidents.values():
        if "channel=paid" in inc.segment:
            assert inc.label == "explained" and inc.id not in rt.incidents.alerted
    assert rt.bus.of_type("INNOCENT_CHANGE"), "planned volume increase should be recognised as innocent"


def test_late_partition_is_a_data_issue_not_an_alert(rt):
    dq = [e for e in rt.bus.of_type("DATA_ISSUE") if e.payload["hour"] == "2026-10-26 13:00:00"]
    assert dq and dq[0].payload["table"] == "fact_commerce" and dq[0].payload["completeness"] < 0.95
    assert any(e.payload["completeness"] == 1.0 for e in dq), "re-evaluated once complete"
    bad = [e for e in rt.bus.of_type("ANOMALY_DETECTED") if e.payload["ts"] == "2026-10-26 13:00:00"
           and e.payload["metric"] in ("orders", "gmv", "product_views")]
    assert not bad


def test_each_problem_reported_once(rt):
    alerts = [e for e in rt.bus.of_type("FINAL_REPORT") if e.payload["kind"] == "alert"]
    keys = [(e.payload["first_cause"], e.payload["segment"]) for e in alerts]
    assert len(keys) == len(set(keys))
    assert len(alerts) == 1                                      # only the payment fault pages anyone on day one


def test_contributor_metric_extends_graph(rt):
    assert CONTRIBUTOR_METRIC["id"] in rt.graph.nodes
    assert rt.graph.formulas.get("payment_costs") == "gmv * payment_cost_rate"
    e = next(e for e in rt.graph.edges.values() if e.type == "formula" and e.dst == "payment_costs"
             and e.src == "payment_cost_rate")
    assert e.status == "validated"
    assert set(rt.graph.nodes["payment_cost_rate"].dimensions) == {"country", "platform", "channel", "category"}


def test_every_step_is_on_the_bus(rt):
    types = {e.type for e in rt.bus.history}
    assert {"THOUGHT", "ACTION", "OBSERVATION", "HYPOTHESIS", "ANOMALY_DETECTED", "INCIDENT_OPENED",
            "FINAL_REPORT", "GRAPH_MUTATION", "MONITOR_TICK"} <= types
