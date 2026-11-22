"""Sentinel on Shoply (Chaimaa's data component): the agent's data interface on her live simulator.

Skipped when the Shoply checkout (.shoply-chaimaa, `git worktree add .shoply-chaimaa <remote>/chaimaa`) is missing.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / ".shoply-chaimaa" / "shoply").exists():
    pytest.skip("Shoply checkout (.shoply-chaimaa) not present", allow_module_level=True)
warnings.filterwarnings("ignore")

from sentinel.adapters.shoply import ShoplyData  # noqa: E402
from sentinel.brain.graph import BrainGraph  # noqa: E402
from sentinel.contracts import MetricNode  # noqa: E402

LIVE = pd.Timestamp("2026-10-26")


@pytest.fixture(scope="module")
def data():
    g = BrainGraph.from_seed()
    return ShoplyData(list(g.nodes.values()), g.formulas)


def test_every_seed_metric_computes_on_shoply(data):
    week = (LIVE - pd.Timedelta(days=7), LIVE)
    for m in data.nodes:
        df = data.metric(m, *week)
        assert len(df) == 168 and df.complete.all(), m
    assert 0.9 < data.metric("payment_success_rate", *week).value.mean() < 0.97


def test_breakdowns_reconcile(data):
    day = (LIVE - pd.Timedelta(days=1), LIVE)
    tot = data.metric("orders", *day).set_index("ts").value
    by = data.metric("orders", *day, group_by=["country"]).groupby("ts").value.sum()
    assert (tot - by).abs().max() == 0


def test_live_clock_runs_the_simulator_and_hides_the_future(data):
    data.now = LIVE + pd.Timedelta(hours=14)                   # 13:00 closed; its late partition is pending
    assert data.metric("sessions", LIVE, LIVE + pd.Timedelta(days=1)).ts.max() == LIVE + pd.Timedelta(hours=13)
    assert data.completeness("fact_commerce", LIVE + pd.Timedelta(hours=13)) == pytest.approx(0.6, abs=0.01)
    data.now = LIVE + pd.Timedelta(hours=15)                   # the late rows arrive one hour later
    assert data.completeness("fact_commerce", LIVE + pd.Timedelta(hours=13)) == pytest.approx(1.0)


def test_events_include_scenarios_and_red_herrings(data):
    data.now = LIVE + pd.Timedelta(hours=18)
    evs = data.events(LIVE, data.now)
    pay = [e for e in evs if e.service == "payments-android"]
    assert pay and pay[0].scope == {"country": "de", "platform": "android"}
    assert any(e.type == "campaign" and e.planned for e in evs)
    assert any("routine release" in e.description for e in evs)  # background releases (no effect)
    assert all(pd.Timestamp(e.ts) < data.now for e in evs)


def test_runtime_registered_metric_works(data):
    node = MetricNode(id="payment_cost_rate_test", name="Payment cost rate", kind="ratio", unit="share",
                      table="fact_commerce", numerator_sql="SUM(payment_costs_eur)", denominator_sql="SUM(gmv_eur)",
                      dimensions=["country", "platform"], owner="test", description="payment costs per GMV")
    data.register_node(node)
    df = data.metric("payment_cost_rate_test", LIVE - pd.Timedelta(days=1), LIVE, group_by=["platform"])
    assert len(df) == 72 and df.value.between(0.01, 0.03).all()
