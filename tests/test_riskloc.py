"""RiskLoc-style localisation on synthetic leaves: single, compound, uniform and rising changes."""
import itertools

import numpy as np
import pandas as pd
import pytest

from sentinel.riskloc import riskloc

DIMS = ["country", "platform", "provider", "channel"]
PAY = {"country": "DE", "platform": "mobile", "provider": "Stripe"}


def leaves(seed):
    rng = np.random.default_rng(seed)
    rows = []
    for c, p, pr, ch in itertools.product(["DE", "FR", "ES", "NL"], ["mobile", "web", "ios"], ["Stripe", "Adyen"],
                                          ["organic", "paid", "direct"]):
        f = rng.uniform(5, 150)
        rows.append(dict(country=c, platform=p, provider=pr, channel=ch, v=rng.poisson(f), f=f))
    return pd.DataFrame(rows), rng


def mask(df, seg):
    m = np.ones(len(df), bool)
    for k, v in seg.items():
        m &= (df[k] == v).values
    return m


def segments(df):
    return [c["segment"] for c in riskloc(df, DIMS)["causes"]]


@pytest.mark.parametrize("seed", range(10))
def test_single_root_cause(seed):
    df, _ = leaves(seed)
    m = mask(df, PAY)
    df.loc[m, "v"] = (df.loc[m, "f"] * 0.4).astype(int)
    assert segments(df) == [PAY]


@pytest.mark.parametrize("seed", range(10))
def test_two_root_causes(seed):
    df, _ = leaves(seed)
    for seg, k in ((PAY, 0.4), ({"country": "FR", "channel": "paid"}, 0.5)):
        m = mask(df, seg)
        df.loc[m, "v"] = (df.loc[m, "f"] * k).astype(int)
    found = sorted(map(str, segments(df)))
    assert found == sorted(map(str, [PAY, {"country": "FR", "channel": "paid"}]))


@pytest.mark.parametrize("seed", range(10))
def test_uniform_change_is_not_localised(seed):
    df, rng = leaves(seed)
    df["v"] = rng.poisson(df.f * 0.9)            # every leaf down 10%: an aggregate-wide change
    assert segments(df) == []


@pytest.mark.parametrize("seed", range(10))
def test_rise_in_one_channel(seed):
    df, rng = leaves(seed)
    m = mask(df, {"channel": "paid"})
    df.loc[m, "v"] = rng.poisson(df.loc[m, "f"] * 1.5)
    res = riskloc(df, DIMS)
    assert res["direction"] == "rise" and [c["segment"] for c in res["causes"]] == [{"channel": "paid"}]
