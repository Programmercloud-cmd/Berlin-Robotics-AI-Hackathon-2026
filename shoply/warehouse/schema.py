"""DuckDB table definitions (kickoff spec, section 3). Written only by Data."""

DDL = {
    "fact_traffic": """
CREATE TABLE fact_traffic (            -- grain: hour x country x platform x channel
  ts TIMESTAMP, country VARCHAR, platform VARCHAR, channel VARCHAR,
  sessions BIGINT, searches BIGINT,
  relevance_sum DOUBLE, relevance_n BIGINT,      -- search_relevance = sum / n
  acquisition_cost_eur DOUBLE
)""",
    "fact_commerce": """
CREATE TABLE fact_commerce (           -- grain: hour x country x platform x channel x category
  ts TIMESTAMP, country VARCHAR, platform VARCHAR, channel VARCHAR, category VARCHAR,
  search_views BIGINT, feed_views BIGINT, direct_views BIGINT,
  feed_impressions BIGINT, feed_oos_impressions BIGINT,
  carts BIGINT, checkouts BIGINT, orders BIGINT, feed_orders BIGINT,
  gmv_eur DOUBLE, discounts_eur DOUBLE, fees_eur DOUBLE, payment_costs_eur DOUBLE
)""",
    "fact_inventory": """
CREATE TABLE fact_inventory (          -- grain: hour x category x sku
  ts TIMESTAMP, category VARCHAR, sku VARCHAR,
  in_stock INTEGER, demand_weight DOUBLE,          -- in_stock_rate = sum(in_stock*w) / sum(w)
  delivered_units BIGINT, po_units BIGINT
)""",
    "fact_ads": """
CREATE TABLE fact_ads (                -- grain: hour x country x advertiser
  ts TIMESTAMP, country VARCHAR, advertiser VARCHAR,
  impressions BIGINT, clicks BIGINT, spend_eur DOUBLE, planned_spend_eur DOUBLE
)""",
    "events": """
CREATE TABLE events (
  id VARCHAR, ts TIMESTAMP, type VARCHAR,          -- release | config | campaign | holiday | data_incident
  service VARCHAR, scope VARCHAR,                  -- scope as a segment string
  description VARCHAR, planned BOOLEAN, expected_effect JSON
)""",
    "ingestion_log": """
CREATE TABLE ingestion_log (
  table_name VARCHAR, ts TIMESTAMP,
  rows BIGINT, expected_rows BIGINT, arrived_at TIMESTAMP
)""",
}

COLUMNS = {
    "fact_traffic": ["ts", "country", "platform", "channel", "sessions", "searches",
                     "relevance_sum", "relevance_n", "acquisition_cost_eur"],
    "fact_commerce": ["ts", "country", "platform", "channel", "category",
                      "search_views", "feed_views", "direct_views",
                      "feed_impressions", "feed_oos_impressions",
                      "carts", "checkouts", "orders", "feed_orders",
                      "gmv_eur", "discounts_eur", "fees_eur", "payment_costs_eur"],
    "fact_inventory": ["ts", "category", "sku", "in_stock", "demand_weight",
                       "delivered_units", "po_units"],
    "fact_ads": ["ts", "country", "advertiser", "impressions", "clicks", "spend_eur",
                 "planned_spend_eur"],
    "events": ["id", "ts", "type", "service", "scope", "description", "planned",
               "expected_effect"],
    "ingestion_log": ["table_name", "ts", "rows", "expected_rows", "arrived_at"],
}

FACT_TABLES = ["fact_traffic", "fact_commerce", "fact_inventory", "fact_ads"]


def create_schema(con) -> None:
    existing = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    for name, ddl in DDL.items():
        if name not in existing:
            con.execute(ddl).fetchall()
