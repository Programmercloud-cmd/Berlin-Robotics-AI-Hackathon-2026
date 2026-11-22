from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from shoply.warehouse import semantic
from shoply.warehouse.fake import ADVERTISERS, COUNTRIES, LATE, PLATFORMS, fake_warehouse
from shoply.warehouse.semantic import Semantic, eval_expr, expr_names, parse_segment

A, B = datetime(2026, 10, 24), datetime(2026, 10, 27)       # the whole fake warehouse, across DST end
COLS = ["value", "numerator", "denominator", "complete"]


@pytest.fixture(scope="module")
def sem():
    return Semantic(fake_warehouse())


def raw(sem, sql, *params):
    return sem.con.execute(sql, list(params)).df()


def test_frame_shape(sem):
    f = sem.metric("orders", A, B, group_by=["country", "platform"])
    assert list(f.columns) == ["ts", "country", "platform", *COLS]
    assert len(f) == 72 * len(COUNTRIES) * len(PLATFORMS)
    assert f["ts"].min() == A and f["ts"].max() == B - timedelta(hours=1)
    assert (f["denominator"] == 1).all() and (f["numerator"] == f["value"]).all()
    assert f["complete"].dtype == bool
    keys = ["ts", "country", "platform"]
    assert f[keys].equals(f.sort_values(keys)[keys])


@pytest.mark.parametrize("mid", ["orders", "gmv", "sessions", "product_views", "take_rate", "payment_success_rate",
                                 "conversion_rate", "view_rate", "search_ctr", "search_relevance", "avg_cpc",
                                 "ad_revenue", "contribution"])
def test_group_by_country_sums_to_total(sem, mid):
    total = sem.metric(mid, A, B).set_index("ts")
    by = sem.metric(mid, A, B, group_by=["country"]).groupby("ts")[["numerator", "denominator"]].sum()
    assert np.allclose(by["numerator"], total["numerator"], rtol=1e-9)
    if sem.graph.metric(mid).kind in ("ratio", "gauge"):
        assert np.allclose(by["denominator"], total["denominator"], rtol=1e-9)
        assert np.allclose(by["numerator"] / by["denominator"], total["value"], rtol=1e-9)


def test_pacing_ratio_by_advertiser_sums_to_total(sem):
    total = sem.metric("pacing_ratio", A, B).set_index("ts")
    by = sem.metric("pacing_ratio", A, B, group_by=["advertiser"]).groupby("ts")[["numerator", "denominator"]].sum()
    assert np.allclose(by["numerator"] / by["denominator"], total["value"], rtol=1e-9)


def test_ratio_is_sum_over_sum_not_avg_of_ratios(sem):
    f = sem.metric("payment_success_rate", A, B, {"country": "de"})
    ref = raw(sem, "SELECT ts, SUM(orders) n, SUM(checkouts) d, AVG(orders / NULLIF(checkouts, 0)) avg_ratio "
                   "FROM fact_commerce WHERE ts >= ? AND ts < ? AND country = 'de' GROUP BY ts ORDER BY ts", A, B)
    assert np.allclose(f["numerator"], ref["n"]) and np.allclose(f["denominator"], ref["d"])
    assert np.allclose(f["value"], ref["n"] / ref["d"])
    assert not np.allclose(f["value"], ref["avg_ratio"])


def test_cross_table_aggregates_before_joining(sem):
    f = sem.metric("conversion_rate", A, B, {"platform": "android"}, ["country"])
    orders = raw(sem, "SELECT ts, country, SUM(orders) v FROM fact_commerce WHERE ts >= ? AND ts < ? "
                      "AND platform = 'android' GROUP BY ALL ORDER BY ALL", A, B)
    sessions = raw(sem, "SELECT ts, country, SUM(sessions) v FROM fact_traffic WHERE ts >= ? AND ts < ? "
                        "AND platform = 'android' GROUP BY ALL ORDER BY ALL", A, B)
    assert np.array_equal(f["numerator"], orders["v"]) and np.array_equal(f["denominator"], sessions["v"])
    assert np.allclose(f["value"], orders["v"] / sessions["v"])


def test_derived_contribution(sem):
    f = sem.metric("contribution", A, B, group_by=["country"])
    parts = {m: sem.metric(m, A, B, group_by=["country"])["value"].to_numpy()
             for m in ["marketplace_fees", "ad_revenue", "platform_discounts", "acquisition_cost", "payment_costs"]}
    expected = (parts["marketplace_fees"] + parts["ad_revenue"] - parts["platform_discounts"]
                - parts["acquisition_cost"] - parts["payment_costs"])
    assert np.allclose(f["value"], expected) and (f["denominator"] == 1).all()
    assert (f["numerator"] == f["value"]).all() and list(f.columns) == ["ts", "country", *COLS]


def test_derived_metric_applies_its_own_filters():
    sem = Semantic(fake_warehouse())
    node = sem.graph.metric("contribution").model_copy(update={"id": "contribution_de", "filters": {"country": "de"}})
    sem.graph.add_metric(node, extra=sem.graph.extra("contribution"))
    de = sem.metric("contribution", A, B, {"country": "de"})["value"]
    assert sem.metric("contribution_de", A, B)["value"].tolist() == pytest.approx(de.tolist())


def test_refuses_dimensions_not_listed(sem):
    with pytest.raises(ValueError):
        sem.metric("conversion_rate", A, B, group_by=["category"])
    with pytest.raises(ValueError):
        sem.metric("conversion_rate", A, B, filters={"category": "toys"})
    with pytest.raises(ValueError):
        sem.metric("in_stock_rate", A, B, group_by=["country"])
    with pytest.raises(ValueError):
        sem.metric("contribution", A, B, group_by=["platform"])
    with pytest.raises(ValueError):
        sem.metric("orders", A, B, group_by=["country", "country"])
    with pytest.raises(ValueError):
        sem.metric("orders", A, B, group_by=["sku"])


def test_filter_values_are_parameters(sem):
    with pytest.raises(ValueError):                         # unknown value, never spliced into SQL
        sem.metric("orders", A, B, {"country": "de' OR '1'='1"})
    one = sem.metric("orders", A, B, {"country": "de"})["value"].sum()
    two = sem.metric("orders", A, B, {"country": ["de", "fr"]})["value"].sum()
    fr = sem.metric("orders", A, B, {"country": "fr"})["value"].sum()
    assert two == one + fr
    both = sem.metric("orders", A, B, {"country": "de", "platform": "android"}, ["channel"])
    ref = raw(sem, "SELECT SUM(orders) FROM fact_commerce WHERE ts >= ? AND ts < ? AND country = ? AND platform = ?",
              A, B, "de", "android").iloc[0, 0]
    assert both["value"].sum() == ref


def test_iterator_filters_are_read_once(sem):
    listed = sem.metric("conversion_rate", A, B, {"country": ["de", "fr"]})
    assert listed.equals(sem.metric("conversion_rate", A, B, {"country": (c for c in ["de", "fr"])}))
    assert listed.equals(sem.evaluate("conversion_rate", A, B, {"country": iter(["de", "fr"])})
                         .assign(numerator=listed["numerator"], denominator=listed["denominator"])[listed.columns])


def test_accepts_aware_and_string_times(sem):
    naive = sem.metric("orders", A, B)
    aware = sem.metric("orders", A.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/Berlin")),
                       B.replace(tzinfo=timezone.utc))
    assert naive.equals(aware) and naive.equals(sem.metric("orders", "2026-10-24T00:00Z", "2026-10-27"))


def test_pacing_ratio_matches_reference(sem):
    start, end = datetime(2026, 10, 24, 10), datetime(2026, 10, 26, 12)     # mid local day, across DST end
    ads = raw(sem, "SELECT ts, advertiser, SUM(spend_eur) s, SUM(planned_spend_eur) p FROM fact_ads "
                   "WHERE ts >= ? AND ts < ? GROUP BY ALL ORDER BY ts", start - timedelta(days=2), end)
    ads["day"] = ads["ts"].dt.tz_localize("UTC").dt.tz_convert("Europe/Berlin").dt.date
    ads[["cs", "cp"]] = ads.groupby(["advertiser", "day"])[["s", "p"]].cumsum()
    ads = ads[ads["ts"] >= start]
    total = ads.groupby("ts")[["cs", "cp"]].sum()
    f = sem.metric("pacing_ratio", start, end)
    assert len(f) == len(total) and np.allclose(f["value"], total["cs"] / total["cp"])
    by = sem.metric("pacing_ratio", start, end, group_by=["advertiser"])
    ref = ads.sort_values(["ts", "advertiser"])
    assert np.allclose(by["numerator"], ref["cs"]) and np.allclose(by["denominator"], ref["cp"])
    adv = ADVERTISERS[-1]
    for midnight in (datetime(2026, 10, 24, 22), datetime(2026, 10, 25, 23)):   # Berlin midnight before / after DST end
        first = by[(by["ts"] == midnight) & (by["advertiser"] == adv)]
        hour = ads[(ads["ts"] == midnight) & (ads["advertiser"] == adv)]
        assert np.isclose(first["numerator"].iloc[0], hour["s"].iloc[0])
    one = sem.metric("pacing_ratio", start, end, {"advertiser": adv})
    assert np.allclose(one["value"], by[by["advertiser"] == adv]["value"])


def test_complete_flag_and_completeness():
    s = Semantic(fake_warehouse())
    table, late, share = LATE
    assert s.completeness(table, late) == pytest.approx(share)
    assert s.completeness("fact_traffic", late) == 1.0 and s.completeness("fact_traffic", datetime(2026, 1, 1)) == 0.0
    window = (late - timedelta(hours=3), late + timedelta(hours=1))
    f = s.metric("orders", *window).set_index("ts")["complete"]
    assert f.to_dict() == {late - timedelta(hours=h): h != 0 for h in range(4)}
    one = (late, late + timedelta(hours=1))
    assert not s.metric("conversion_rate", *one)["complete"].iloc[0]        # AND across tables
    assert s.metric("sessions", *one)["complete"].iloc[0]
    assert not s.metric("contribution", *one)["complete"].iloc[0]           # AND across inputs
    s.con.execute("UPDATE ingestion_log SET rows = expected_rows WHERE table_name = ? AND ts = ?", [table, late])
    assert s.completeness(table, late) == 1.0 and s.metric("orders", *one)["complete"].iloc[0]


def test_events(sem):
    evs = sem.events(datetime(2026, 10, 26), datetime(2026, 10, 27))
    assert [e.id for e in evs] == ["autumn_campaign", "android_payment_de"]          # oldest first
    pay = evs[1]
    assert pay.scope == {"country": "de", "platform": "android"} and pay.service == "payments-android"
    camp = evs[0]
    assert camp.planned and camp.expected_effect == {"sessions": 1.4} and camp.service is None and camp.scope == {}
    only = sem.events(datetime(2026, 10, 26), datetime(2026, 10, 27), services=["payments-android"])
    assert [e.id for e in only] == ["android_payment_de"]
    assert sem.events(datetime(2026, 10, 26), datetime(2026, 10, 27), services=[]) == []
    assert sem.events(datetime(2026, 10, 26, 9), datetime(2026, 10, 26, 9)) == []


def test_module_level_api(sem):
    semantic.connect(sem.con)
    assert semantic.metric("orders", A, B).equals(sem.metric("orders", A, B))
    assert semantic.completeness("fact_ads", A) == 1.0
    assert [e.id for e in semantic.events(A, B, ["payments-android"])] == ["android_payment_de"]


def test_expressions_are_restricted():
    assert expr_names("a + b * (c - a) / d") == ["a", "b", "c", "d"]
    for bad in ["a ** 2", "f(a)", "a.b", "a + 1", "-a", "a if b else c", "a +"]:
        with pytest.raises(ValueError):
            expr_names(bad)
    s = eval_expr("a / b", {"a": pd.Series([1.0, 1.0]), "b": pd.Series([2.0, 0.0])})
    assert s.iloc[0] == 0.5 and np.isnan(s.iloc[1])
    a = eval_expr("a / b", {"a": np.array([1.0, 1.0]), "b": np.array([2.0, 0.0])})
    assert a[0] == 0.5 and np.isnan(a[1]) and np.isnan(eval_expr("a / b", {"a": 1.0, "b": 0.0}))
    assert parse_segment("") == {} and parse_segment("country=de|platform=ios") == {"country": "de", "platform": "ios"}
