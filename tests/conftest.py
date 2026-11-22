from datetime import datetime

import pytest

from shoply.sim.simulator import Simulator

SEED = 42
END = datetime(2026, 10, 29)          # history + three live days


def q(sim, sql, *params):
    """Scalar query helper."""
    return sim.con.execute(sql, list(params)).fetchall()[0][0]


@pytest.fixture(scope="session")
def world():
    """Full history plus the live demo days, every scenario enabled."""
    sim = Simulator(":memory:", seed=SEED)
    sim.run_until(END)
    return sim


@pytest.fixture(scope="session")
def baseline():
    """Same seed, no scenarios: the counterfactual for every effect test."""
    sim = Simulator(":memory:", seed=SEED, scenarios=None)
    sim.run_until(END)
    return sim
