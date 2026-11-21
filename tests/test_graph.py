import copy

import pytest

from argus.graph.formula import Formula, FormulaError, Unit, infer_unit
from argus.graph.graph import GraphError, MetricGraph, load_contracts
from argus.graph.model import EdgeType
from argus.graph.semantics import normalize_unit, parse_population, subset_relation
from argus.graph.tree import build_tree, render
from argus.graph.validate import validate_structure

SOURCES, REGISTRY, OBJECTIVES = "catalog/sources.yaml", "catalog/contracts/registry.yaml", "catalog/objectives.yaml"
HOLDBACK = "catalog/contracts/holdback/{}.yaml"


@pytest.fixture
def g():
    graph, regs = MetricGraph.bootstrap(SOURCES, REGISTRY, OBJECTIVES)
    assert all(r.state == "registered" for r in regs), [(r.metric_id, r.reasons) for r in regs if r.reasons]
    return graph


def contract(metric_id: str) -> dict:
    return copy.deepcopy(next(c for c in load_contracts(REGISTRY, raw=True) if c["metric_id"] == metric_id))


def holdback(name: str) -> dict:
    return load_contracts(HOLDBACK.format(name), raw=True)[0]


def edge(g, type_, src, dst):
    return [e for e in g.edges.values() if e.type == type_ and e.source == src and e.target == dst]


# ------------------------------------------------------------------ formula & semantics
def test_formula_roles_and_safety():
    r = Formula.parse("1 - a - b").roles()
    assert r["a"].role == "subtrahend" and r["a"].polarity == -1
    assert Formula.parse("x / y").roles()["y"].role == "divisor"
    with pytest.raises(FormulaError):
        Formula.parse("__import__('os').system('x')")
    with pytest.raises(FormulaError):
        infer_unit(Formula.parse("a + b"), {"a": "EURc", "b": "count"})


def test_units_and_populations():
    assert normalize_unit("EUR cents per click") == Unit.parse("EURc/count")
    assert normalize_unit("ratio") == Unit.parse("") and normalize_unit("units") != normalize_unit("count")
    p = parse_population("finalised_payment_attempts_with_timeout_by_finalisation_time")
    assert (p.entity, p.qualifier, p.basis) == ("finalised_payment_attempts", "timeout", "finalisation_time")
    assert subset_relation("sessions_with_checkout_by_session_start", "sessions_by_session_start")
    assert not subset_relation("confirmed_orders_by_confirmation_time", "sessions_by_session_start")


# ------------------------------------------------------------------ bootstrap
def test_bootstrap_33_and_no_seeded_business_edges(g):
    assert len(g.metrics) == 33
    assert validate_structure(g).ok
    assert g.unresolved_objectives() == ["marketplace_contribution"]
    drivers = [e for e in g.edges.values() if e.type == EdgeType.DRIVER]
    assert drivers and all(e.provenance.startswith("rule:") and e.status == "provisional" for e in drivers)


def test_contract_edges(g):
    e, = edge(g, EdgeType.ARITHMETIC, "payment_attempts", "payment_success_rate")
    assert e.status == "validated" and e.role == "divisor" and e.evidence["derived"] is True
    assert set(g.breakdowns("advertising_spend")) == {"campaign_id", "country", "acquisition_channel"}  # mapped
    assert g.source_ids("data_completeness_ratio") == ["source_manifests", "ingestion_receipts"]
    assert g.metric("data_completeness_ratio").role == "data_quality"


def test_inferred_structure_is_provisional(g):
    for child in ("successful_payments", "payment_timeouts", "other_failed_payments"):
        e, = edge(g, EdgeType.SUBSET, child, "payment_attempts")
        assert e.status == "provisional" and e.provenance == "rule:population_subset"
    parts = g.components("payment_attempts")
    assert list(parts) == ["other_failed_payments + payment_timeouts + successful_payments"]  # superseded smaller
    e, = edge(g, EdgeType.ARITHMETIC, "sessions", "session_conversion_rate")
    assert e.provenance == "rule:shared_denominator" and e.status == "provisional"
    assert not edge(g, EdgeType.SUBSET, "orders", "sessions")  # different entity & time basis: not a subset
    assert edge(g, EdgeType.DRIVER, "successful_payments", "orders")  # from the definition of orders


# ------------------------------------------------------------------ extension (the three demo additions)
def test_register_holdbacks(g):
    for name in ("payment_timeout_rate", "unavailable_feed_share", "marketplace_contribution"):
        r = g.register(holdback(name))
        assert r.state == "registered", r.reasons
        assert r.edges_created
    assert g.unresolved_objectives() == []
    m = g.metric("marketplace_contribution")
    assert m.canonical_dimensions == ["country", "acquisition_channel"]
    es = g.edges_of("marketplace_contribution", [EdgeType.ARITHMETIC])
    assert len(es) == 4 and all(e.status == "validated" for e in es)
    assert {e.source: e.polarity for e in es}["advertising_spend"] == -1
    assert any("calendar period" in c.detail for c in es[0].checks)
    assert "advertising_spend" in g.upstream("marketplace_contribution")
    assert g.metric("payment_timeout_rate").status == "warming"


@pytest.mark.parametrize("mutate, reason", [
    (lambda c: c.update(parent="orders"), "field not allowed"),
    (lambda c: c.update(dimensions=c["dimensions"] + ["platform"]), "not supported"),
    (lambda c: c["calculation"]["right"].update(metric_id="nope"), "unregistered metric"),
    (lambda c: c["calculation"]["right"].update(version=2), "does not match"),
    (lambda c: c.pop("calculation"), "exactly one of query_ref or calculation"),
    (lambda c: c["calculation"].update(operator="power"), "malformed calculation"),
    (lambda c: c["calculation"]["left"].update(metric_id="x"), "unexpected keys"),
])
def test_bad_contracts_rejected(g, mutate, reason):
    c = holdback("marketplace_contribution")
    mutate(c)
    r = g.register(c)
    assert r.state == "rejected" and any(reason in x for x in r.reasons), r.reasons
    assert "marketplace_contribution" not in g.metrics


def test_unit_mismatch_rejected(g):
    c = holdback("payment_timeout_rate")
    c.update(metric_id="nonsense", calculation={"operator": "add", "left": {"metric_id": "orders", "version": 1},
                                                 "right": {"metric_id": "gross_merchandise_value", "version": 1}},
             dimensions=["country"])
    c["source_ids"] = ["orders"]
    r = g.register(c)
    assert r.state == "rejected" and "unit check failed" in r.reasons[0]


def test_cycle_rejected(g):
    c = contract("payment_attempts")
    c.update(version=2, query_ref=None, calculation={
        "operator": "divide", "left": {"metric_id": "successful_payments", "version": 1},
        "right": {"metric_id": "payment_success_rate", "version": 1}})
    r = g.register(c)
    assert r.state == "rejected" and "cyclic arithmetic definition" in r.reasons


def test_versions(g):
    c = contract("successful_payments")
    assert g.register(c).warnings == ["identical contract already registered (idempotent)"]
    c2 = dict(c, definition="changed")
    assert "conflicting definition" in g.register(c2).reasons[0]
    r = g.register(dict(c2, version=2))
    assert r.state == "registered" and any("payment_success_rate" in w for w in r.warnings)
    e, = edge(g, EdgeType.ARITHMETIC, "successful_payments", "payment_success_rate")
    assert e.status == "provisional"  # dependent references the old version
    assert "older" in g.register(dict(c, version=1, definition="x")).reasons[0]


def test_deprecate_guard(g):
    with pytest.raises(GraphError):
        g.deprecate("payment_attempts")
    g.deprecate("ad_impressions")
    assert g.metric("ad_impressions").status == "deprecated"


def test_roundtrip_and_tree(g, tmp_path):
    g.register(holdback("payment_timeout_rate"))
    g.save(tmp_path / "g.json")
    h = MetricGraph.load(tmp_path / "g.json")
    assert h.to_dict()["edges"] == g.to_dict()["edges"] and h.revision == g.revision
    text = render(build_tree(h))
    assert "Confirmed orders (orders)" in text and "Unresolved objectives" in text and "↗" in text
