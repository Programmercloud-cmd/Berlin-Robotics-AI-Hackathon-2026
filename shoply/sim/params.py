"""Baseline parameters of the Shoply simulator.

Values marked "spec" come from the simulator spec. Everything else is an assumption
chosen to be plausible; change it here, nowhere else.
"""
from datetime import datetime

# ---- time (spec) ----
HISTORY_START = datetime(2026, 8, 31)          # UTC, naive; hour_index 0
LIVE_START = datetime(2026, 10, 26)            # Monday 00:00 UTC; history = 8 weeks before it
TIMEZONE = "Europe/Berlin"                     # local time for every country (DST ends 2026-10-25)

# ---- dimensions and session shares (spec) ----
COUNTRIES = {"de": 0.40, "fr": 0.25, "es": 0.15, "nl": 0.10, "pl": 0.10}
PLATFORMS = {"android": 0.45, "ios": 0.35, "web": 0.20}
CHANNELS = {"organic": 0.60, "paid": 0.25, "for_you": 0.15}
CATEGORIES = ["electronics", "fashion", "home", "beauty", "toys"]
ADVERTISERS = [f"adv_{i:03d}" for i in range(1, 31)]
TIMEZONES = {c: TIMEZONE for c in COUNTRIES}
MOBILE = {"android", "ios"}

# ---- traffic (spec) ----
DAILY_SESSIONS = 600_000                       # lambda = DAILY_SESSIONS / 24 * hour_curve * ...
SESSION_GAMMA_SHAPE = 20.0                     # sessions ~ Poisson(lambda * Gamma(k, mean 1))
PEAK_LOCAL_HOUR = 20
LUNCH_LOCAL_HOUR = 13
WEEKEND_MULT = {"mobile": 1.15, "web": 0.90}
TREND_PER_DAY = 0.001                          # lambda * (1 + 0.001 * day)
HOLIDAYS = [
    {"id": "holiday_de_2026-10-03", "date": "2026-10-03", "country": "de",
     "description": "Tag der Deutschen Einheit", "sessions": 0.8},
]

SEARCHES_PER_SESSION = {"organic": 0.95, "paid": 0.95, "for_you": 0.40}       # spec
SEARCH_RELEVANCE = 0.80                        # spec: mean relevance score per search
SEARCH_RELEVANCE_SD = 0.15                     # per-search score sd (assumption)
SEARCH_CTR = 0.45                              # spec: clicks per search = 0.45 * relevance / 0.8
DIRECT_VIEWS_PER_SESSION = {"organic": 1.2, "paid": 1.2, "for_you": 1.2}      # spec
FEED_IMPRESSIONS_PER_SESSION = {"organic": 2.0, "paid": 2.0, "for_you": 8.0}  # spec
FEED_CTR = 0.06                                # spec
ACQUISITION_COST_PER_PAID_SESSION = 0.35       # spec, EUR

# ---- funnel ----
CATEGORY_VIEW_SHARE = {"electronics": 0.25, "fashion": 0.30, "home": 0.20, "beauty": 0.15, "toys": 0.10}  # spec
# Per product view; view-weighted mean 0.051 so conversion ~ 1.8 views * 0.051 * 0.35 * 0.94 ~ 3.0%.
CATEGORY_ADD_TO_CART = {"electronics": 0.0385, "fashion": 0.0578, "home": 0.0496, "beauty": 0.0619, "toys": 0.0482}
CHECKOUT_RATE = 0.35                           # carts -> checkouts; cart abandonment 1 - orders/carts ~ 67%
PAYMENT_SUCCESS_RATE = 0.94                    # spec
BASE_IN_STOCK_RATE = 0.98                      # spec: carts ~ Bin(views, p_cat * in_stock_rate / 0.98)
FEED_OOS_BASE_SHARE = 0.03                     # spec: oos share = 0.03 + 0.5 * (1 - in_stock_rate)
FEED_OOS_SENSITIVITY = 0.5

# Heterogeneous base rates (assumption) so that mix effects exist. Normalised to a
# session-weighted mean of 1 in simulator.py, so the overall rates stay on spec.
COUNTRY_ADD_TO_CART = {"de": 1.15, "fr": 1.00, "es": 0.95, "nl": 1.05, "pl": 0.75}
PLATFORM_ADD_TO_CART = {"android": 0.90, "ios": 1.05, "web": 1.20}
COUNTRY_AOV = {"de": 1.10, "fr": 1.00, "es": 0.90, "nl": 1.10, "pl": 0.75}
PLATFORM_AOV = {"android": 0.92, "ios": 1.05, "web": 1.15}

# ---- money (spec) ----
CATEGORY_AOV = {"electronics": 70.0, "fashion": 40.0, "home": 55.0, "beauty": 25.0, "toys": 30.0}
CATEGORY_TAKE_RATE = {"electronics": 0.08, "fashion": 0.15, "home": 0.12, "beauty": 0.14, "toys": 0.12}
ORDER_VALUE_CV = 0.6                           # single order value ~ Gamma(1/cv^2) around AOV (assumption)
DISCOUNT_RATE = 0.03
PAYMENT_COST_RATE = 0.014
PAYMENT_COST_PER_CHECKOUT = 0.10

# ---- inventory (spec) ----
SKUS_PER_CATEGORY = 40
SKU_ZIPF_EXPONENT = 1.0                        # forecast popularity within a category
DEMAND_FORECAST_ERROR_SD = 0.35                # true demand = forecast popularity * lognormal(0, 0.35)
DELIVERY_LAG_DAYS = 3                          # lead_days
DELIVERY_CYCLE_DAYS = 3                        # a purchase order every 3 days
PO_HOUR_UTC = 6                                # POs are placed and deliveries arrive at 06:00 UTC
ORDER_UP_TO_DAYS = 9
ORDER_UP_TO_SAFETY = 1.15                      # order up to 9 * 1.15 days of forecast
FORECAST_EWMA_ALPHA = 0.3                      # daily EWMA of observed sales (assumption)

# ---- ads (spec) ----
TOTAL_DAILY_AD_BUDGET = 25_000.0
AD_BUDGET_SD = 0.8                             # lognormal spread of advertiser budgets (assumption)
AD_CPC_MEAN = 0.35                             # assumption
AD_CTR = 0.015                                 # assumption

# ---- background releases (spec: harmless red herrings) ----
BACKGROUND_RELEASES_PER_DAY = 2.0
RELEASE_SERVICES = {                           # service -> scope of its release event
    "app-ios": {"platform": "ios"}, "app-android": {"platform": "android"},
    "web-frontend": {"platform": "web"}, "search": {}, "payments-android": {"platform": "android"},
    "payments-ios": {"platform": "ios"}, "payments-web": {"platform": "web"}, "recs-model": {},
    "checkout": {}, "ads-pacing": {},
}

# ---- ingestion ----
ARRIVAL_DELAY_MIN = (2, 9)                     # minutes after the hour closes

# Scenario effect semantics. Multiplicative effects scale a baseline parameter;
# absolute effects replace it (see README).
MULTIPLICATIVE_EFFECTS = {
    "sessions", "searches", "search_ctr", "search_relevance", "feed_impressions", "feed_ctr",
    "direct_views", "add_to_cart_rate", "checkout_rate", "payment_success_rate",
    "avg_order_value", "discount_rate", "take_rate", "acquisition_cpc", "completeness",
    "ad_cpc", "ad_spend",
}
ABSOLUTE_EFFECTS = {"oos_exposure_share", "delivery_lag_days"}

# ---- hour curve (assumption; circular, peak 20:00, lunch bump 13:00, mean 1) ----
HOUR_CURVE_FLOOR = 0.30
HOUR_CURVE_PEAK_WIDTH = 3.5
HOUR_CURVE_LUNCH_WEIGHT = 0.40
HOUR_CURVE_LUNCH_WIDTH = 2.5
