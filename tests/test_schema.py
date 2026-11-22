import pytest

from shoply.sim import params as P
from shoply.warehouse.schema import COLUMNS, DDL

SPEC_TYPES = {
    "fact_traffic": "TIMESTAMP VARCHAR VARCHAR VARCHAR BIGINT BIGINT DOUBLE BIGINT DOUBLE",
    "fact_commerce": "TIMESTAMP VARCHAR VARCHAR VARCHAR VARCHAR " + "BIGINT " * 9 + "DOUBLE DOUBLE DOUBLE DOUBLE",
    "fact_inventory": "TIMESTAMP VARCHAR VARCHAR INTEGER DOUBLE BIGINT BIGINT",
    "fact_ads": "TIMESTAMP VARCHAR VARCHAR BIGINT BIGINT DOUBLE DOUBLE",
    "events": "TIMESTAMP".join(["VARCHAR ", " VARCHAR VARCHAR VARCHAR VARCHAR BOOLEAN JSON"]),
    "ingestion_log": "VARCHAR TIMESTAMP BIGINT BIGINT TIMESTAMP",
}
GRAIN = {"fact_traffic": 45, "fact_commerce": 225, "fact_inventory": 200, "fact_ads": 150}


@pytest.mark.parametrize("table", list(DDL))
def test_columns_match_spec(world, table):
    rows = world.con.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ? "
        "ORDER BY ordinal_position", [table]).fetchall()
    assert [r[0] for r in rows] == COLUMNS[table]
    assert [r[1] for r in rows] == SPEC_TYPES[table].split()


def test_only_spec_tables(world):
    tables = {r[0] for r in world.con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert tables == set(DDL)


@pytest.mark.parametrize("table,n", GRAIN.items())
def test_full_grain_every_hour(world, table, n):
    hours = int((P.LIVE_START - P.HISTORY_START).total_seconds() // 3600) + 72
    counts = world.con.execute(f"SELECT COUNT(*) FROM {table} GROUP BY ts").fetchall()
    assert len(counts) == hours
    assert {c[0] for c in counts} == {n}


def test_dimension_values(world):
    vals = lambda t, c: {r[0] for r in world.con.execute(f"SELECT DISTINCT {c} FROM {t}").fetchall()}  # noqa: E731
    assert vals("fact_traffic", "country") == set(P.COUNTRIES)
    assert vals("fact_traffic", "platform") == set(P.PLATFORMS)
    assert vals("fact_traffic", "channel") == set(P.CHANNELS)
    assert vals("fact_commerce", "category") == set(P.CATEGORIES)
    assert vals("fact_ads", "advertiser") == set(P.ADVERTISERS)
    assert len(vals("fact_inventory", "sku")) == 200


def test_counts_are_consistent(world):
    bad = world.con.execute("""
        SELECT COUNT(*) FROM fact_commerce WHERE
          LEAST(search_views, feed_views, direct_views, feed_impressions, feed_oos_impressions,
                carts, checkouts, orders, feed_orders) < 0
          OR feed_oos_impressions > feed_impressions OR feed_views > feed_impressions
          OR carts > search_views + feed_views + direct_views
          OR checkouts > carts OR orders > checkouts OR feed_orders > orders
          OR (orders = 0 AND gmv_eur <> 0) OR gmv_eur < 0 OR discounts_eur < 0""").fetchall()[0][0]
    assert bad == 0
    assert world.con.execute("SELECT COUNT(*) FROM fact_traffic WHERE relevance_n <> searches "
                             "OR (channel <> 'paid' AND acquisition_cost_eur <> 0)").fetchall()[0][0] == 0
    assert world.con.execute("SELECT COUNT(*) FROM fact_ads WHERE clicks > impressions OR spend_eur < 0"
                             ).fetchall()[0][0] == 0


def test_money_rules(world):
    err = world.con.execute(f"""
        SELECT MAX(ABS(payment_costs_eur - ({P.PAYMENT_COST_RATE} * gmv_eur
                                             + {P.PAYMENT_COST_PER_CHECKOUT} * checkouts)))
        FROM fact_commerce""").fetchall()[0][0]
    assert err < 1e-6
    for cat, rate in P.CATEGORY_TAKE_RATE.items():
        e = world.con.execute("SELECT MAX(ABS(fees_eur - ? * gmv_eur)) FROM fact_commerce WHERE category = ?",
                              [rate, cat]).fetchall()[0][0]
        assert e < 1e-6


def test_ingestion_log(world):
    rows = world.con.execute("""
        SELECT table_name, COUNT(*), MIN(rows = expected_rows), MIN(arrived_at > ts + INTERVAL 1 HOUR)
        FROM ingestion_log GROUP BY 1 ORDER BY 1""").fetchall()
    assert [r[0] for r in rows] == sorted(GRAIN)
    assert all(r[2] and r[3] for r in rows)
    assert len({r[1] for r in rows}) == 1
