"""Publish seeded history and planned dates. Simulator settings stay in the Lab."""

import hashlib
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from psycopg.types.json import Jsonb

from eye import simulator
from eye.db import connect, set_state, setup
from eye.intervals import CADENCE, DAY, HOUR

HOLIDAY = datetime(2026, 11, 22, tzinfo=timezone.utc)
PREVIOUS_HOLIDAY = datetime(2025, 11, 23, tzinfo=timezone.utc)
HOLIDAY_SCOPE = {"country": "DE"}
HOLIDAY_TRAFFIC = 1.75
SEGMENTS = Counter()
for (country, platform, _), weight in zip(simulator.SEGMENTS, simulator.SEGMENT_WEIGHTS):
    SEGMENTS[country, platform] += weight


def holiday_traffic(start):
    return (HOLIDAY_SCOPE, HOLIDAY_TRAFFIC) if HOLIDAY <= start < HOLIDAY + DAY else None


def aggregate_history(seed):
    rng = np.random.default_rng(seed)
    end = simulator.DEMO_START.replace(hour=0)
    weights = np.array(list(SEGMENTS.values()), dtype=float)
    for day in range(365, -1, -1):
        start = end - day * DAY
        # Apply a slow business trend to the aggregate simulator, independent of the promotion.
        trend = 1 - day / 2900
        allocation = weights.copy()
        if start == PREVIOUS_HOLIDAY:
            allocation *= [HOLIDAY_TRAFFIC if country == HOLIDAY_SCOPE["country"] else 1
                           for country, _ in SEGMENTS]
        mix = np.tile(allocation, (10, 1)).ravel() / (10 * sum(allocation))
        hours = 24 if day else simulator.DEMO_START.hour
        volume = [round(simulator.session_volume(start + hour * HOUR, seed, patterned=True) * trend
                        * sum(allocation) / sum(weights)) for hour in range(hours)]
        shoppers = np.array([rng.multinomial(n, mix).reshape(10, len(weights)) for n in volume])
        shoppers = np.pad(shoppers, ((0, 0), (0, 2), (0, 0)))
        views = rng.binomial(shoppers, simulator.VIEW_RATE)
        carts = rng.binomial(views, simulator.CART_RATE)
        checkouts = rng.binomial(carts, simulator.CHECKOUT_RATE)
        orders = rng.binomial(checkouts, 1 - simulator.TIMEOUT_RATE - simulator.DECLINE_RATE)
        days = np.stack([shoppers, views, carts, checkouts, orders], axis=-1)

        def row(minutes, at, segment, measurements):
            sessions, views, carts, checkout, sales = map(int, measurements)
            return (minutes, at, *segment, Jsonb({"sessions": sessions, "product_view_sessions": views,
                     "add_to_cart_sessions": carts, "checkout_sessions": checkout, "orders": sales}),
                    Jsonb({"session_conversion_rate": [sales, sessions], "checkout_completion_rate": [sales, checkout]}), True)

        if day:
            for index, segment in enumerate(SEGMENTS):
                yield row(1440, start, segment, days.sum(axis=(0, 1))[index])
        if day <= 56:
            for tick, readings in enumerate(days.reshape(-1, len(weights), 5)):
                at = start + tick * CADENCE
                if at < simulator.DEMO_START - 56 * DAY:
                    continue
                for index, segment in enumerate(SEGMENTS):
                    yield row(5, at, segment, readings[index])


def seed_lab(dsn, seed, cache=None):
    import pgserver

    pg_bin = Path(pgserver.__file__).with_name("pginstall") / "bin"
    if cache is None:
        cache = Path(__file__).resolve().parents[1] / ".runtime" / "seasonality"
    version = hashlib.sha256(b"".join(Path(__file__).with_name(name).read_bytes()
                             for name in ("lab_history.py", "simulator.py", "schema.sql"))).hexdigest()[:12]
    archive = cache / f"history-{seed}-{version}.dump"
    with connect(dsn) as db:
        setup(db)
    if archive.exists() and subprocess.run([pg_bin / "pg_restore", "--data-only", "--single-transaction",
                                           str(archive), "--dbname", dsn], capture_output=True).returncode == 0:
        return "restored"
    with connect(dsn) as db:
        simulator.seed(db, seed=seed, patterned=True)
        with db.cursor().copy("copy seasonal_buckets from stdin") as copy:
            for row in aggregate_history(seed):
                copy.write_row(row)
        db.cursor().executemany("insert into planned_events values (%s,%s,%s,%s,%s)",
                                [("november-shopping", "November holiday shopping", start, start + DAY, Jsonb(HOLIDAY_SCOPE))
                                 for start in (PREVIOUS_HOLIDAY, HOLIDAY)])
        set_state(db, "seasonal_history", True)
    cache.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=cache) as directory:
        temporary = Path(directory) / "baseline.dump"
        subprocess.run([pg_bin / "pg_dump", "--data-only", "--schema=eye", "--format=custom",
                        "--file", temporary, "--dbname", dsn], check=True, capture_output=True)
        temporary.replace(archive)
    return "generated"
