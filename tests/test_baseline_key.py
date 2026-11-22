"""The fast scalar profile key must match the vectorised one, including across the DST change."""
import pandas as pd

from sentinel.brain.validate import local_profile_key
from sentinel.monitor.baseline import profile_key


def test_profile_key_matches_vectorised_key():
    ts = pd.date_range("2026-03-27", "2026-04-02", freq="h").append(pd.date_range("2026-10-22", "2026-10-29", freq="h"))
    assert [profile_key(t) for t in ts] == list(local_profile_key(pd.Series(ts)))
