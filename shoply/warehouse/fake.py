"""Small deterministic fake warehouse for tests: a few dimension values, 72 hours across the DST end.

Every fact table, the ingestion log and the events table are filled from seeded pandas DataFrames, so the
graph and semantic layers can be tested without the simulator.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from itertools import product

import duckdb
import numpy as np
import pandas as pd

from shoply.warehouse.schema import COLUMNS, create_schema

START = datetime(2026, 10, 24)              # Berlin DST ends 2026-10-25 01:00 UTC
HOURS = 72
COUNTRIES, PLATFORMS, CHANNELS = ["de", "fr"], ["android", "ios"], ["organic", "paid"]
CATEGORIES, ADVERTISERS = ["electronics", "fashion"], ["adv_001", "adv_002"]
LATE = ("fact_commerce", datetime(2026, 10, 26, 13), 0.5)   # table, hour, share of rows arrived


def _grid(**dims: list) -> pd.DataFrame:
    hours = [START + timedelta(hours=h) for h in range(HOURS)]
    return pd.DataFrame(list(product(hours, *dims.values())), columns=["ts", *dims])


def fake_frames(seed: int = 42) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)

    def ints(n: int, lo: int, hi: int) -> np.ndarray:
        return rng.integers(lo, hi, n)

    t = _grid(country=COUNTRIES, platform=PLATFORMS, channel=CHANNELS)
    n = len(t)
    t["sessions"], t["searches"] = ints(n, 500, 1000), ints(n, 200, 400)
    t["relevance_n"] = t["searches"]
    t["relevance_sum"] = t["relevance_n"] * rng.uniform(0.6, 0.9, n)
    t["acquisition_cost_eur"] = rng.uniform(10, 50, n) * (t["channel"] == "paid")

    c = _grid(country=COUNTRIES, platform=PLATFORMS, channel=CHANNELS, category=CATEGORIES)
    n = len(c)
    c["search_views"], c["feed_impressions"] = ints(n, 50, 100), ints(n, 200, 400)
    c["feed_views"], c["direct_views"] = ints(n, 20, 60), ints(n, 10, 40)
    c["feed_oos_impressions"] = ints(n, 0, 20)
    c["carts"], c["checkouts"], c["orders"] = ints(n, 10, 30), ints(n, 5, 10), ints(n, 2, 5)
    c["feed_orders"] = ints(n, 0, 2)
    c["gmv_eur"] = c["orders"] * rng.uniform(20, 80, n)
    c["discounts_eur"] = c["gmv_eur"] * rng.uniform(0, 0.05, n)
    c["fees_eur"] = c["gmv_eur"] * rng.uniform(0.08, 0.12, n)
    c["payment_costs_eur"] = c["gmv_eur"] * 0.015

    i = _grid(category=CATEGORIES, sku=["sku_1", "sku_2"])
    n = len(i)
    i["sku"] = i["category"] + "_" + i["sku"]
    i["in_stock"], i["demand_weight"] = ints(n, 0, 2), rng.uniform(0.5, 2, n)
    i["delivered_units"], i["po_units"] = ints(n, 0, 10), ints(n, 0, 10)

    a = _grid(country=COUNTRIES, advertiser=ADVERTISERS)
    n = len(a)
    a["impressions"], a["clicks"] = ints(n, 100, 300), ints(n, 5, 20)
    a["spend_eur"] = a["clicks"] * rng.uniform(0.2, 0.6, n)
    a["planned_spend_eur"] = rng.uniform(3, 8, n)

    facts = {"fact_traffic": t, "fact_commerce": c, "fact_inventory": i, "fact_ads": a}
    log = pd.concat([f.groupby("ts").size().rename("expected_rows").reset_index().assign(table_name=name)
                     for name, f in facts.items()], ignore_index=True)
    log["rows"] = log["expected_rows"]
    late = (log["table_name"] == LATE[0]) & (log["ts"] == LATE[1])
    log.loc[late, "rows"] = (log.loc[late, "expected_rows"] * LATE[2]).round().astype(int)
    log["arrived_at"] = log["ts"] + timedelta(hours=1)

    events = pd.DataFrame([
        {"id": "android_payment_de", "ts": datetime(2026, 10, 26, 9), "type": "release",
         "service": "payments-android", "scope": "country=de|platform=android",
         "description": "payments SDK release", "planned": False, "expected_effect": None},
        {"id": "autumn_campaign", "ts": datetime(2026, 10, 26, 6), "type": "campaign", "service": None,
         "scope": "", "description": "autumn campaign", "planned": True,
         "expected_effect": json.dumps({"sessions": 1.4})},
    ])
    return {**{k: v[COLUMNS[k]] for k, v in facts.items()},
            "ingestion_log": log[COLUMNS["ingestion_log"]], "events": events[COLUMNS["events"]]}


def fake_warehouse(seed: int = 42) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with the warehouse schema filled from fake_frames(seed)."""
    con = duckdb.connect(":memory:")
    create_schema(con)
    for name, frame in fake_frames(seed).items():
        con.register("_frame", frame)
        con.execute(f"INSERT INTO {name} SELECT * FROM _frame").fetchall()
        con.unregister("_frame")
    return con

