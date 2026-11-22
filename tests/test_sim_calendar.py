"""calendar.yaml: retail events, seasonality, holidays, payday, growth, structural changes,
past incidents and ground truth. Every simulation here covers at most three weeks.

Effects are measured against the same seed with the entry excluded, so (with common
random numbers) the difference is the entry and not sampling noise.
"""
import csv
import os
import time
from datetime import date, datetime, timedelta

import pytest
import yaml

from shoply.sim import params as P
from shoply.sim.calendar import (DEFAULT_CALENDAR, ground_truth, load_calendar, utc,
                                 write_ground_truth)
from shoply.sim.simulator import Simulator
from tests.conftest import q

SEED = 42
INCIDENTS = ["checkout_bug_web_fr", "search_relevance_es", "supplier_delay_electronics",
             "recs_model_shift_for_you", "ads_pacing_bug_adv_005"]


def sim(start, until, **kw):
    s = Simulator(":memory:", seed=SEED, scenarios=None, start=utc(start), **kw)
    s.run_until(utc(until))
    return s


def local_day(d: date) -> str:
    """SQL predicate for one Europe/Berlin day."""
    return f"ts >= '{utc(d)}' AND ts < '{utc(d + timedelta(days=1))}'"


def sessions(s, where="TRUE"):
    return q(s, f"SELECT SUM(sessions) FROM fact_traffic WHERE {where}")


# --------------------------------------------------------------------- loading
CAL = load_calendar()


def test_calendar_entries_are_planned_and_scoped():
    raw = yaml.safe_load(open(DEFAULT_CALENDAR))
    assert {"retail", "seasonality", "holidays", "payday", "growth", "structural", "incidents"} <= set(raw)
    for s in CAL.scenarios:
        kind = CAL.kinds[s.id]
        if kind == "incident":
            assert s.event["type"] == "release" and s.end_event and s.end is not None
            assert not s.event.get("planned") and s.scope
        elif kind != "baseline":
            assert s.event["planned"] is True and s.effects or s.event["expected_effect"], s.id


def test_calendar_covers_the_year():
    ids = CAL.ids
    assert {"black_friday_2025", "cyber_monday_2025", "christmas_ramp_2025", "christmas_peak_2025",
            "christmas_low_2025", "january_sales_2026", "summer_sale_2026"} <= ids
    assert {"season_aug_fr_2026-08", "season_aug_es_2026-08", "season_aug_de_2026-08",
            "season_sep_toys_2026-09", "season_sep_fashion_2026-09", "season_nov_2025-11",
            "season_dec_2025-12"} <= ids
    holidays = {i for i in ids if i.startswith("holiday_")}
    assert holidays == {"holiday_de_2026-10-03", "holiday_de_2026-04-06", "holiday_de_2026-05-01",
                        "holiday_fr_2026-07-14", "holiday_fr_2026-08-15", "holiday_fr_2026-05-01",
                        "holiday_es_2026-10-12", "holiday_es_2026-08-15", "holiday_nl_2026-04-27",
                        "holiday_pl_2026-05-03", "holiday_pl_2025-11-01"}
    assert {f"payday_{m}" for m in ("2025-11", "2026-03", "2026-10")} <= ids
    assert set(INCIDENTS) <= ids and {"fashion_take_rate_2026_05", "paid_budget_2026_02"} <= ids
    assert P.CATEGORIES == ["electronics", "fashion", "home", "beauty", "toys"]       # no "sports"


def test_dates_are_local_berlin_days_across_dst():
    s = {x.id: x for x in CAL.scenarios}
    assert (s["black_friday_2025"].start, s["black_friday_2025"].end) == (datetime(2025, 11, 27, 23),
                                                                          datetime(2025, 11, 28, 23))
    assert s["checkout_bug_web_fr"].start == datetime(2026, 3, 10, 13)          # CET
    assert s["search_relevance_es"].start == datetime(2026, 4, 15, 8)           # CEST
    assert s["payday_2026-04"].start == datetime(2026, 3, 29, 22)              # 2026-03-30 local, after DST
    assert s["payday_2026-04"].end - s["payday_2026-04"].start == timedelta(hours=96)
    assert s["payday_2026-03"].end - s["payday_2026-03"].start == timedelta(hours=96)
    assert s["season_aug_fr_2026-08"].start == datetime(2026, 7, 31, 22)
    assert Simulator._local(datetime(2026, 3, 29, 0)).hour == 1                 # CET
    assert Simulator._local(datetime(2026, 3, 29, 1)).hour == 3                 # 02:00 is skipped


def test_growth_is_15_percent_per_year():
    assert CAL.growth(CAL.growth_anchor + timedelta(days=365)) == pytest.approx(1.15)
    assert CAL.growth(date(2025, 10, 27)) == pytest.approx(1.15 ** (-(date(2026, 8, 31) - date(2025, 10, 27)).days / 365))


def test_history_starts_2025_10_27():
    assert P.YEAR_START == datetime(2025, 10, 27) and P.LIVE_START == datetime(2026, 10, 26)
    with pytest.raises(ValueError):
        Simulator(":memory:", start=datetime(2025, 10, 26))


# ----------------------------------------------------------------- black friday
@pytest.fixture(scope="module")
def november():
    """2025-11-20 .. 2025-11-30: Black Friday, the Friday before and the start of the December payday."""
    return sim(date(2025, 11, 20), date(2025, 11, 30))


def test_black_friday_at_least_double_prior_friday(november):
    bf = sessions(november, local_day(date(2025, 11, 28)))
    prior = sessions(november, local_day(date(2025, 11, 21)))
    assert bf >= 2 * prior
    assert bf / prior == pytest.approx(2.5, rel=0.05)


def test_black_friday_triples_discounts(november):
    sql = "SELECT SUM(discounts_eur) / SUM(gmv_eur) FROM fact_commerce WHERE {}"
    r = q(november, sql.format(local_day(date(2025, 11, 28)))) / q(november, sql.format(local_day(date(2025, 11, 21))))
    assert r == pytest.approx(3.0, rel=0.02)


def test_calendar_events_logged_planned(november):
    rows = november.con.execute("SELECT id, type, planned, ts FROM events WHERE planned ORDER BY ts").fetchall()
    ids = {r[0] for r in rows}
    assert {"black_friday_2025", "payday_2025-12"} <= ids
    bf = next(r for r in rows if r[0] == "black_friday_2025")
    assert bf[1] == "campaign" and bf[3] == datetime(2025, 11, 27, 23)


def test_payday_lifts_sessions():
    """2025-11-24 .. 12-01 with and without the payday window: x1.05 on Nov 29-30, nothing elsewhere."""
    on = sim(date(2025, 11, 26), date(2025, 12, 1))
    off = sim(date(2025, 11, 26), date(2025, 12, 1), exclude=["payday_2025-12"])
    assert sessions(on, local_day(date(2025, 11, 29))) / sessions(off, local_day(date(2025, 11, 29))) == \
        pytest.approx(1.05, abs=0.01)
    assert sessions(on, local_day(date(2025, 11, 27))) == sessions(off, local_day(date(2025, 11, 27)))


# ------------------------------------------------------------- summer, august
@pytest.fixture(scope="module")
def summer():
    """2026-07-20 .. 2026-08-10 (3 weeks): end of July, start of August, recs model shift."""
    return sim(date(2026, 7, 20), date(2026, 8, 10))


def week(start: date) -> str:
    return f"ts >= '{utc(start)}' AND ts < '{utc(start + timedelta(days=7))}'"


def test_august_fr_sessions_below_july(summer):
    jul, aug = week(date(2026, 7, 20)), week(date(2026, 8, 3))                  # Mon..Sun, no payday
    fr = sessions(summer, f"country = 'fr' AND {aug}") / sessions(summer, f"country = 'fr' AND {jul}")
    de = sessions(summer, f"country = 'de' AND {aug}") / sessions(summer, f"country = 'de' AND {jul}")
    assert fr < 1 and fr == pytest.approx(0.75, abs=0.03)
    assert de == pytest.approx(0.85, abs=0.03) and fr < de


def test_country_holiday_only_in_that_country():
    on = sim(date(2026, 4, 26), date(2026, 4, 28))
    off = sim(date(2026, 4, 26), date(2026, 4, 28), exclude=["holiday_nl_2026-04-27"])
    day = local_day(date(2026, 4, 27))
    assert sessions(on, f"country = 'nl' AND {day}") / sessions(off, f"country = 'nl' AND {day}") == \
        pytest.approx(0.8, abs=0.02)
    assert sessions(on, f"country <> 'nl' AND {day}") == sessions(off, f"country <> 'nl' AND {day}")
    assert q(on, "SELECT type || '|' || scope FROM events WHERE id = 'holiday_nl_2026-04-27'") == "holiday|country=nl"


def test_back_to_school_lifts_toys_and_fashion_carts():
    on = sim(date(2026, 9, 1), date(2026, 9, 3))
    off = sim(date(2026, 9, 1), date(2026, 9, 3), exclude=["season_sep_toys_2026-09", "season_sep_fashion_2026-09"])
    sql = "SELECT SUM(carts) / SUM(search_views + feed_views + direct_views) FROM fact_commerce WHERE category = '{}'"
    assert q(on, sql.format("toys")) / q(off, sql.format("toys")) == pytest.approx(1.1, abs=0.03)
    assert q(on, sql.format("home")) / q(off, sql.format("home")) == pytest.approx(1.0, abs=0.01)


# --------------------------------------------------------------- structural changes
def test_fashion_take_rate_plus_one_point():
    s = sim(date(2026, 4, 29), date(2026, 5, 3))
    sql = "SELECT SUM(fees_eur) / SUM(gmv_eur) FROM fact_commerce WHERE category = '{}' AND {}"
    assert q(s, sql.format("fashion", local_day(date(2026, 4, 30)))) == pytest.approx(0.15, abs=1e-9)
    assert q(s, sql.format("fashion", local_day(date(2026, 5, 2)))) == pytest.approx(0.16, abs=1e-9)
    assert q(s, sql.format("home", local_day(date(2026, 5, 2)))) == pytest.approx(P.CATEGORY_TAKE_RATE["home"])
    e = s.con.execute("SELECT type, planned, scope FROM events WHERE id = 'fashion_take_rate_2026_05'").fetchall()
    assert e == [("config", True, "category=fashion")]


def test_paid_budget_increase():
    """params hold today's paid budget; before 2026-02-01 paid sessions were 1/1.2 of it."""
    on = sim(date(2026, 1, 31), date(2026, 2, 2))
    off = sim(date(2026, 1, 31), date(2026, 2, 2), exclude=["paid_budget_2026_02", "paid_budget_2026_02_before"])
    jan, feb = local_day(date(2026, 1, 31)), local_day(date(2026, 2, 1))
    assert sessions(off, f"channel = 'paid' AND {jan}") / sessions(on, f"channel = 'paid' AND {jan}") == \
        pytest.approx(1.2, abs=0.02)
    assert sessions(on, f"channel = 'paid' AND {feb}") == sessions(off, f"channel = 'paid' AND {feb}")
    assert sessions(on, "channel <> 'paid'") == sessions(off, "channel <> 'paid'")
    e = on.con.execute("SELECT type, planned, scope, expected_effect::VARCHAR FROM events "
                       "WHERE id = 'paid_budget_2026_02'").fetchall()
    assert e == [("config", True, "channel=paid", '{"sessions": 1.2}')]


# ------------------------------------------------------------------ past incidents
WINDOWS = {   # incident -> (slice start, slice end) local days, at most three weeks
    "checkout_bug_web_fr": (date(2026, 3, 10), date(2026, 3, 11)),
    "search_relevance_es": (date(2026, 4, 15), date(2026, 4, 16)),
    "supplier_delay_electronics": (date(2026, 6, 1), date(2026, 6, 13)),
    "recs_model_shift_for_you": (date(2026, 7, 19), date(2026, 7, 23)),
    "ads_pacing_bug_adv_005": (date(2026, 9, 7), date(2026, 9, 10)),
}
CHECKS = {    # incident -> (sql with {where}, in-scope predicate, expected in-scope ratio or a bound)
    "checkout_bug_web_fr": ("SELECT SUM(checkouts) / SUM(carts) FROM fact_commerce WHERE {}",
                            "country = 'fr' AND platform = 'web'", 0.7),
    "search_relevance_es": ("SELECT SUM(relevance_sum) / SUM(relevance_n) FROM fact_traffic WHERE {}",
                            "country = 'es'", 0.7),
    "supplier_delay_electronics": ("SELECT SUM(in_stock * demand_weight) / SUM(demand_weight) FROM fact_inventory "
                                   "WHERE {}", "category = 'electronics'", "lower"),
    "recs_model_shift_for_you": ("SELECT SUM(feed_oos_impressions) / SUM(feed_impressions) FROM fact_commerce "
                                 "WHERE {}", "channel = 'for_you'", "higher"),
    "ads_pacing_bug_adv_005": ("SELECT SUM(spend_eur) FROM fact_ads WHERE {}", "advertiser = 'adv_005'", "higher"),
}
OUT_OF_SCOPE = {   # incident -> the rest of the warehouse, same metric
    "checkout_bug_web_fr": "NOT (country = 'fr' AND platform = 'web')",
    "search_relevance_es": "country <> 'es'",
    "supplier_delay_electronics": "category <> 'electronics'",
    "recs_model_shift_for_you": "channel <> 'for_you'",
    "ads_pacing_bug_adv_005": "advertiser <> 'adv_005'",
}
UNTOUCHED = {      # incident -> rows that must be identical with and without it
    "checkout_bug_web_fr": [("fact_traffic", "TRUE"), ("fact_ads", "TRUE")],
    "search_relevance_es": [("fact_traffic", "country <> 'es'"), ("fact_ads", "TRUE")],
    "supplier_delay_electronics": [("fact_traffic", "TRUE"), ("fact_ads", "TRUE"),
                                   ("fact_inventory", "category <> 'electronics'")],
    "recs_model_shift_for_you": [("fact_traffic", "TRUE"), ("fact_ads", "TRUE")],
    "ads_pacing_bug_adv_005": [("fact_traffic", "TRUE"), ("fact_commerce", "TRUE"), ("fact_inventory", "TRUE"),
                               ("fact_ads", "advertiser <> 'adv_005'")],
}


def rows(s, table, where):
    return s.con.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY ALL").fetchall()


@pytest.mark.parametrize("iid", INCIDENTS)
def test_incident_visible_only_in_its_scope(iid):
    a, b = WINDOWS[iid]
    on, off = sim(a, b), sim(a, b, exclude=[iid])
    inc = next(s for s in CAL.scenarios if s.id == iid)
    during = f"ts >= '{inc.start}' AND ts < '{inc.end}'"
    if iid == "supplier_delay_electronics":   # orders placed in the window arrive 10 days late
        during = f"ts >= '{inc.start + timedelta(days=4)}' AND ts < '{inc.start + timedelta(days=11)}'"
    if iid == "ads_pacing_bug_adv_005":       # overspends, then the daily budget runs out
        during = f"ts >= '{inc.start}' AND ts < '{inc.start + timedelta(hours=6)}'"
    sql, scope, expected = CHECKS[iid]
    ratio = lambda where: q(on, sql.format(f"{where} AND {during}")) / q(off, sql.format(f"{where} AND {during}"))
    r = ratio(scope)
    if expected == "lower":
        assert r < 0.9
    elif expected == "higher":
        assert r > 1.5
    else:
        assert r == pytest.approx(expected, abs=0.03)
    # outside the scope: same metric unchanged (shared stock may move it slightly), raw rows identical
    assert ratio(OUT_OF_SCOPE[iid]) == pytest.approx(1.0, abs=0.02)
    for table, where in UNTOUCHED[iid]:
        assert rows(on, table, where) == rows(off, table, where), table
    before = f"ts < '{inc.start}'"
    for t in ("fact_traffic", "fact_commerce", "fact_inventory", "fact_ads"):
        assert rows(on, t, before) == rows(off, t, before)


@pytest.mark.parametrize("iid", INCIDENTS)
def test_incident_has_release_and_resolution_events(iid):
    inc = next(s for s in CAL.scenarios if s.id == iid)
    evs = {e.id: e for e in inc.events()}
    assert set(evs) == {iid, f"{iid}_end"}
    assert evs[iid].ts == inc.start and evs[f"{iid}_end"].ts == inc.end
    assert all(e.type == "release" and not e.planned for e in evs.values())
    assert evs[iid].scope == inc.scope


def test_incident_events_written_to_warehouse():
    s = sim(date(2026, 3, 10), date(2026, 3, 11))
    got = s.con.execute("SELECT id, ts, type, planned, scope FROM events WHERE id LIKE 'checkout_bug%' "
                        "ORDER BY ts").fetchall()
    assert got == [("checkout_bug_web_fr", datetime(2026, 3, 10, 13), "release", False, "country=fr|platform=web"),
                   ("checkout_bug_web_fr_end", datetime(2026, 3, 10, 19), "release", False,
                    "country=fr|platform=web")]


# ------------------------------------------------------------------- ground truth
def test_ground_truth_lists_every_event_and_incident(tmp_path):
    csv_path, yaml_path = write_ground_truth(tmp_path)
    with open(csv_path) as f:
        table = list(csv.DictReader(f))
    listed = yaml.safe_load(open(yaml_path))
    ids = {r["id"] for r in table}
    assert ids == {r["id"] for r in listed}
    assert {s.id for s in CAL.scenarios if CAL.kinds[s.id] != "baseline"} <= ids
    assert {"autumn_campaign", "pacing_bug", "growth_trend"} <= ids
    for r in table:
        if r["id"] in INCIDENTS:
            assert r["kind"] == "incident" and r["planned"] == "False" and r["end"] and r["scope"]
    bf = next(r for r in listed if r["id"] == "black_friday_2025")
    assert bf["planned"] is True and bf["effects"] == {"sessions": 2.5, "discount_rate": 3.0}


def test_committed_ground_truth_is_up_to_date():
    root = os.path.dirname(DEFAULT_CALENDAR)
    with open(os.path.join(root, "ground_truth.csv")) as f:
        committed = list(csv.DictReader(f))
    assert committed == [{k: str(v) for k, v in r.items()} for r in ground_truth()]


# ---------------------------------------------------------------- full-year timing
@pytest.mark.skipif(not os.environ.get("SHOPLY_FULL_YEAR"), reason="set SHOPLY_FULL_YEAR=1 (about 1-2 min)")
def test_full_year_generation_under_3_minutes():
    t = time.perf_counter()
    s = Simulator(":memory:", seed=SEED, start=P.YEAR_START)
    s.build_history()
    assert time.perf_counter() - t < 180
    assert q(s, "SELECT MIN(ts) FROM fact_traffic") == P.YEAR_START
    assert q(s, "SELECT MAX(ts) FROM fact_traffic") == P.LIVE_START - timedelta(hours=1)


def test_excluding_a_structural_change_drops_its_baseline_too():
    sim = Simulator(":memory:", exclude=["fashion_take_rate_2026_05"])
    assert not [s.id for s in sim.scenarios.scenarios if s.id.startswith("fashion_take_rate_2026_05")]


def test_ground_truth_leaves_out_excluded_ids():
    ids = {r["id"] for r in ground_truth(exclude=["checkout_bug_web_fr"])}
    assert "checkout_bug_web_fr" not in ids and "search_relevance_es" in ids


def test_start_must_be_a_whole_hour():
    with pytest.raises(ValueError):
        Simulator(":memory:", start=datetime(2026, 8, 31, 0, 30))


def test_excluding_only_a_baseline_drops_it():
    sim = Simulator(":memory:", exclude=["paid_budget_2026_02_before"])
    ids = {s.id for s in sim.scenarios.scenarios}
    assert "paid_budget_2026_02_before" not in ids and "paid_budget_2026_02" in ids
