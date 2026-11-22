from datetime import datetime

import pytest
import yaml

from shoply.graph import DATA_DIR
from shoply.graph.check import check_formula
from shoply.warehouse.fake import CHANNELS, COUNTRIES, PLATFORMS, fake_warehouse
from shoply.warehouse.semantic import Semantic

A, B = datetime(2026, 10, 24), datetime(2026, 10, 27)
FORMULAS = yaml.safe_load((DATA_DIR / "formulas.yaml").read_text())


@pytest.fixture(scope="module")
def sem():
    return Semantic(fake_warehouse())


@pytest.mark.parametrize("f", FORMULAS, ids=[f["output"] for f in FORMULAS])
def test_supplied_formulas_hold(sem, f):
    r = check_formula(f["output"], f["expr"], A, B, sem=sem)
    assert r.holds and r.failing_slices == [], r.failing_slices
    assert r.median_rel_error < 0.005 and r.share_within >= 0.95
    assert r.dimensions == sem.graph.shared_dims([f["output"], *sem.graph.formula(f["output"])["inputs"]])
    segments = {s.segment for s in r.slices}
    data = sem.con.execute("SELECT DISTINCT country, platform, channel FROM fact_traffic").df()
    for d in r.dimensions:
        if d in data:
            assert {f"{d}={v}" for v in data[d]} <= segments
    assert "" in segments


def test_slices_cover_every_shared_dimension(sem):
    r = check_formula("orders", "sessions * conversion_rate", A, B, sem=sem)
    assert r.dimensions == ["country", "platform", "channel"]
    assert len(r.slices) == 1 + len(COUNTRIES) + len(PLATFORMS) + len(CHANNELS)
    assert all(s.rows == 72 for s in r.slices)


def test_wrong_formula_is_rejected(sem):
    r = check_formula("orders", "sessions * checkout_rate", A, B, sem=sem)
    assert not r.holds and r.share_within < 0.95
    assert {s.segment for s in r.failing_slices} >= {"", "country=de", "platform=android", "channel=paid"}


def test_filters_and_bad_input(sem):
    r = check_formula("orders", "sessions * conversion_rate", A, B, sem=sem, filters={"country": "de"})
    assert r.holds and r.dimensions == ["platform", "channel"]
    with pytest.raises(ValueError):
        check_formula("orders", "sessions * 2", A, B, sem=sem)
    with pytest.raises(KeyError):
        check_formula("orders", "sessions * nope", A, B, sem=sem)


def test_multi_value_filter_is_still_sliced(sem):
    r = check_formula("orders", "sessions * checkout_rate", A, B, sem=sem, filters={"country": iter(COUNTRIES)})
    assert r.dimensions == ["country", "platform", "channel"]
    assert {f"country={c}" for c in COUNTRIES} <= {s.segment for s in r.failing_slices}
    assert check_formula("orders", "sessions * conversion_rate", A, B, sem=sem, filters={"country": ["de"]}).dimensions \
        == ["platform", "channel"]
