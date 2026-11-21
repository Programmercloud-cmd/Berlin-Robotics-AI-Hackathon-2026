"""Executable definitions from docs/metrics.md on plan/jacob."""

JOURNEY = ["country", "platform", "acquisition_channel"]
SEARCH = ["country", "platform", "query_language"]
FEED = ["country", "platform", "category", "recommendation_model_version"]
PAYMENT = ["country", "platform", "payment_provider", "checkout_release_version"]
INVENTORY = ["sku_id", "category", "seller_id"]
ECONOMICS = ["country", "platform", "category", "acquisition_channel"]
ADS = ["campaign_id", "target_country", "advertising_channel"]
CONTRIBUTION = ["country", "acquisition_channel"]
DATA = ["source_id", "event_type"]
OUTCOMES = ("payment_succeeded", "payment_timed_out", "payment_declined")
STAGED = ("payment_timeout_rate", "unavailable_feed_share", "marketplace_contribution")
LEGACY = {"product_views": "product_view_sessions", "cart_additions": "add_to_cart_sessions"}
POPULATIONS = {
    "orders": "orders confirmed in the interval", "feed_events": "feed impressions or linked clicks by occurrence",
    "inventory_snapshots": "active SKUs at the requested snapshot", "order_lines": "confirmed order lines",
    "order_economics": "confirmed order lines", "contribution": "calendar-period orders and acquisition ad costs",
    "ad_events": "acquisition campaign events", "campaign_pacing": "campaigns for one Berlin business day",
    "ingestion_receipts": "events received by the observation snapshot", "source_manifests": "expected event receipts",
}


def define(identifier, name, description, unit, dimensions, source, method, activity=None,
           *, direction="none", alert_scope=(), maturity=0, **options):
    calculation = {"op": "catalog", "metric": identifier, "method": method, **options}
    if activity:
        calculation["activity"] = activity
    return {
        "id": identifier, "name": name, "definition": description, "calculation": calculation,
        "dimensions": dimensions, "direction": direction,
        "access": {
            "source": source, "unit": unit, "definition_version": 1,
            "population": options.get("population", POPULATIONS.get(source, method)), "maturity_seconds": maturity,
            "alert_dimensions": list(alert_scope),
        },
    }


JOURNEY_ALERT = {"direction": "down", "alert_scope": JOURNEY[:2]}
PAYMENT_ALERT = {"direction": "down", "alert_scope": PAYMENT[:3]}
COHORT = {"population": "session start cohort", "maturity": 900, **JOURNEY_ALERT}


def cohort(identifier, name, description, method, activity, unit="count", **options):
    return define(identifier, name, description, unit, JOURNEY, "sessions" if identifier == "sessions"
                  else "session_funnel", method, activity, **COHORT, **options)


def search(identifier, name, description, method, activity, unit="count", **options):
    return define(identifier, name, description, unit, SEARCH, "search_requests", method, activity,
                  population="search request cohort", **options)


def payment(identifier, name, description, method, activity=None, unit="count", **options):
    return define(identifier, name, description, unit, PAYMENT, "payments", method, activity,
                  activities=list(OUTCOMES), population="finalized payment attempts", **options)


def money(identifier, name, description, field):
    return define(identifier, name, description, "EUR cents", ECONOMICS, "order_economics", "sum",
                  "order_confirmed", value=field, extended=True, distinct="order_line_id")


CATALOG = {item["id"]: item for item in [
    cohort("sessions", "Sessions", "Distinct sessions by session start.", "cohort", "session_started"),
    cohort("product_view_sessions", "Sessions with product views", "Sessions with at least one product view.",
           "cohort", "product_viewed"),
    cohort("add_to_cart_sessions", "Sessions with cart additions", "Sessions with at least one cart addition.",
           "cohort", "cart_added"),
    cohort("checkout_sessions", "Checkout sessions", "Sessions that started checkout.", "cohort", "checkout_started"),
    define("orders", "Orders", "Distinct confirmed orders by confirmation time.", "count", JOURNEY, "orders",
           "count", "order_confirmed", distinct="order_id", **JOURNEY_ALERT),
    cohort("session_conversion_rate", "Session conversion", "Converted sessions per mature session cohort.",
           "cohort_ratio", "order_confirmed", "ratio", denominator_activity="session_started"),
    cohort("checkout_completion_rate", "Checkout completion", "Converted checkout sessions per mature checkout cohort.",
           "cohort_ratio", "order_confirmed", "ratio", denominator_activity="checkout_started"),
    search("searches", "Searches", "Search requests by request time.", "count", "search_requested"),
    search("searches_with_click", "Searches with a click", "Requests with a result click within two minutes.",
           "search_click", "search_result_clicked", maturity=120),
    search("zero_result_searches", "Zero-result searches", "Requests returning zero results.",
           "count", "search_requested", where={"result_count": 0}),
    search("search_click_rate", "Search click rate", "Search requests with a result click within two minutes per request.",
           "search_ratio", "search_result_clicked", "ratio", maturity=120, **JOURNEY_ALERT,
           components=["searches_with_click", "searches"]),
    define("feed_impressions", "Feed impressions", "Product-card impressions in the personalized feed.", "count",
           FEED, "feed_events", "count", "feed_impression", extended=True),
    define("feed_clicks", "Feed clicks", "Valid impression-linked clicks by click time.", "count",
           FEED, "feed_events", "feed_click", "feed_clicked", extended=True),
    define("unavailable_feed_impressions", "Unavailable feed impressions", "Impressions at zero sellable stock.",
           "count", FEED, "feed_events", "count", "feed_impression", extended=True,
           where={"available_units": 0}),
    define("unavailable_feed_share", "Unavailable feed share", "Zero-stock impressions per feed impression.",
           "ratio", FEED, "feed_events", "ratio", "feed_impression", extended=True,
           where={"available_units": 0}, components=["unavailable_feed_impressions", "feed_impressions"]),
    payment("payment_attempts", "Payment attempts", "Finalized attempts, including retries.",
            "payments", "payment_attempted", **PAYMENT_ALERT),
    payment("successful_payments", "Successful payments", "Finalized attempts with outcome success.",
            "payments", "payment_succeeded", outcome="payment_succeeded", **PAYMENT_ALERT),
    payment("payment_timeouts", "Payment timeouts", "Finalized attempts with outcome timeout.",
            "payments", "payment_timed_out", outcome="payment_timed_out"),
    payment("other_failed_payments", "Other failed payments", "Finalized attempts with outcome other failure.",
            "payments", "payment_declined", outcome="payment_declined"),
    payment("payment_success_rate", "Payment success rate", "Successful payments per finalized attempt.",
            "payment_ratio", "payment_succeeded", "ratio", outcome="payment_succeeded",
            components=["successful_payments", "payment_attempts"], **PAYMENT_ALERT),
    payment("payment_timeout_rate", "Payment timeout rate", "Timed-out payments per finalized attempt.",
            "payment_ratio", "payment_timed_out", "ratio", outcome="payment_timed_out", direction="up",
            alert_scope=PAYMENT[:3], components=["payment_timeouts", "payment_attempts"]),
    define("available_units", "Available units", "Sellable stock at the latest snapshot per SKU.", "units",
           INVENTORY, "inventory_snapshots", "inventory_sum", "inventory_snapshot",
           value="available_units", direction="down", alert_scope=["category"]),
    define("units_sold", "Units sold", "Quantities on confirmed order lines.", "units",
           INVENTORY, "order_lines", "sum", "order_confirmed", value="quantity", extended=True,
           distinct="order_line_id"),
    define("stockout_sku_share", "Stockout SKU share", "Active SKUs with zero sellable stock per active SKU.",
           "ratio", INVENTORY, "inventory_snapshots", "inventory_ratio", "inventory_snapshot",
           direction="up", alert_scope=["category"]),
    money("gross_merchandise_value", "Gross merchandise value", "List-price value before platform discounts.", "list_value_cents"),
    money("platform_fee_revenue", "Platform fee revenue", "Marketplace commission on pre-discount value.", "fee_cents"),
    money("platform_funded_discounts", "Platform-funded discounts", "Platform-funded discounts on confirmed order lines.", "discount_cents"),
    define("advertising_spend", "Advertising spend", "Acquisition ad click costs by click time.", "EUR cents",
           ADS, "ad_events", "sum", "ad_clicked", value="cost_cents", extended=True),
    money("variable_operating_cost", "Variable operating cost", "Processing and servicing cost on confirmed order lines.", "operating_cost_cents"),
    define("marketplace_contribution", "Marketplace contribution", "Calendar-period fees less discounts, click spend and operating cost.",
           "EUR cents", CONTRIBUTION, "contribution", "contribution", "order_confirmed", extended=True, activities=["ad_clicked"],
           components=["platform_fee_revenue", "platform_funded_discounts", "advertising_spend", "variable_operating_cost"]),
    define("ad_impressions", "Ad impressions", "Acquisition campaign impressions.", "count", ADS, "ad_events",
           "count", "ad_impression", extended=True),
    define("ad_clicks", "Ad clicks", "Acquisition campaign clicks.", "count", ADS, "ad_events",
           "count", "ad_clicked", extended=True),
    define("effective_cost_per_click", "Effective cost per click", "Advertising spend per acquisition click.",
           "EUR cents per click", ADS, "ad_events", "ratio", "ad_clicked", value="cost_cents",
           extended=True, components=["advertising_spend", "ad_clicks"]),
    define("daily_budget_utilization", "Daily budget utilization", "Berlin business-day click spend per configured campaign budget.",
           "ratio", ADS, "campaign_pacing", "budget", "campaign_budget", activities=["ad_clicked"], extended=True),
    define("event_ingestion_lag_seconds", "Event ingestion lag", "95th-percentile delay from event occurrence to ingestion.",
           "seconds", DATA, "ingestion_receipts", "lag", direction="up"),
    define("data_completeness_ratio", "Data completeness", "Received unique events per source-manifest count.",
           "ratio", DATA, "source_manifests", "completeness", direction="down"),
]}

CATALOG["marketplace_contribution"]["access"]["dimension_mapping"] = {
    "target_country": "country", "advertising_channel": "acquisition_channel",
}
INITIAL_METRICS = [entry for identifier, entry in CATALOG.items() if identifier not in STAGED]
TIMEOUT_RATE_METRIC = CATALOG["payment_timeout_rate"]
