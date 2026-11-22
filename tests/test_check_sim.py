import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

from shoply.sim.simulator import Simulator
from shoply.warehouse.semantic import Semantic

spec = importlib.util.spec_from_file_location("check_sim", Path(__file__).parents[1] / "scripts" / "check_sim.py")
check_sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_sim)

A, B = datetime(2026, 10, 26), datetime(2026, 10, 26, 6)


@pytest.fixture(scope="module")
def sim():
    s = Simulator(":memory:", seed=42, start=A, scenarios=None)
    s.run_until(B)
    return s


def test_totals_are_ratios_of_sums(sim):
    orders, sessions, carts = sim.con.execute(
        "SELECT (SELECT SUM(orders) FROM fact_commerce), (SELECT SUM(sessions) FROM fact_traffic), "
        "(SELECT SUM(carts) FROM fact_commerce)").fetchall()[0]
    sem = Semantic(sim.con)
    assert check_sim.total(sem, "conversion_rate", (A, B)) == pytest.approx(orders / sessions)
    assert check_sim.total(sem, "orders", (A, B)) == orders
    assert check_sim.cart_abandonment(sim, (A, B)) == pytest.approx(1 - orders / carts)


def test_row_formats_delta():
    assert check_sim.row("x", 0.7, 0.93, ".2%").split()[-1] == "-24.7%"
    assert check_sim.row("x", 1, 0, ",.0f").split()[-1] == "n/a"
