from datetime import datetime

import pandas as pd
import pytest

from dev.fake_data import FakeData
from sentinel.brain.graph import BrainGraph, edge_key
from sentinel.brain.tree import render_tree
from sentinel.brain.validate import check_causal, local_profile_key, validate_graph
from sentinel.contracts import Edge

LIVE = datetime(2026, 10, 26)


@pytest.fixture(scope="module")
def data():
    g = BrainGraph.from_seed()
    return FakeData(list(g.nodes.values()), g.formulas)


@pytest.fixture(scope="module")
def validated(data):
    g = BrainGraph.from_seed()
    validate_graph(g, data, LIVE)
    return g


def E(g, key):
    return g.edges[key]


# ------------------------------------------------------------------ seed graph structure
def test_seed_structure():
    g = BrainGraph.from_seed()
    assert len(g.nodes) == 30
    ops = {e.src: e.op for e in g.inputs("contribution")}
    assert ops == {"marketplace_fees": "sum", "ad_revenue": "sum", "platform_discounts": "difference",
                   "acquisition_cost": "difference", "payment_costs": "difference"}
    assert {e.src: e.op for e in g.inputs("view_rate")} == {"product_views": "product", "sessions": "ratio"}
    assert sorted(e.dimension for e in g.edges_where("breakdown", src="orders")) == \
        ["category", "channel", "country", "platform"]
    assert g.tables_of("conversion_rate") == ["fact_commerce", "fact_traffic"]   # cross-table ratio
    assert g.tables_of("contribution") == ["fact_ads", "fact_commerce", "fact_traffic"]
    assert "payment_success_rate" in g.upstream("orders")
    assert "orders" in g.downstream("payment_success_rate")


def test_formula_cycle_rejected():
    g = BrainGraph.from_seed()
    with pytest.raises(ValueError, match="cycle"):
        g.add_formula("sessions", "orders / conversion_rate")


def test_driver_tree_connected_after_aov_seed():
    """With avg_order_value seeded, orders (and payments) reach contribution."""
    g = BrainGraph.from_seed()
    assert g.roots() == ["contribution"]
    assert {"orders", "payment_success_rate"} <= set(g.upstream("contribution"))


# ------------------------------------------------------------------ validation
def test_seed_formulas_validate(validated):
    for e in validated.edges.values():
        if e.type == "formula" and e.dst != "contribution":
            assert e.status == "validated", (edge_key(e), validated.notes[edge_key(e)])
    assert all(e.status == "validated" for e in validated.edges.values() if e.type == "breakdown")


def test_wrong_formula_needs_review(data):
    g = BrainGraph.from_seed()
    g.add_formula("gmv", "orders * take_rate")   # nonsense identity
    validate_graph(g, data, LIVE, only={"gmv"})
    e = E(g, "formula:orders->gmv")
    assert e.status == "needs_review" and "fails" in g.notes[edge_key(e)]["reason"]
    assert not g.usable(e)


def test_causal_verdicts(validated):
    def note(k):
        return validated.notes[k]["reason"]
    assert E(validated, "causal:in_stock_rate->add_to_cart_rate").status == "validated"
    po = E(validated, "causal:po_units->delivered_units")
    assert po.status == "validated" and validated.notes[edge_key(po)]["stats"]["best_lag_h"] == 48
    disc = E(validated, "causal:platform_discounts->conversion_rate")
    assert disc.status == "needs_review" and "confounded" in note(edge_key(disc))   # discounts = f(orders): a proxy
    assert "definitional overlap" in note("causal:pacing_ratio->ad_revenue")
    assert E(validated, "causal:search_relevance->search_ctr").status == "seed"     # weak: stays a hypothesis


def test_wrong_sign_contradicted(validated, data):
    e = Edge(type="causal", src="in_stock_rate", dst="add_to_cart_rate", sign="-", lag_h=(0, 2), prior=0.5)
    verdict, reason, st, conf = check_causal(validated, data, e, datetime(2026, 9, 7), datetime(2026, 10, 25))
    assert verdict == "contradicted" and conf < 0.5


def test_baseline_keyed_in_local_time_across_dst():
    """DST: 20:00 local is 18:00 UTC before 25 Oct and 19:00 UTC after."""
    k = local_profile_key(pd.Series(pd.to_datetime(["2026-10-23 18:00", "2026-10-26 19:00", "2026-10-26 18:00"])))
    assert k[0] == k[1] != k[2]


def test_roundtrip_and_tree(validated, tmp_path):
    validated.save(tmp_path / "g.json")
    h = BrainGraph.load(tmp_path / "g.json")
    assert h.to_dict()["edges"] == validated.to_dict()["edges"]
    text = render_tree(h)
    assert "Contribution (contribution)" in text and "! needs_review" in text and "↗" in text


# ------------------------------------------------------------------ the dev data stand-in honours the spec
def test_fake_data_baseline(data):
    day = lambda m: data.metric(m, datetime(2026, 10, 19), datetime(2026, 10, 20))
    orders = day("orders").value.sum()
    conv = day("conversion_rate")
    assert 18_000 < orders < 21_500
    assert 0.030 < conv.numerator.sum() / conv.denominator.sum() < 0.036


def test_fake_data_late_partition_and_fault(data):
    old = data.now
    try:
        data.now = pd.Timestamp("2026-10-26 14:00")
        f = data.metric("orders", datetime(2026, 10, 26, 13), datetime(2026, 10, 26, 14))
        assert not f.complete.iloc[0] and 0.5 < data.completeness("fact_commerce", datetime(2026, 10, 26, 13)) < 0.7
        data.now = pd.Timestamp("2026-10-26 15:00")
        assert data.completeness("fact_commerce", datetime(2026, 10, 26, 13)) == 1.0
        data.now = pd.Timestamp("2026-10-27")
        psr = data.metric("payment_success_rate", datetime(2026, 10, 26, 16), datetime(2026, 10, 26, 20),
                          filters={"country": "de", "platform": "android"})
        assert 0.65 < psr.numerator.sum() / psr.denominator.sum() < 0.77
    finally:
        data.now = old
