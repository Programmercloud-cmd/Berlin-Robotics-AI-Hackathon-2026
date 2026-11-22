import json
from collections import Counter

import pytest
import yaml

from shoply.contracts import Edge, MetricNode
from shoply.graph import (BREAKDOWN, CONTEXT, DATA_DIR, FORMULA, POSSIBLE_CAUSE, GraphValidationError,
                            MetricGraph, load_graph, load_held_back)
from shoply.graph.export import to_dict, to_dot, to_json, to_mermaid

HELD_BACK = {"avg_order_value", "for_you_oos_exposure_share"}


@pytest.fixture()
def g():
    return load_graph()


def codes(excinfo):
    return {i.code for i in excinfo.value.issues}


def node(id="new_metric", **kw):
    base = dict(id=id, name=id, description="", kind="count", unit="count", table="fact_commerce",
                value_sql="SUM(x)", dimensions=["country"])
    return MetricNode(**{**base, **kw})


# ---------- starting graph ----------
def test_registers_exactly_the_29_supplied_metrics(g):
    supplied = yaml.safe_load((DATA_DIR / "metrics.yaml").read_text())
    assert len(g) == 29 and {m.id for m in g.metrics()} == {m["id"] for m in supplied}
    assert not HELD_BACK & {m.id for m in g.metrics()}
    assert g.validate() == []
    for raw in supplied:
        m = g.metric(raw["id"])
        assert (m.name, m.description, m.kind, m.unit, m.dimensions) == (
            raw["name"], raw["description"], raw["kind"], raw["unit"], raw["dimensions"])
        assert g.good_direction(m.id) == raw["good_direction"]
        assert m.services == raw.get("services", [])


def test_only_supplied_relationships_exist(g):
    formulas = yaml.safe_load((DATA_DIR / "formulas.yaml").read_text())
    causal = yaml.safe_load((DATA_DIR / "causal.yaml").read_text())
    assert Counter(e.type for e in g.edges()) == {FORMULA: 22, POSSIBLE_CAUSE: 8, BREAKDOWN: 84, CONTEXT: 25}
    assert {(e.src, e.dst) for e in g.edges("CONTEXT")} == {
        (f"service:{s}", m.id) for m in g.metrics() for s in m.services}
    for f in formulas:
        assert g.formula(f["output"])["expr"] == f["expr"]
    assert {e.dst for e in g.edges("FORMULA")} == {f["output"] for f in formulas}
    got = {(e.src, e.dst, e.sign, e.lag_h, e.prior) for e in g.edges("POSSIBLE_CAUSE")}
    assert got == {(c["src"], c["dst"], c["sign"], tuple(c["lag_h"]), c["prior"]) for c in causal}
    for m in g.metrics():
        assert g.breakdowns(m.id) == m.dimensions


def test_arrows_point_from_driver_to_affected_metric(g):
    assert {e.src for e in g.drivers("orders", ["FORMULA"])} == {"sessions", "conversion_rate"}
    assert all(e.dst == "orders" for e in g.drivers("orders"))
    cause = g.possible_causes("search_ctr")[0]
    assert (cause.src, cause.dst) == ("search_relevance", "search_ctr")


def test_formula_ops(g):
    f = g.formula("contribution")
    assert f["op"] == "difference" and f["inputs"] == {
        "marketplace_fees": "+", "ad_revenue": "+", "platform_discounts": "-",
        "acquisition_cost": "-", "payment_costs": "-"}
    assert {e.op for e in g.drivers("contribution", ["FORMULA"])} == {"difference"}
    assert g.formula("view_rate") == {"expr": "product_views / sessions", "op": "ratio",
                                      "inputs": {"product_views": "*", "sessions": "/"}}
    pv = g.formula("product_views")
    assert pv["op"] == "sum" and pv["inputs"] == {"searches": "*", "search_ctr": "*", "feed_impressions": "*",
                                                  "feed_ctr": "*", "direct_views": "+"}
    assert g.nx.edges["platform_discounts", "contribution", "formula"]["operator"] == "-"
    assert g.formula("gmv") is None


def test_metadata_without_a_contract_slot_is_kept(g):
    assert g.metric("gmv").value_sql == "SUM(gmv_eur)"
    assert g.metric("view_rate").table == "cross_table"
    assert g.metric("view_rate").denominator_sql == "SUM(sessions)"
    assert g.tables("view_rate") == ["fact_commerce", "fact_traffic"]
    assert g.extra("contribution")["expr"] == g.formula("contribution")["expr"]
    assert g.good_direction("pacing_ratio") == "two_sided" and "note" in g.extra("pacing_ratio")
    assert g.dimension_values["platform"] == ["android", "ios", "web"]


# ---------- payment path ----------
def test_payment_path(g):
    path = g.paths("payment_success_rate", "orders")[0]
    assert [(e.src, e.dst, e.type) for e in path] == [
        ("payment_success_rate", "conversion_rate", FORMULA), ("conversion_rate", "orders", FORMULA)]
    assert g.downstream("payment_success_rate") == {"conversion_rate": 1, "orders": 2}
    assert g.formula("conversion_rate")["op"] == "product"
    assert g.formula("conversion_rate")["inputs"]["payment_success_rate"] == "*"
    assert g.metrics(service="payments-android") == [g.metric("payment_success_rate")]
    assert g.breakdowns("payment_success_rate") == ["country", "platform", "channel", "category"]
    assert g.segment_issues("payment_success_rate", {"platform": "android", "country": "de"}) == []
    assert g.shared_dimensions("payment_success_rate", "conversion_rate") == ["country", "platform", "channel"]
    assert [e.src for e in g.drivers("contribution") if e.src == "payment_costs"] == ["payment_costs"]
    # orders reaches GMV only once the withheld avg_order_value is added
    assert g.paths("payment_success_rate", "contribution") == []


def test_exploration(g):
    assert g.upstream("orders", depth=1) == {"sessions": 1, "conversion_rate": 1}
    assert "search_relevance" in g.upstream("orders") and "search_relevance" not in g.upstream("orders", ["FORMULA"])
    assert g.upstream("in_stock_rate") == {"delivered_units": 1, "po_units": 2}
    assert {m.id for m in g.metrics(table="fact_ads")} == {"ad_revenue", "sponsored_clicks", "avg_cpc", "pacing_ratio"}
    assert {m.id for m in g.metrics(text="for you")} >= {"feed_impressions", "feed_ctr", "feed_purchase_rate"}
    assert g.segment_issues("in_stock_rate", {"country": "de"}) and g.segment_issues("orders", {"country": "xx"})
    assert g.upstream("orders", iter(["FORMULA"])) == g.upstream("orders", ["FORMULA"])
    assert len(g.subgraph("orders", 2, iter(["FORMULA"])).edges()) == len(g.subgraph("orders", 2, ["FORMULA"]).edges())
    sub = g.subgraph("conversion_rate", depth=1)
    assert {m.id for m in sub.metrics()} == {"conversion_rate", "view_rate", "add_to_cart_rate", "checkout_rate",
                                             "payment_success_rate", "platform_discounts", "orders"}
    d = g.describe("conversion_rate")
    assert d["used_in"] == ["orders"] and d["possible_causes"][0]["src"] == "platform_discounts"
    with pytest.raises(KeyError):
        g.metric("avg_order_value")


# ---------- validation ----------
def test_duplicate_id(g):
    with pytest.raises(GraphValidationError) as e:
        g.add_metric(node("orders"))
    assert codes(e) == {"duplicate_id"}


def test_missing_metric(g):
    with pytest.raises(GraphValidationError) as e:
        g.add_edge(Edge(src="nope", dst="orders", type="causal", sign="+"))
    assert codes(e) == {"missing_metric"}
    with pytest.raises(GraphValidationError) as e:
        g.add_formula("gmv", "orders * avg_order_value")
    assert codes(e) == {"missing_metric"}


@pytest.mark.parametrize("edge", [
    dict(src="orders", dst="gmv", type="causal"),                                   # no sign
    dict(src="orders", dst="orders", type="causal", sign="+"),                      # self-cause
    dict(src="search_relevance", dst="search_ctr", type="causal", sign="+"),        # already exists
    dict(src="orders", dst="gmv", type="causal", sign="+", lag_h=(5, 1)),
    dict(src="orders", dst="gmv", type="causal", sign="+", prior=1.5),
    dict(src="orders", dst="gmv", type="context", sign="+"),
    dict(src="in_stock_rate", dst="in_stock_rate", type="breakdown", dimension="country"),
    dict(src="orders", dst="gmv", type="breakdown", dimension="country"),
    dict(src="orders", dst="gmv", type="formula"),                                  # no expr
    dict(src="orders", dst="gmv", type="formula", expr="sessions * take_rate", op="product"),
    dict(src="orders", dst="sessions", type="formula", expr="orders / conversion_rate", op="ratio"),  # cycle
    dict(src="gmv", dst="orders", type="formula", expr="gmv / take_rate", op="ratio"),  # second formula
])
def test_invalid_relationship(g, edge):
    with pytest.raises(GraphValidationError) as e:
        g.add_edge(Edge(**edge))
    assert "invalid_relationship" in codes(e)
    assert g.validate() == [] and len(g.edges()) == 139


def test_partial_formula_is_rejected(g):
    aov, extra = load_held_back()["avg_order_value"]
    edges = g.formula_edges("gmv", "orders * avg_order_value")
    with pytest.raises(GraphValidationError) as e:
        g.add_metric(aov, edges=edges[:1], extra=extra)
    assert codes(e) == {"invalid_relationship"} and "avg_order_value" not in g
    g.add_metric(aov, extra=extra)
    with pytest.raises(GraphValidationError) as e:
        g.add_edge(edges[0])
    assert codes(e) == {"invalid_relationship"} and g.formula("gmv") is None
    g.add_edges(edges)
    assert set(g.formula("gmv")["inputs"]) == {"orders", "avg_order_value"}


def test_unknown_relationship_type(g):
    with pytest.raises(ValueError):
        g.edges("CORRELATION")
    with pytest.raises(ValueError):
        Edge(src="orders", dst="gmv", type="correlation")


def test_incompatible_units(g):
    for output, expr in [("gmv", "orders * take_rate"), ("take_rate", "gmv * orders"), ("gmv", "payment_costs + orders")]:
        with pytest.raises(GraphValidationError) as e:
            g.add_formula(output, expr)
        assert codes(e) == {"incompatible_units"}
    with pytest.raises(GraphValidationError) as e:
        g.add_metric(node(kind="count", unit="eur"))
    assert codes(e) == {"incompatible_units"}


def test_loader_reports_every_problem(tmp_path):
    for f in DATA_DIR.glob("*.yaml"):
        (tmp_path / f.name).write_text(f.read_text())
    metrics = yaml.safe_load((tmp_path / "metrics.yaml").read_text())
    metrics.append(dict(metrics[3]))                                   # duplicate gmv
    (tmp_path / "metrics.yaml").write_text(yaml.safe_dump(metrics))
    with (tmp_path / "formulas.yaml").open("a") as fh:
        fh.write("- output: gmv\n  expr: orders * avg_order_value\n")
    with (tmp_path / "causal.yaml").open("a") as fh:
        fh.write("- src: orders\n  dst: gmv\n  sign: '?'\n")
    with pytest.raises(GraphValidationError) as e:
        load_graph(tmp_path)
    assert codes(e) == {"duplicate_id", "missing_metric", "invalid_relationship"}


# ---------- extension ----------
def test_held_back_metrics_can_be_added_after_validation(g):
    held = load_held_back()
    assert set(held) == HELD_BACK
    aov, extra = held["avg_order_value"]
    with pytest.raises(GraphValidationError):                          # rejected as a whole: nothing registered
        g.add_metric(aov, edges=g.formula_edges("gmv", "orders + avg_order_value"), extra=extra)
    assert "avg_order_value" not in g and len(g) == 29

    g.add_metric(aov, edges=g.formula_edges("gmv", "orders * avg_order_value", status="proposed"), extra=extra)
    assert g.formula("gmv") == {"expr": "orders * avg_order_value", "op": "product",
                                "inputs": {"orders": "*", "avg_order_value": "*"}}
    assert [e.dst for e in g.paths("payment_success_rate", "contribution")[0]] == [
        "conversion_rate", "orders", "gmv", "marketplace_fees", "contribution"]

    oos, extra = held["for_you_oos_exposure_share"]
    g.add_metric(oos, extra=extra, edges=[
        *g.breakdown_edges(oos, ["category"]),
        Edge(src=oos.id, dst="feed_purchase_rate", type="causal", sign="-", status="proposed")])
    assert g.breakdowns(oos.id) == ["category"]
    assert oos.id in {e.src for e in g.possible_causes("feed_purchase_rate")}
    assert len(g) == 31 and g.validate() == []


def test_context_edges_are_supported():
    g = MetricGraph()
    g.add_metric(node("a"))
    g.add_metric(node("b"))
    g.add_edge(Edge(src="a", dst="b", type="context"))
    assert [e.src for e in g.context("b")] == ["a"] and g.edges(CONTEXT)[0].dst == "b"


# ---------- export ----------
def test_export_escapes_quotes_in_names():
    g = MetricGraph()
    g.add_metric(node("a", name='Orders from "For You"'))
    assert 'label="Orders from \\"For You\\"\\na [count]"' in to_dot(g)
    assert 'a["Orders from #quot;For You#quot; (count)"]' in to_mermaid(g)


def test_export(g):
    data = json.loads(to_json(g))
    assert data == json.loads(json.dumps(to_dict(g)))
    assert len(data["nodes"]) == 29 + 11 and len(data["links"]) == 139
    assert sum(n.get("kind") == "service" for n in data["nodes"]) == 11
    assert {l["relationship"] for l in data["links"]} == {"FORMULA", "BREAKDOWN", "POSSIBLE_CAUSE", "CONTEXT"}
    link = next(l for l in data["links"] if l["source"] == "payment_success_rate" and l["type"] == "formula")
    assert link["target"] == "conversion_rate" and link["expr"]
    assert 'payment_success_rate -> conversion_rate [label="FORMULA product"' in to_dot(g)
    assert 'search_relevance -.->|"POSSIBLE_CAUSE +"| search_ctr' in to_mermaid(g)
    assert "BREAKDOWN" not in to_dot(g) and "BREAKDOWN country" in to_dot(g, breakdowns=True)
    assert "CONTEXT" not in to_mermaid(g) and 'service__payments_android -.-|"CONTEXT"| payment_success_rate' in to_mermaid(g, context=True)
    assert "service__marketing -> sessions" in to_dot(g, context=True)


def test_export_ids_stay_unique():
    g = MetricGraph.load()
    for name in ("payments_android", "payments.android"):        # sanitise to the same id as payments-android
        g.add_edge(Edge(src=g.add_service(name), dst="orders", type=CONTEXT))
    dot, mermaid = to_dot(g, context=True), to_mermaid(g, context=True)
    ids = [line.split()[0] for line in dot.splitlines() if "shape=ellipse" in line]
    assert len(ids) == len(set(ids)) == len(g.services()) == 13
    assert len({line.split("(")[0] for line in mermaid.splitlines() if '(["service:' in line}) == 13
    into_orders = {line.split()[0] for line in dot.splitlines() if " -> orders " in line and line.split()[0] in ids}
    assert len(into_orders) == sum(e.type == CONTEXT and e.dst == "orders" for e in g.edges()) >= 2


# ---------- MetricGraph.load and helpers ----------
def test_load_classmethod_matches_loader():
    g = MetricGraph.load()
    assert len(g) == 29 and g.validate() == [] and len(g.services()) == 11


def test_context_edges_from_services(g):
    assert {e.src for e in g.context("payment_success_rate")} == {
        "service:payments-android", "service:payments-ios", "service:payments-web", "service:checkout"}
    assert {e.dst for e in g.driven("service:search")} == {"searches", "search_ctr", "search_relevance"}
    assert "service:search" not in g and "service:search" in g.nx
    assert all(not n.startswith("service:") for n in g.upstream("orders"))          # services only on request
    assert "service:checkout" in g.upstream("orders", ["FORMULA", "CONTEXT"])


def test_drivers_driven_shared_dims(g):
    assert {e.src for e in g.drivers("orders", ["FORMULA"])} == {"sessions", "conversion_rate"}
    assert {e.dst for e in g.driven("in_stock_rate")} == {"add_to_cart_rate", "feed_purchase_rate"}
    assert g.shared_dims(["orders", "sessions", "conversion_rate"]) == ["country", "platform", "channel"]
    assert g.shared_dims(["in_stock_rate", "ad_revenue"]) == []


def test_hops(g):
    assert g.hops("payment_success_rate", "orders") == 2
    assert g.hops("orders", "payment_success_rate") == 2
    assert g.hops("orders", "payment_success_rate", directed=True) is None
    assert g.hops("po_units", "add_to_cart_rate", directed=True) == 3
    assert g.hops("service:payments-android", "orders") == 3
    assert g.hops("orders", "orders") == 0
    assert g.hops("pacing_ratio", "in_stock_rate", types=["POSSIBLE_CAUSE"]) is None


def test_metric_tree(g):
    t = g.metric_tree()
    assert t["id"] == "contribution" and t["op"] == "difference" and t["operator"] is None
    assert [(c["id"], c["operator"]) for c in t["children"]] == [
        ("marketplace_fees", "+"), ("ad_revenue", "+"), ("platform_discounts", "-"),
        ("acquisition_cost", "-"), ("payment_costs", "-")]
    fees = t["children"][0]
    assert fees["op"] == "product" and [c["id"] for c in fees["children"]] == ["gmv", "take_rate"]
    assert fees["children"][0]["children"] == []
    assert g.metric_tree("orders", depth=1)["children"][1]["children"] == []
    leaves = []

    def walk(n):
        leaves.extend([n["id"]] if not n["children"] else [])
        for c in n["children"]:
            walk(c)
    walk(g.metric_tree("orders"))
    assert leaves == ["sessions", "searches", "search_ctr", "feed_impressions", "feed_ctr", "direct_views",
                      "sessions", "add_to_cart_rate", "checkout_rate", "payment_success_rate"]


def test_formula_edge_op_is_top_level():
    edges = MetricGraph.formula_edges("x", "a * b + c")
    assert {e.op for e in edges} == {"sum"}
