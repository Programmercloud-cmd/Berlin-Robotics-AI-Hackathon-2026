from datetime import datetime

import pandas as pd
import pytest

from shoply.sim.scenarios import Scenario
from shoply.sim.simulator import HourRNG, Simulator, hour_index
from shoply.warehouse.schema import COLUMNS, FACT_TABLES

START, END = datetime(2026, 10, 25), datetime(2026, 10, 27)


def dump(sim, table):
    cols = COLUMNS[table]
    keys = [c for c in cols if c in ("ts", "country", "platform", "channel", "category", "sku", "advertiser",
                                     "id", "table_name")]
    return sim.con.execute(f"SELECT * FROM {table} ORDER BY {', '.join(keys)}").df()


def run(seed=1, **kw):
    sim = Simulator(":memory:", seed=seed, start=START, **kw)
    sim.run_until(END)
    return sim


@pytest.mark.parametrize("table", list(COLUMNS))
def test_same_seed_same_data(table):
    pd.testing.assert_frame_equal(dump(run(), table), dump(run(), table))


def test_different_seed_differs():
    assert not dump(run(1), "fact_commerce").equals(dump(run(2), "fact_commerce"))


def test_live_steps_equal_batch():
    batch = run()
    live = Simulator(":memory:", seed=1, start=START)
    while live.now < END:
        live.step()
    for t in COLUMNS:
        pd.testing.assert_frame_equal(dump(batch, t), dump(live, t))


def test_file_backed_db(tmp_path):
    path = tmp_path / "shoply.duckdb"
    sim = Simulator(path, seed=1, start=START)
    sim.run_until(datetime(2026, 10, 25, 3))
    sim.con.close()
    import duckdb
    con = duckdb.connect(str(path), read_only=True)
    assert con.execute("SELECT COUNT(*) FROM fact_commerce").fetchall()[0][0] == 3 * 225


def test_unknown_exclude_rejected():
    with pytest.raises(ValueError):
        Simulator(":memory:", exclude=["nope"])


def test_fact_tables_listed():
    assert set(FACT_TABLES) <= set(COLUMNS)


def test_populated_warehouse_rejected(tmp_path):
    path = tmp_path / "w.duckdb"
    sim = Simulator(path, seed=1, scenarios=None, start=datetime(2026, 10, 1))
    sim.run_until(datetime(2026, 10, 1, 2))
    sim.con.close()
    with pytest.raises(ValueError):
        Simulator(path, seed=1, scenarios=None)


def test_run_until_accepts_aware_timestamp():
    from datetime import timezone
    sim = Simulator(":memory:", seed=1, scenarios=None, start=datetime(2026, 10, 1))
    sim.run_until(datetime(2026, 10, 1, 3, tzinfo=timezone.utc))
    assert sim.now == datetime(2026, 10, 1, 3)


def test_state_save_and_load_continues_identically(tmp_path):
    mid, end = datetime(2026, 10, 26, 13, 30).replace(minute=0), datetime(2026, 10, 26, 18)
    full = Simulator(":memory:", seed=1, start=START)
    full.run_until(end)
    db, state = tmp_path / "w.duckdb", tmp_path / "sim.pkl"
    first = Simulator(db, seed=1, start=START)
    first.run_until(mid)                       # stop right after the late partition hour
    first.save_state(state)
    first.con.close()
    resumed = Simulator.load_state(state, db)
    assert resumed.now == mid
    resumed.run_until(end)
    for t in COLUMNS:
        pd.testing.assert_frame_equal(dump(full, t), dump(resumed, t))


def test_load_state_rejects_other_warehouse(tmp_path):
    sim = Simulator(":memory:", seed=1, start=START)
    sim.run_until(datetime(2026, 10, 25, 2))
    sim.save_state(tmp_path / "s.pkl")
    with pytest.raises(ValueError):
        Simulator.load_state(tmp_path / "s.pkl", ":memory:")


def test_load_state_rejects_same_hour_warehouse_of_other_run(tmp_path):
    end = datetime(2026, 10, 25, 6)
    sims = []
    for seed in (1, 2):
        sim = Simulator(tmp_path / f"{seed}.duckdb", seed=seed, start=START)
        sim.run_until(end)
        sim.save_state(tmp_path / f"{seed}.pkl")
        sim.con.close()
        sims.append(sim)
    with pytest.raises(ValueError, match="not written by this run"):
        Simulator.load_state(tmp_path / "1.pkl", tmp_path / "2.duckdb")
    assert Simulator.load_state(tmp_path / "1.pkl", tmp_path / "1.duckdb").now == end


def test_hour_rng_is_seeded_by_seed_and_hour():
    from shoply.sim.simulator import HourRNG, hour_index
    assert hour_index(datetime(2026, 8, 31)) == 0 and hour_index(datetime(2026, 9, 1, 1)) == 25
    a, b = HourRNG(42, 7).uniform("x", 5), HourRNG(42, 7).uniform("x", 5)
    assert (a == b).all()
    assert not (a == HourRNG(42, 8).uniform("x", 5)).all()
    assert not (a == HourRNG(42, 7).uniform("y", 5)).all()


def test_common_random_numbers():
    """Changing one row's parameter leaves the draws of the other rows unchanged."""
    from shoply.sim.simulator import HourRNG
    r = HourRNG(42, 3)
    base = r.binomial("orders", [1000, 1000, 1000], [0.94, 0.94, 0.94])
    hit = r.binomial("orders", [1000, 1000, 1000], [0.94, 0.71, 0.94])
    assert base[0] == hit[0] and base[2] == hit[2] and hit[1] < base[1]


def test_degenerate_parameters_draw_zero():
    from shoply.sim.simulator import HourRNG
    r = HourRNG(42, 3)
    g = r.gamma("gmv", [5.0, 0.0, 5.0], [0.0, 10.0, 10.0])
    assert g[0] == 0 and g[1] == 0 and g[2] > 0
    assert (r.poisson("x", [0.0, -1.0]) == 0).all() and (r.binomial("y", [10, 0], [0.0, 0.5]) == 0).all()


def test_zero_aov_scenario_keeps_money_finite():
    s = Scenario(id="free", start=datetime(2026, 10, 25, 2), scope={"country": "de"}, effects={"avg_order_value": 0})
    sim = Simulator(":memory:", seed=42, start=START, scenarios=[s])
    sim.run_until(datetime(2026, 10, 25, 6))
    bad = sim.con.execute("SELECT COUNT(*) FROM fact_commerce WHERE isnan(gmv_eur) OR isnan(fees_eur) "
                          "OR isnan(discounts_eur) OR isnan(payment_costs_eur)").fetchall()[0][0]
    assert bad == 0
    de = sim.con.execute("SELECT SUM(orders), SUM(gmv_eur) FROM fact_commerce WHERE country = 'de' "
                         "AND ts >= '2026-10-25 02:00'").fetchall()[0]
    assert de[0] > 0 and de[1] == 0


def test_start_before_history_rejected():
    with pytest.raises(ValueError):
        Simulator(":memory:", start=datetime(2025, 10, 26, 23))


def test_hours_before_the_default_start_have_their_own_seeds():
    """The year before 2026-08-31 has negative hour indices; each still gets distinct, repeatable draws."""
    assert hour_index(datetime(2025, 10, 27)) < 0
    a, b, c = (HourRNG(42, hour_index(t)).uniform("x", 4)
               for t in (datetime(2025, 10, 27), datetime(2025, 10, 27, 1), datetime(2025, 10, 27)))
    assert (a == c).all() and not (a == b).any()
    assert not (HourRNG(42, -5).uniform("x", 4) == HourRNG(42, 5).uniform("x", 4)).any()
