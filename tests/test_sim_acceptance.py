"""Acceptance tests from the simulator spec (seed 42).

A/B: `world` runs every scenario, `baseline` the same seed without scenarios.
"""
from datetime import datetime

import pandas as pd
import pytest

from shoply.sim.simulator import Simulator
from shoply.warehouse.schema import COLUMNS
from tests.conftest import q
from tests.test_sim_determinism import dump

HIST = "ts < TIMESTAMP '2026-10-26'"
FAULT = "ts >= '2026-10-26 15:00' AND ts < '2026-10-26 21:00'"
AFTER = "ts >= '2026-10-26 21:00' AND ts < '2026-10-27 09:00'"
CAMPAIGN = "ts >= '2026-10-26 06:00' AND ts < '2026-10-26 12:00'"
PSR = "SELECT SUM(orders) / SUM(checkouts) FROM fact_commerce WHERE country = '{}' AND platform = '{}' AND {}"


def conversion(sim, where):
    return (q(sim, f"SELECT SUM(orders) FROM fact_commerce WHERE {where}")
            / q(sim, f"SELECT SUM(sessions) FROM fact_traffic WHERE {where}"))


def test_conversion_in_range(world):
    assert 0.026 <= conversion(world, HIST) <= 0.036


def test_cart_abandonment_in_range(world):
    abandonment = 1 - q(world, f"SELECT SUM(orders) / SUM(carts) FROM fact_commerce WHERE {HIST}")
    assert 0.66 <= abandonment <= 0.72


def test_about_600k_sessions_per_day(world):
    daily = world.con.execute(f"SELECT date_trunc('day', ts) d, SUM(sessions) FROM fact_traffic "
                              f"WHERE {HIST} GROUP BY 1").fetchall()
    assert len(daily) == 56
    assert all(500_000 < s < 750_000 for _, s in daily)
    assert sum(s for _, s in daily) / 56 == pytest.approx(600_000, rel=0.08)


def test_determinism_two_runs_identical():
    a = Simulator(":memory:", seed=42, start=datetime(2026, 10, 26))
    b = Simulator(":memory:", seed=42, start=datetime(2026, 10, 26))
    a.run_until(datetime(2026, 10, 27))
    b.run_until(datetime(2026, 10, 27))
    for t in COLUMNS:
        pd.testing.assert_frame_equal(dump(a, t), dump(b, t))


def test_payment_fault_ab(world, baseline):
    hit = q(world, PSR.format("de", "android", FAULT)) / q(baseline, PSR.format("de", "android", FAULT))
    assert hit <= 1 - 0.18
    for country, platform in [("de", "ios"), ("fr", "android")]:
        r = q(world, PSR.format(country, platform, FAULT)) / q(baseline, PSR.format(country, platform, FAULT))
        assert r == pytest.approx(1.0, abs=0.02)
    total = f"SELECT SUM(orders) FROM fact_commerce WHERE {FAULT}"
    assert q(world, total) < q(baseline, total)


def test_rollback_restores(world, baseline):
    r = q(world, PSR.format("de", "android", AFTER)) / q(baseline, PSR.format("de", "android", AFTER))
    assert r == pytest.approx(1.0, abs=0.02)
    e = world.con.execute("SELECT ts, description FROM events WHERE id = 'android_payment_de_end'").fetchall()[0]
    assert e == (datetime(2026, 10, 26, 21), "rollback to 5.11")


def test_campaign_ab(world, baseline):
    paid = f"SELECT SUM(sessions) FROM fact_traffic WHERE channel = 'paid' AND {CAMPAIGN}"
    assert q(world, paid) / q(baseline, paid) == pytest.approx(1.4, abs=0.03)
    assert conversion(world, CAMPAIGN) < conversion(baseline, CAMPAIGN)
    orders = f"SELECT SUM(orders) FROM fact_commerce WHERE {CAMPAIGN}"
    assert q(world, orders) > q(baseline, orders)
    e = world.con.execute("SELECT type, planned, expected_effect::VARCHAR FROM events WHERE id = 'autumn_campaign'"
                          ).fetchall()[0]
    assert e[0] == "campaign" and e[1] is True and '"sessions"' in e[2]


@pytest.mark.parametrize("local", [False, True])
def test_no_midnight_cliff(world, local):
    df = world.con.execute("SELECT ts, SUM(sessions) s FROM fact_traffic GROUP BY 1 ORDER BY 1").df()
    ts = pd.to_datetime(df["ts"])
    hour = ts.dt.tz_localize("UTC").dt.tz_convert("Europe/Berlin").dt.hour if local else ts.dt.hour
    change = (df["s"] / df["s"].shift(1) - 1).abs()
    assert change[hour == 0].max() < 0.40


def test_slices_sum_to_totals(world):
    for table, dims, measures in [
        ("fact_traffic", ["country", "platform", "channel"], ["sessions", "searches", "acquisition_cost_eur"]),
        ("fact_commerce", ["country", "platform", "channel", "category"], ["orders", "checkouts", "gmv_eur"]),
        ("fact_ads", ["country", "advertiser"], ["clicks", "spend_eur"]),
    ]:
        sums = ", ".join(f"SUM({m})" for m in measures)
        total = world.con.execute(f"SELECT {sums} FROM {table}").fetchall()[0]
        for d in dims:
            parts = world.con.execute(f"SELECT {d}, {sums} FROM {table} GROUP BY 1").fetchall()
            for i in range(len(measures)):
                assert sum(p[i + 1] for p in parts) == pytest.approx(total[i], rel=1e-9)
    # ratios are SUM(num) / SUM(den) of the slices, never an average of slice ratios
    by_country = world.con.execute("SELECT SUM(orders), SUM(checkouts) FROM fact_commerce GROUP BY country").fetchall()
    tot = q(world, "SELECT SUM(orders) / SUM(checkouts) FROM fact_commerce")
    assert sum(o for o, _ in by_country) / sum(c for _, c in by_country) == pytest.approx(tot)
    # commerce rows of one traffic cell carry exactly that cell's search clicks split across categories
    bad = q(world, """SELECT COUNT(*) FROM (SELECT ts, country, platform, channel, COUNT(*) n
                      FROM fact_commerce GROUP BY ALL) WHERE n <> 5""")
    assert bad == 0
