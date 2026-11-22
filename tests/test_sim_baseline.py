"""Baseline behaviour from the simulator spec, measured on history (no scenario is active)."""
from datetime import date, datetime

import pytest

from shoply.sim import params as P
from shoply.sim.simulator import HOURLY, Simulator, expected_daily_orders
from tests.conftest import q

CLEAN = "ts < TIMESTAMP '2026-10-26'"
DAYS = 56


def test_history_has_no_scenario_events(world):
    assert q(world, f"SELECT COUNT(*) FROM events WHERE {CLEAN} AND type NOT IN ('release', 'holiday')") == 0
    assert q(world, f"SELECT COUNT(*) FROM events WHERE {CLEAN} AND description NOT LIKE '%routine release' "
                    "AND type = 'release'") == 0


def test_history_equals_no_scenario_run(world, baseline):
    sql = f"SELECT SUM(orders), SUM(gmv_eur), SUM(feed_oos_impressions) FROM fact_commerce WHERE {CLEAN}"
    assert world.con.execute(sql).fetchall() == baseline.con.execute(sql).fetchall()


def test_hour_curve():
    assert HOURLY.mean() == pytest.approx(1.0)
    assert int(HOURLY.argmax()) == P.PEAK_LOCAL_HOUR
    assert HOURLY[13] > 1 > HOURLY[10]                                     # lunch bump
    assert HOURLY.min() / HOURLY.max() > 0.2


def test_sessions_per_day(world):
    # first full weekdays: lambda = 600k/day * (1 + 0.001 * day)
    wk = q(world, "SELECT SUM(sessions) / 5 FROM fact_traffic WHERE ts >= '2026-08-31' AND ts < '2026-09-05'")
    assert wk == pytest.approx(600_000 * 1.002, rel=0.03)
    assert q(world, f"SELECT SUM(sessions) / {DAYS} FROM fact_traffic WHERE {CLEAN}") == pytest.approx(600_000, rel=0.08)


def test_trend(world):
    sql = "SELECT SUM(sessions) FROM fact_traffic WHERE ts >= '{}' AND ts < '{}'"
    early = q(world, sql.format("2026-09-07", "2026-09-21"))
    late = q(world, sql.format("2026-10-05", "2026-10-19"))
    assert late / early == pytest.approx(1.028 / 1.0, abs=0.02)


def test_dimension_shares(world):
    tot = q(world, f"SELECT SUM(sessions) FROM fact_traffic WHERE {CLEAN}")
    for col, val, share in [("country", "de", .40), ("country", "pl", .10), ("platform", "android", .45),
                            ("channel", "organic", .60), ("channel", "for_you", .15)]:
        s = q(world, f"SELECT SUM(sessions) FROM fact_traffic WHERE {CLEAN} AND {col} = ?", val)
        assert s / tot == pytest.approx(share, abs=0.015)
    views = q(world, f"SELECT SUM(direct_views) FROM fact_commerce WHERE {CLEAN}")
    for cat, share in P.CATEGORY_VIEW_SHARE.items():
        v = q(world, f"SELECT SUM(direct_views) FROM fact_commerce WHERE {CLEAN} AND category = ?", cat)
        assert v / views == pytest.approx(share, abs=0.005)


def test_funnel_rates(world):
    r = world.con.execute(f"""
        SELECT SUM(search_views + feed_views + direct_views), SUM(direct_views), SUM(feed_views),
               SUM(feed_impressions), SUM(carts), SUM(checkouts), SUM(orders), SUM(gmv_eur), SUM(fees_eur),
               SUM(discounts_eur)
        FROM fact_commerce WHERE {CLEAN}""").fetchall()[0]
    views, direct, feed, impr, carts, checkouts, orders, gmv, fees, disc = r
    sessions, searches, rel_sum, rel_n = world.con.execute(
        f"SELECT SUM(sessions), SUM(searches), SUM(relevance_sum), SUM(relevance_n) FROM fact_traffic "
        f"WHERE {CLEAN}").fetchall()[0]
    assert direct / sessions == pytest.approx(1.2, abs=0.01)
    assert feed / impr == pytest.approx(0.06, abs=0.001)
    assert searches / sessions == pytest.approx(0.85 * 0.95 + 0.15 * 0.40, abs=0.01)
    assert rel_sum / rel_n == pytest.approx(0.80, abs=0.002)
    assert checkouts / carts == pytest.approx(0.35, abs=0.005)
    assert 0.66 <= 1 - orders / carts <= 0.72
    assert orders / checkouts == pytest.approx(0.94, abs=0.003)
    assert 0.026 <= orders / sessions <= 0.036
    assert orders / DAYS == pytest.approx(expected_daily_orders().sum() * 1.05, rel=0.06)
    assert 40 < gmv / orders < 52
    assert 0.11 < fees / gmv < 0.13
    assert disc / gmv == pytest.approx(0.03)


def test_search_views_by_channel(world):
    rows = world.con.execute(f"""
        SELECT t.channel, s.v / t.n FROM (SELECT channel, SUM(searches) n FROM fact_traffic WHERE {CLEAN} GROUP BY 1) t
        JOIN (SELECT channel, SUM(search_views) v FROM fact_commerce WHERE {CLEAN} GROUP BY 1) s USING (channel)
    """).fetchall()
    for _, ctr in rows:
        assert ctr == pytest.approx(0.45, abs=0.003)


def test_heterogeneous_base_rates(world):
    conv = dict(world.con.execute(f"""
        SELECT c.country, c.o / t.s FROM
          (SELECT country, SUM(orders) o FROM fact_commerce WHERE {CLEAN} GROUP BY 1) c
          JOIN (SELECT country, SUM(sessions) s FROM fact_traffic WHERE {CLEAN} GROUP BY 1) t USING (country)
    """).fetchall())
    assert conv["de"] > conv["fr"] > conv["pl"]
    aov = dict(world.con.execute(f"SELECT platform, SUM(gmv_eur) / SUM(orders) FROM fact_commerce "
                                 f"WHERE {CLEAN} GROUP BY 1").fetchall())
    assert aov["web"] > aov["ios"] > aov["android"]


def test_peak_at_20_local(world):
    # CEST (UTC+2) in history; Europe/Berlin for every country
    for country in ["de", "pl"]:
        h = q(world, f"""SELECT h FROM (SELECT hour(ts) h, SUM(sessions) s FROM fact_traffic
                         WHERE country = '{country}' AND {CLEAN} GROUP BY 1) ORDER BY s DESC LIMIT 1""")
        assert h == 18


def test_local_time_follows_dst():
    assert Simulator._local(datetime(2026, 10, 24, 18)).hour == 20       # CEST
    assert Simulator._local(datetime(2026, 10, 26, 19)).hour == 20       # CET after 2026-10-25
    assert Simulator._local(datetime(2026, 10, 25, 0)) == (2, True, date(2026, 10, 25))
    assert Simulator._local(datetime(2026, 10, 25, 1)).hour == 2         # 02:00 happens twice


@pytest.mark.parametrize("mobile,mult", [(True, 1.15), (False, 0.90)])
def test_weekend_effect(world, mobile, mult):
    plat = "platform <> 'web'" if mobile else "platform = 'web'"
    # 04:00-20:00 UTC is the same local calendar day
    sql = f"""SELECT AVG(s) FROM (SELECT date_trunc('day', ts) d, SUM(sessions) s FROM fact_traffic
              WHERE {CLEAN} AND {plat} AND hour(ts) BETWEEN 4 AND 20 AND dayofweek(ts) {{}} GROUP BY 1)"""
    weekend = q(world, sql.format("IN (0, 6)"))
    weekday = q(world, sql.format("BETWEEN 1 AND 5"))
    assert weekend / weekday == pytest.approx(mult, abs=0.03)


def test_holiday(world):
    e = world.con.execute("SELECT type, scope, ts FROM events WHERE id = 'holiday_de_2026-10-03'").fetchall()[0]
    assert e[0] == "holiday" and e[1] == "country=de"
    assert str(e[2]) == "2026-10-02 22:00:00"
    # A/B on the same seed (common random numbers): DE sessions x0.8 for the Berlin day, other countries unchanged
    runs = []
    for holidays in (True, False):
        sim = Simulator(":memory:", seed=42, scenarios=None, start=datetime(2026, 10, 2))
        if not holidays:
            sim._holiday_mult = {}
        sim.run_until(datetime(2026, 10, 4, 6))
        runs.append(sim)
    sql = ("SELECT SUM(sessions) FROM fact_traffic WHERE country = '{}' "
           "AND ts >= TIMESTAMP '2026-10-02 22:00' AND ts < TIMESTAMP '2026-10-03 22:00'")
    assert q(runs[0], sql.format("de")) / q(runs[1], sql.format("de")) == pytest.approx(0.8, abs=0.01)
    assert q(runs[0], sql.format("fr")) == q(runs[1], sql.format("fr"))
    edge = "SELECT SUM(sessions) FROM fact_traffic WHERE country = 'de' AND ts = TIMESTAMP '{}'"
    assert q(runs[0], edge.format("2026-10-02 21:00")) == q(runs[1], edge.format("2026-10-02 21:00"))
    assert q(runs[0], edge.format("2026-10-03 22:00")) == q(runs[1], edge.format("2026-10-03 22:00"))


def test_inventory_baseline(world):
    r = q(world, f"""SELECT AVG(r) FROM (SELECT ts, category, SUM(in_stock * demand_weight) / SUM(demand_weight) r
                     FROM fact_inventory WHERE {CLEAN} GROUP BY ALL)""")
    assert r > 0.97
    # a supplier delivery every 3 days at 06:00 UTC, for every category
    days = world.con.execute(f"""SELECT category, COUNT(DISTINCT ts), MIN(hour(ts)), MAX(hour(ts)) FROM fact_inventory
                                 WHERE {CLEAN} AND delivered_units > 0 GROUP BY 1""").fetchall()
    assert len(days) == 5 and all(17 <= n <= 19 and lo == hi == 6 for _, n, lo, hi in days)
    first = q(world, "SELECT MIN(ts) FROM fact_inventory WHERE delivered_units > 0")
    assert str(first) == "2026-09-03 06:00:00"                     # PO of 2026-08-31 06:00 + lead_days 3
    assert q(world, "SELECT ROUND(SUM(demand_weight), 6) FROM fact_inventory WHERE ts = '2026-09-01'") == 5
    assert q(world, "SELECT COUNT(DISTINCT sku) FROM fact_inventory") == 200


def test_ads_on_plan(world):
    ratio = q(world, f"SELECT SUM(spend_eur) / SUM(planned_spend_eur) FROM fact_ads WHERE {CLEAN}")
    assert ratio == pytest.approx(1.0, abs=0.03)
    assert q(world, f"SELECT SUM(planned_spend_eur) / {DAYS} FROM fact_ads WHERE {CLEAN}") == pytest.approx(25_000, rel=0.01)
    cpc = q(world, f"SELECT SUM(spend_eur) / SUM(clicks) FROM fact_ads WHERE {CLEAN}")
    assert 0.2 < cpc < 0.6
    assert q(world, "SELECT COUNT(DISTINCT advertiser) FROM fact_ads") == 30


def test_background_releases(world):
    rows = world.con.execute(f"""SELECT service, scope, planned FROM events
                                 WHERE {CLEAN} AND description LIKE '%routine release'""").fetchall()
    assert len(rows) / DAYS == pytest.approx(P.BACKGROUND_RELEASES_PER_DAY, rel=0.3)
    assert {r[0] for r in rows} <= set(P.RELEASE_SERVICES) and len({r[0] for r in rows}) >= 8
    assert not any(r[2] for r in rows)
