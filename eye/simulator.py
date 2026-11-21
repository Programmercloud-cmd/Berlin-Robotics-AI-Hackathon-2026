"""Seeded marketplace operator. Only observed, qualified OCEL events leave this module."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from itertools import product
from math import cos, pi
from random import Random

from psycopg.types.json import Jsonb

DEMO_START = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
PAYMENT_SCOPE = {"country": "DE", "platform": "mobile", "provider": "Stripe"}
SEARCH_SCOPE = {"country": "FR", "platform": "web"}
SEGMENTS = list(product(("DE", "FR"), ("mobile", "web"), ("Stripe", "Adyen")))
CATEGORIES = ("home", "outdoors", "electronics")
PRODUCTS = {f"sku-{i:03}": {"category": CATEGORIES[i % 3], "price_cents": 1200 + 185 * i}
            for i in range(48)}
FEED_ONLY = {"sku-048": {"category": "home", "price_cents": 2490},
             "sku-049": {"category": "outdoors", "price_cents": 3190}}
ALL_PRODUCTS = {key: {**value, "sku_id": key, "seller_id": f"seller-{index % 6 + 1}"}
                for index, (key, value) in enumerate((PRODUCTS | FEED_ONLY).items())}
SOURCES = {
    "search_requested": "search", "search_result_clicked": "search",
    "feed_impression": "feed", "feed_clicked": "feed",
    "payment_attempted": "payments", "payment_succeeded": "payments",
    "payment_timed_out": "payments", "payment_declined": "payments",
    "order_confirmed": "orders", "inventory_snapshot": "inventory",
    "ad_impression": "ads", "ad_clicked": "ads", "campaign_budget": "ads",
}


def seed(db, days=4, seed=42, search=True, patterned=False):
    for hour in range(24 * days, 0, -1):
        emit(db, DEMO_START - hour * HOUR, seed=seed, search=search, patterned=patterned)


def traffic_cycle(start):
    """Return the multiplier and position in the repeating 28-day traffic pattern."""
    day = (start.date() - DEMO_START.date()).days % 28
    weekday = (0.98, 0.99, 1.0, 1.01, 1.02, 1.03, 0.99)[day % 7]
    week = (0.99, 1.01, 0.98, 1.02)[day // 7]
    return weekday * week, day


def emit(db, start, seed=42, traffic=1.0, fault=False, search=True, search_disruption=False,
         patterned=False, scenario=None):
    start = start.astimezone(timezone.utc)
    clock = int(start.timestamp())
    rng = Random(f"{seed}:{clock}")
    search_rng = Random(f"{seed}:{clock}:search")
    catalog_rng = Random(f"{seed}:{clock}:catalog")
    page_rng = Random(f"{seed}:{clock}:product-page")
    prefix = f"{clock}:"
    previous = db.execute(
        """select attrs from events where activity = 'inventory_snapshot' and window_start =
           (select max(start_at) from windows where start_at < %s)""",
        (start,),
    ).fetchall()
    stock = {row["attrs"]["sku"]: row["attrs"]["available_units"] for row in previous
             if row["attrs"]["sku"] in PRODUCTS}
    for sku in PRODUCTS:
        if stock.get(sku, 0) < 50:
            stock[sku] = rng.randint(65, 95)

    opening_stock = stock.copy()
    sales = defaultdict(list)
    feed = []
    objects, events, links = [], [], []

    def add_object(key, kind, attrs):
        objects.append((key, kind, Jsonb(attrs)))

    def add_event(activity, elapsed, attrs, references, source_rng=rng):
        identifier = f"{prefix}event-{len(events)}"
        occurred = start + elapsed
        ingested = occurred + timedelta(milliseconds=source_rng.randint(150, 4750))
        metadata = {"source_id": SOURCES.get(activity, "storefront"), "event_type": activity}
        events.append((identifier, activity, occurred, ingested, start, Jsonb({**attrs, **metadata})))
        links.extend((identifier, key, qualifier) for key, qualifier in references)
        return identifier

    seasonal = 0.9 + 0.15 * cos((start.hour - 14) * pi / 12)
    if patterned:
        cycle, _ = traffic_cycle(start)
        seasonal *= cycle * Random(f"{seed}:{clock}:traffic").uniform(0.98, 1.02)
    fault_scope = scenario.scope.model_dump(exclude_none=True) if scenario else {}
    volume = round(600 * seasonal * traffic)
    for index in range(volume):
        base = f"{prefix}{index}:"
        session, cart, order, payment = (f"{base}{kind}" for kind in ("session", "cart", "order", "payment"))
        country, platform, provider = rng.choices(SEGMENTS, weights=(4, 1, 1, 1, 1, 1, 1, 1))[0]
        channel = catalog_rng.choice(("direct", "organic", "paid_search", "display"))
        journey = {"country": country, "platform": platform, "acquisition_channel": channel,
                   "session_id": session}
        checkout = {**journey, "provider": provider, "payment_provider": provider,
                    "checkout_release_version": "checkout-v2" if platform == "mobile" else "checkout-v1",
                    "order_id": order, "payment_id": payment}
        elapsed = timedelta(seconds=rng.randrange(0, 50 * 60))
        sku = rng.choice(tuple(PRODUCTS))
        item = {**journey, "sku": sku, **ALL_PRODUCTS[sku]}
        session_link = [(session, "customer session")]
        product_link = [*session_link, (sku, "product")]

        add_object(session, "session", journey)
        add_event("session_started", elapsed, journey, session_link)
        if catalog_rng.random() < 0.30:
            feed_sku = catalog_rng.choice(tuple(ALL_PRODUCTS))
            feed.append((elapsed, feed_sku, journey, session_link, catalog_rng.random() < 0.35))
        if search and search_rng.random() < 0.76:
            request = f"{base}search"
            add_object(request, "search", journey)
            search_links = [*session_link, (request, "search request")]
            search_affected = search_disruption and all(journey[key] == value for key, value in SEARCH_SCOPE.items())
            missing = 0.95 if search_affected else 0.06
            results = 0 if search_rng.random() < missing else search_rng.randint(2, 40)
            search_attrs = {**journey, "search_id": request, "query_language": country.lower(),
                            "result_count": results}
            add_event("search_requested", elapsed + timedelta(seconds=1), search_attrs, search_links, search_rng)
            if results and search_rng.random() < 0.72:
                add_event("search_result_clicked", elapsed + timedelta(seconds=3),
                          {**item, **search_attrs}, [*search_links, (sku, "selected result")], search_rng)
        add_event("product_page_requested", elapsed + timedelta(seconds=2), item, product_link, page_rng)
        page_affected = (scenario and scenario.fault_stage == "product_page_load"
                         and all(journey.get(key) == value for key, value in fault_scope.items()))
        if rng.random() >= 0.97 or (page_affected and page_rng.random() < scenario.severity):
            add_event("product_page_load_failed", elapsed + timedelta(seconds=5),
                      {**item, "http_status": 503}, product_link, page_rng)
            continue
        add_event("product_viewed", elapsed + timedelta(seconds=5), item, product_link)
        if rng.random() < 0.22:
            viewed = rng.choice(tuple(PRODUCTS))
            add_event("product_viewed", elapsed + timedelta(seconds=13),
                      {**journey, "sku": viewed, **ALL_PRODUCTS[viewed]},
                      [*session_link, (viewed, "product")])
        if rng.random() >= 0.83:
            continue
        add_object(cart, "cart", {})
        cart_links = [*product_link, (cart, "cart")]
        add_event("cart_added", elapsed + timedelta(seconds=22), item, cart_links)
        if rng.random() >= 0.80 or not stock[sku]:
            continue
        add_object(order, "order", {"sku": sku, "amount_cents": item["price_cents"]})
        add_object(payment, "payment", {"provider": provider})
        order_links = [*cart_links, (order, "order")]
        payment_links = [*order_links, (payment, "payment attempt")]
        commerce = {**item, **checkout}
        add_event("checkout_started", elapsed + timedelta(seconds=37), commerce, order_links)
        add_event("payment_attempted", elapsed + timedelta(seconds=45), commerce, payment_links)
        payment_affected = fault and all(checkout[key] == value for key, value in PAYMENT_SCOPE.items())
        timeout_rate = 0.63 if payment_affected else 0.022
        if (scenario and scenario.fault_stage == "payment_timeout" and
                all(checkout[key] == value for key, value in fault_scope.items())):
            timeout_rate = scenario.severity
        outcome = rng.random()
        if outcome < timeout_rate:
            add_event("payment_timed_out", elapsed + timedelta(seconds=62),
                      {**commerce, "response": "gateway_timeout"}, payment_links)
        elif outcome < timeout_rate + 0.012:
            add_event("payment_declined", elapsed + timedelta(seconds=47),
                      {**commerce, "response": "card_declined"}, payment_links)
        else:
            add_event("payment_succeeded", elapsed + timedelta(seconds=47),
                      {**commerce, "response": "authorized"}, payment_links)
            value = item["price_cents"]
            line = {**commerce, "order_line_id": f"{order}:line-1", "quantity": 1, "list_value_cents": value,
                    "fee_cents": value * 12 // 100, "discount_cents": 150 if catalog_rng.random() < 0.2 else 0,
                    "operating_cost_cents": 40 + value // 100}
            confirmation = elapsed + timedelta(seconds=50)
            add_event("order_confirmed", confirmation, line, payment_links)
            sales[sku].append(confirmation)
            stock[sku] -= 1

    for elapsed, sku, journey, session_link, clicked in feed:
        available = max(0, opening_stock.get(sku, 0) - sum(sale <= elapsed for sale in sales[sku]))
        attrs = {**journey, **ALL_PRODUCTS[sku], "recommendation_model_version": "feed-v1",
                 "available_units": available}
        references = [*session_link, (sku, "recommended product")]
        impression = add_event("feed_impression", elapsed, attrs, references, catalog_rng)
        if clicked:
            add_event("feed_clicked", elapsed + timedelta(seconds=4), {**attrs, "impression_id": impression},
                      references, catalog_rng)

    for country, channel in product(("DE", "FR"), ("paid_search", "display")):
        campaign = f"campaign-{country}-{channel}"
        campaign_attrs = {"target_country": country, "advertising_channel": channel, "campaign_id": campaign}
        add_object(f"{prefix}{campaign}", "campaign", campaign_attrs)
        references = [(f"{prefix}{campaign}", "acquisition campaign")]
        add_event("campaign_budget", timedelta(0), {**campaign_attrs, "budget_cents": 30000}, references, catalog_rng)
        for _ in range(35):
            elapsed = timedelta(seconds=catalog_rng.randrange(0, 59 * 60))
            impression = add_event("ad_impression", elapsed, campaign_attrs, references, catalog_rng)
            if catalog_rng.random() < 0.18:
                add_event("ad_clicked", elapsed + timedelta(seconds=5),
                          {**campaign_attrs, "impression_id": impression, "cost_cents": catalog_rng.randint(50, 180)},
                          references, catalog_rng)

    for sku, units in (stock | dict.fromkeys(FEED_ONLY, 0)).items():
        add_event("inventory_snapshot", timedelta(minutes=59),
                  {"sku": sku, "available_units": units, **ALL_PRODUCTS[sku]}, [(sku, "product")])

    window = db.execute(
        """insert into windows (start_at, end_at, expected_events, observation_snapshot)
           values (%s, %s, %s, %s)
           on conflict (start_at) do nothing returning start_at""",
        (start, start + HOUR, len(events), start + HOUR + timedelta(minutes=15)),
    ).fetchone()
    if not window:
        return
    with db.cursor() as cursor:
        cursor.executemany(
            "insert into objects values (%s, 'product', %s) on conflict (id) do nothing",
            [(sku, Jsonb(attrs)) for sku, attrs in ALL_PRODUCTS.items()],
        )
        events.sort(key=lambda row: row[2])
        for table, data in (("objects", objects), ("events", events), ("event_objects", links)):
            with cursor.copy(f"copy {table} from stdin") as copy:
                for row in data:
                    copy.write_row(row)
        manifest = Counter((row[2].replace(second=0, microsecond=0), row[5].obj["source_id"], row[1])
                           for row in events)
        with cursor.copy("copy source_manifests from stdin") as copy:
            for (minute, source, activity), count in manifest.items():
                copy.write_row((start, minute, source, activity, count))
