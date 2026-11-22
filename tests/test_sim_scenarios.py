"""Expected effects of every scenario in scenarios.yaml.

Effects are measured against `baseline`, the same seed with no scenarios, so the
difference is the scenario and not sampling noise (the simulator uses common
random numbers, so unaffected rows draw the same random numbers in both runs).
"""
from datetime import datetime

import pandas as pd
import pytest

from shoply.sim.scenarios import Scenario, load_scenarios
from shoply.sim.simulator import Simulator
from shoply.warehouse.schema import FACT_TABLES
from tests.conftest import q
from tests.test_sim_determinism import dump



def ratio(world, baseline, sql):
    return q(world, sql) / q(baseline, sql)


def event(sim, eid):
    r = sim.con.execute("SELECT ts, type, service, scope, description, planned, expected_effect::VARCHAR "
                        "FROM events WHERE id = ?", [eid]).fetchall()[0]
    assert r is not None, f"event {eid} missing"
    return dict(zip(["ts", "type", "service", "scope", "description", "planned", "expected_effect"], r))


# ---------------------------------------------------------------- loading
def test_yaml_has_the_seven_scenarios():
    ids = [s.id for s in load_scenarios()]
    assert ids == ["autumn_campaign", "late_partition", "android_payment_de", "ios_release",
                   "for_you_model_shift", "search_relevance_de", "pacing_bug"]


def test_unknown_effect_or_dimension_rejected():
    with pytest.raises(ValueError):
        Scenario(id="x", start=datetime(2026, 1, 1), effects={"made_up": 2})
    with pytest.raises(ValueError):
        Scenario(id="x", start=datetime(2026, 1, 1), scope={"region": "eu"})


# ------------------------------------------------------- autumn_campaign
CAMPAIGN = "ts >= '2026-10-26 06:00' AND ts < '2026-10-26 12:00'"


def test_campaign_lifts_paid_sessions(world, baseline):
    r = ratio(world, baseline, f"SELECT SUM(sessions) FROM fact_traffic WHERE channel = 'paid' AND {CAMPAIGN}")
    assert r == pytest.approx(1.4, abs=0.03)
    r = ratio(world, baseline, f"SELECT SUM(sessions) FROM fact_traffic WHERE channel = 'organic' AND {CAMPAIGN}")
    assert r == pytest.approx(1.0, abs=0.02)


def test_campaign_lowers_paid_add_to_cart(world, baseline):
    sql = (f"SELECT SUM(carts) / SUM(search_views + feed_views + direct_views) FROM fact_commerce "
           f"WHERE {CAMPAIGN} AND channel = '{{}}'")
    assert ratio(world, baseline, sql.format("paid")) == pytest.approx(0.85, abs=0.04)
    assert ratio(world, baseline, sql.format("organic")) == pytest.approx(1.0, abs=0.04)


def test_campaign_raises_acquisition_cost(world, baseline):
    r = ratio(world, baseline, f"SELECT SUM(acquisition_cost_eur) FROM fact_traffic WHERE {CAMPAIGN}")
    assert r == pytest.approx(1.4, abs=0.04)


def test_campaign_ends(world, baseline):
    r = ratio(world, baseline, "SELECT SUM(sessions) FROM fact_traffic WHERE channel = 'paid' "
                               "AND ts >= '2026-10-26 12:00' AND ts < '2026-10-27 12:00'")
    assert r == pytest.approx(1.0, abs=0.02)


def test_campaign_event_is_planned(world):
    e = event(world, "autumn_campaign")
    assert e["type"] == "campaign" and e["planned"] is True and e["scope"] == "channel=paid"
    assert str(e["ts"]) == "2026-10-26 06:00:00"
    assert e["expected_effect"].replace(" ", "") == '{"sessions":1.4}'


# -------------------------------------------------------- late_partition
LATE_TS = datetime(2026, 10, 26, 13)


def _live(**kw):
    return Simulator(":memory:", seed=3, start=datetime(2026, 10, 26), **kw)


def test_late_partition_arrives_one_tick_later():
    sim = _live()
    while sim.now < LATE_TS:
        sim.step()
    assert sim.step() == LATE_TS
    assert q(sim, "SELECT COUNT(*) FROM fact_commerce WHERE ts = ?", LATE_TS) == 135
    rows, expected = sim.con.execute("SELECT rows, expected_rows FROM ingestion_log "
                                     "WHERE table_name = 'fact_commerce' AND ts = ?", [LATE_TS]).fetchall()[0]
    assert (rows, expected) == (135, 225)
    # other tables of the same hour are complete
    assert q(sim, "SELECT MIN(rows = expected_rows) FROM ingestion_log WHERE table_name <> 'fact_commerce'")
    sim.step()                                                # 14:00 tick delivers the rest
    assert q(sim, "SELECT COUNT(*) FROM fact_commerce WHERE ts = ?", LATE_TS) == 225
    rows, arrived = sim.con.execute("SELECT rows, arrived_at FROM ingestion_log "
                                    "WHERE table_name = 'fact_commerce' AND ts = ?", [LATE_TS]).fetchall()[0]
    assert rows == 225 and arrived > datetime(2026, 10, 26, 15)
    assert q(sim, "SELECT COUNT(*) FROM ingestion_log WHERE table_name = 'fact_commerce' AND ts = ?", LATE_TS) == 1


def test_late_partition_changes_arrival_not_values():
    a, b = _live(), _live(exclude=["late_partition"])
    a.run_until(datetime(2026, 10, 26, 16))
    b.run_until(datetime(2026, 10, 26, 16))
    for t in FACT_TABLES:
        pd.testing.assert_frame_equal(dump(a, t), dump(b, t))


def test_late_partition_logs_no_event(world):
    assert q(world, "SELECT COUNT(*) FROM events WHERE id LIKE 'late_partition%' OR type = 'data_incident'") == 0


# ---------------------------------------------------- android_payment_de
FAULT = "ts >= '2026-10-26 15:00' AND ts < '2026-10-26 21:00'"
PSR = "SELECT SUM(orders) / SUM(checkouts) FROM fact_commerce WHERE {}"


def test_payment_fault_hits_de_android(world):
    assert q(world, PSR.format(f"country = 'de' AND platform = 'android' AND {FAULT}")) == pytest.approx(0.94 * 0.755, abs=0.03)


@pytest.mark.parametrize("seg", ["country = 'fr' AND platform = 'android'",
                                 "country = 'de' AND platform = 'ios'",
                                 "country = 'de' AND platform = 'web'"])
def test_payment_fault_spares_other_segments(world, seg):
    assert q(world, PSR.format(f"{seg} AND {FAULT}")) == pytest.approx(0.94, abs=0.015)


@pytest.mark.parametrize("window", ["ts >= '2026-10-26 09:00' AND ts < '2026-10-26 15:00'",
                                    "ts >= '2026-10-26 21:00' AND ts < '2026-10-27 09:00'"])
def test_payment_fault_only_inside_window(world, window):
    assert q(world, PSR.format(f"country = 'de' AND platform = 'android' AND {window}")) == pytest.approx(0.94, abs=0.015)


def test_payment_fault_is_downstream_of_checkout(world, baseline):
    sql = (f"SELECT SUM(checkouts) FROM fact_commerce WHERE country = 'de' AND platform = 'android' "
           f"AND channel <> 'paid' AND {FAULT}")
    assert ratio(world, baseline, sql) == pytest.approx(1.0, abs=0.08)
    orders = (f"SELECT SUM(orders) FROM fact_commerce WHERE country = 'de' AND platform = 'android' "
              f"AND channel <> 'paid' AND {FAULT}")
    assert ratio(world, baseline, orders) == pytest.approx(0.755, abs=0.08)


def test_payment_fault_events(world):
    s, e = event(world, "android_payment_de"), event(world, "android_payment_de_end")
    assert (s["type"], s["service"], s["description"]) == ("release", "payments-android", "5.12 rollout")
    assert (e["type"], e["service"], e["description"]) == ("release", "payments-android", "rollback to 5.11")
    assert s["scope"] == e["scope"] == "country=de|platform=android"
    assert (str(s["ts"]), str(e["ts"])) == ("2026-10-26 15:00:00", "2026-10-26 21:00:00")


# ----------------------------------------------------------- ios_release
def test_ios_release_is_event_only():
    a, b = _live(), _live(exclude=["ios_release"])
    a.run_until(datetime(2026, 10, 27))
    b.run_until(datetime(2026, 10, 27))
    for t in FACT_TABLES:
        pd.testing.assert_frame_equal(dump(a, t), dump(b, t))
    e = event(a, "ios_release")
    assert (e["type"], e["service"], e["description"], e["planned"]) == ("release", "app-ios", "7.3 rollout", False)
    assert str(e["ts"]) == "2026-10-26 15:00:00"
    assert q(b, "SELECT COUNT(*) FROM events WHERE id = 'ios_release'") == 0


# --------------------------------------------------- for_you_model_shift
SHIFT = "ts >= '2026-10-27 10:00'"


def test_model_shift_raises_feed_ctr(world, baseline):
    sql = f"SELECT SUM(feed_views) / SUM(feed_impressions) FROM fact_commerce WHERE {SHIFT} AND channel = '{{}}'"
    assert ratio(world, baseline, sql.format("for_you")) == pytest.approx(1.2, abs=0.04)
    assert ratio(world, baseline, sql.format("organic")) == pytest.approx(1.0, abs=0.03)


def test_model_shift_raises_oos_exposure(world, baseline):
    sql = f"SELECT SUM(feed_oos_impressions) / SUM(feed_impressions) FROM fact_commerce WHERE {SHIFT} AND channel = '{{}}'"
    assert q(world, sql.format("for_you")) == pytest.approx(0.35, abs=0.01)
    assert q(baseline, sql.format("for_you")) < 0.05
    assert q(world, sql.format("organic")) < 0.05


def test_model_shift_lowers_feed_purchase_rate(world, baseline):
    sql = f"SELECT SUM(feed_orders) / SUM(feed_views) FROM fact_commerce WHERE {SHIFT} AND channel = 'for_you'"
    assert ratio(world, baseline, sql) < 0.8


def test_model_shift_not_before_start(world, baseline):
    sql = ("SELECT SUM(feed_views) / SUM(feed_impressions) FROM fact_commerce WHERE channel = 'for_you' "
           "AND ts >= '2026-10-26' AND ts < '2026-10-27 10:00'")
    assert ratio(world, baseline, sql) == pytest.approx(1.0, abs=0.03)


def test_model_shift_event(world):
    e = event(world, "for_you_model_shift")
    assert (e["type"], e["service"], e["description"], e["scope"]) == ("release", "recs-model", "model v42", "channel=for_you")


# --------------------------------------------------- search_relevance_de
RELEV = "ts >= '2026-10-27 14:00' AND ts < '2026-10-29'"


def test_search_relevance_drops_in_de_only(world, baseline):
    sql = f"SELECT SUM(relevance_sum) / SUM(relevance_n) FROM fact_traffic WHERE {RELEV} AND country = '{{}}'"
    assert q(world, sql.format("de")) == pytest.approx(0.80 * 0.7, abs=0.005)
    assert q(world, sql.format("fr")) == pytest.approx(0.80, abs=0.005)
    before = "SELECT SUM(relevance_sum) / SUM(relevance_n) FROM fact_traffic WHERE country = 'de' AND ts < '2026-10-27 14:00'"
    assert q(world, before) == pytest.approx(0.80, abs=0.005)


def test_search_relevance_lowers_de_search_views_and_orders(world, baseline):
    sql = f"SELECT SUM({{}}) FROM fact_commerce WHERE {RELEV} AND country = '{{}}'"
    assert ratio(world, baseline, sql.format("search_views", "de")) == pytest.approx(0.7, abs=0.02)
    assert ratio(world, baseline, sql.format("direct_views", "de")) == pytest.approx(1.0, abs=0.01)
    assert ratio(world, baseline, sql.format("orders", "de")) < 0.95
    assert ratio(world, baseline, sql.format("search_views", "fr")) == pytest.approx(1.0, abs=0.01)


def test_search_relevance_logs_no_event(world):
    assert q(world, "SELECT COUNT(*) FROM events WHERE id LIKE 'search_relevance%'") == 0


# ---------------------------------------------- delivery_lag_days (custom)
def _supplier_delay():
    s = Scenario(id="supplier_delay", start=datetime(2026, 10, 3), scope={"category": "electronics"},
                 effects={"delivery_lag_days": 9})
    sim = Simulator(":memory:", seed=42, start=datetime(2026, 9, 28), scenarios=[s])
    sim.run_until(datetime(2026, 10, 14))
    return sim


@pytest.fixture(scope="module")
def delayed():
    return _supplier_delay()


def test_supplier_delay_empties_electronics(delayed):
    isr = ("SELECT MIN(r) FROM (SELECT ts, SUM(in_stock * demand_weight) / SUM(demand_weight) r FROM fact_inventory "
           "WHERE category = '{}' AND ts >= '2026-10-03' GROUP BY 1)")
    assert q(delayed, isr.format("electronics")) < 0.5
    assert q(delayed, isr.format("fashion")) > 0.85


def test_supplier_delay_pushes_back_deliveries(delayed):
    first = q(delayed, "SELECT MIN(ts) FROM fact_inventory WHERE category = 'electronics' "
                       "AND delivered_units > 0 AND ts >= '2026-10-03'")
    assert first == datetime(2026, 10, 10, 6)       # PO of 2026-10-01 06:00 (cycle anchored at start) + 9 days
    assert q(delayed, "SELECT MIN(ts) FROM fact_inventory WHERE category = 'fashion' "
                      "AND delivered_units > 0 AND ts >= '2026-10-03'") == datetime(2026, 10, 4, 6)


def test_stockout_removes_orders(delayed):
    ref = Simulator(":memory:", seed=42, start=datetime(2026, 9, 28), scenarios=None)
    ref.run_until(datetime(2026, 10, 14))
    sql = ("SELECT SUM(orders) FROM fact_commerce WHERE category = '{}' "
           "AND ts >= '2026-10-07' AND ts < '2026-10-10'")
    assert q(delayed, sql.format("electronics")) / q(ref, sql.format("electronics")) < 0.7
    assert q(delayed, sql.format("fashion")) / q(ref, sql.format("fashion")) == pytest.approx(1.0, abs=0.02)


def test_oos_feed_clicks_cannot_convert(delayed):
    sql = ("SELECT SUM(feed_oos_impressions) / SUM(feed_impressions) FROM fact_commerce "
           "WHERE category = 'electronics' AND ts >= '2026-10-08' AND ts < '2026-10-09'")
    assert q(delayed, sql) > 0.4


# ------------------------------------------------------------ pacing_bug
PACE = "SELECT SUM(spend_eur) / SUM(planned_spend_eur) FROM fact_ads WHERE advertiser = '{}' AND {}"
MORNING = "ts >= '{d} 06:00' AND ts < '{d} 14:00'"


def test_pacing_bug_overspends_adv_017(world):
    assert q(world, PACE.format("adv_017", MORNING.format(d="2026-10-28"))) > 1.1
    assert q(world, PACE.format("adv_017", MORNING.format(d="2026-10-27"))) == pytest.approx(1.0, abs=0.1)


def test_pacing_bug_only_adv_017(world):
    r = q(world, "SELECT SUM(spend_eur) / SUM(planned_spend_eur) FROM fact_ads WHERE advertiser <> 'adv_017' "
                 "AND ts >= '2026-10-28 06:00' AND ts < '2026-10-28 14:00'")
    assert r == pytest.approx(1.0, abs=0.05)


def test_pacing_bug_exhausts_budget_early(world, baseline):
    evening = "ts >= '2026-10-28 20:00' AND ts < '2026-10-28 23:00'"
    assert q(world, PACE.format("adv_017", evening)) < 0.2
    assert q(baseline, PACE.format("adv_017", evening)) > 0.6
    day = "ts >= '2026-10-27 23:00' AND ts < '2026-10-28 23:00'"        # one CET day
    spent = q(world, f"SELECT SUM(spend_eur) FROM fact_ads WHERE advertiser = 'adv_017' AND {day}")
    planned = q(world, f"SELECT SUM(planned_spend_eur) FROM fact_ads WHERE advertiser = 'adv_017' AND {day}")
    assert spent <= planned * 1.01


def test_exhausted_budget_stops_impressions(world):
    rows = world.con.execute("""SELECT impressions, clicks FROM fact_ads WHERE advertiser = 'adv_017'
                                AND ts >= '2026-10-28 14:00' AND ts < '2026-10-28 23:00' AND clicks = 0""").fetchall()
    assert rows and all(i == 0 for i, _ in rows)
    ctr = q(world, "SELECT SUM(clicks) / SUM(impressions) FROM fact_ads WHERE advertiser = 'adv_017' "
                   "AND ts >= '2026-10-28' AND ts < '2026-10-29'")
    assert ctr == pytest.approx(0.015, abs=0.002)


def test_pacing_bug_logs_no_event(world):
    assert q(world, "SELECT COUNT(*) FROM events WHERE id LIKE 'pacing_bug%'") == 0


# ----------------------------------------------------------------- inject
def test_inject_starts_scenario_now():
    sim = Simulator(":memory:", seed=5, start=datetime(2026, 10, 20), scenarios=load_scenarios())
    sim.run_until(datetime(2026, 10, 21, 3))
    s = sim.inject("android_payment_de")
    assert s.start == datetime(2026, 10, 21, 3) and s.end == datetime(2026, 10, 21, 9)
    sim.run_until(datetime(2026, 10, 21, 15))
    seg = "country = 'de' AND platform = 'android' AND ts >= '{}' AND ts < '{}'"
    assert q(sim, PSR.format(seg.format("2026-10-21 03:00", "2026-10-21 09:00"))) == pytest.approx(0.71, abs=0.03)
    assert q(sim, PSR.format(seg.format("2026-10-21 09:00", "2026-10-21 15:00"))) == pytest.approx(0.94, abs=0.02)
    assert q(sim, PSR.format(seg.format("2026-10-20 00:00", "2026-10-21 03:00"))) == pytest.approx(0.94, abs=0.02)
    ids = [r[0] for r in sim.con.execute("SELECT id FROM events WHERE id LIKE 'android%' ORDER BY ts").fetchall()]
    assert ids == ["android_payment_de@2026-10-21T03", "android_payment_de@2026-10-21T03_end"]


def test_inject_unknown_scenario():
    with pytest.raises(KeyError):
        Simulator(":memory:").inject("nope")


def test_excluded_scenario_can_still_be_injected():
    sim = Simulator(":memory:", seed=5, start=datetime(2026, 10, 20), exclude=["pacing_bug"])
    assert "pacing_bug" not in [s.id for s in sim.scenarios.scenarios]
    sim.inject("pacing_bug")
    assert any(s.id.startswith("pacing_bug@") for s in sim.scenarios.scenarios)


def test_ad_spend_never_exceeds_daily_budget(world):
    from zoneinfo import ZoneInfo
    from shoply.sim import params as P
    df = world.con.execute("SELECT ts, country, advertiser, spend_eur, planned_spend_eur FROM fact_ads").df()
    utc = pd.to_datetime(df["ts"]).dt.tz_localize("UTC")
    df["day"] = [t.tz_convert(ZoneInfo(P.TIMEZONES[c])).date() for t, c in zip(utc, df["country"])]
    grp = df.groupby(["country", "advertiser", "day"])
    daily = grp[["spend_eur", "planned_spend_eur"]].sum()
    full = daily[grp.size() >= 23]                     # complete local days (23/25 h on DST days)
    assert (full["spend_eur"] <= full["planned_spend_eur"] * 1.0001).all()
