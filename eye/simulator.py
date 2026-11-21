"""Seeded marketplace operator. Only observed, qualified OCEL events leave this module."""

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


def seed(db, days=4, seed=42, search=False):
    for hour in range(24 * days, 0, -1):
        emit(db, DEMO_START - hour * HOUR, seed=seed, search=search)


def emit(db, start, seed=42, traffic=1.0, fault=False, search=False, search_disruption=False):
    start = start.astimezone(timezone.utc)
    clock = int(start.timestamp())
    rng = Random(f"{seed}:{clock}")
    search_rng = Random(f"{seed}:{clock}:search")
    prefix = f"{clock}:"
    previous = db.execute(
        """select attrs from events where activity = 'inventory_snapshot' and window_start =
           (select max(start_at) from windows where start_at < %s)""",
        (start,),
    ).fetchall()
    stock = {row["attrs"]["sku"]: row["attrs"]["available_units"] for row in previous}
    for sku in PRODUCTS:
        if stock.get(sku, 0) < 50:
            stock[sku] = rng.randint(65, 95)

    objects, events, links = [], [], []

    def add_object(key, kind, attrs):
        objects.append((key, kind, Jsonb(attrs)))

    def add_event(activity, elapsed, attrs, references, source_rng=rng):
        identifier = f"{prefix}event-{len(events)}"
        occurred = start + elapsed
        ingested = occurred + timedelta(milliseconds=source_rng.randint(150, 4750))
        events.append((identifier, activity, occurred, ingested, start, Jsonb(attrs)))
        links.extend((identifier, key, qualifier) for key, qualifier in references)

    seasonal = 0.9 + 0.15 * cos((start.hour - 14) * pi / 12)
    volume = round(600 * seasonal * traffic)
    for index in range(volume):
        base = f"{prefix}{index}:"
        session, cart, order, payment = (f"{base}{kind}" for kind in ("session", "cart", "order", "payment"))
        country, platform, provider = rng.choices(SEGMENTS, weights=(4, 1, 1, 1, 1, 1, 1, 1))[0]
        journey = {"country": country, "platform": platform}
        checkout = {**journey, "provider": provider}
        elapsed = timedelta(seconds=rng.randrange(0, 50 * 60))
        sku = rng.choice(tuple(PRODUCTS))
        item = {**journey, "sku": sku, **PRODUCTS[sku]}
        session_link = [(session, "customer session")]
        product_link = [*session_link, (sku, "product")]

        add_object(session, "session", journey)
        add_event("session_started", elapsed, journey, session_link)
        if search and search_rng.random() < 0.76:
            request = f"{base}search"
            add_object(request, "search", journey)
            search_links = [*session_link, (request, "search request")]
            missing = 0.95 if search_disruption and journey == SEARCH_SCOPE else 0.06
            results = 0 if search_rng.random() < missing else search_rng.randint(2, 40)
            add_event("search_requested", elapsed + timedelta(seconds=1),
                      {**journey, "result_count": results}, search_links, search_rng)
            if results and search_rng.random() < 0.72:
                add_event("search_result_clicked", elapsed + timedelta(seconds=3),
                          item, [*search_links, (sku, "selected result")], search_rng)
        if rng.random() >= 0.97:
            continue
        add_event("product_viewed", elapsed + timedelta(seconds=5), item, product_link)
        if rng.random() < 0.22:
            viewed = rng.choice(tuple(PRODUCTS))
            add_event("product_viewed", elapsed + timedelta(seconds=13),
                      {**journey, "sku": viewed, **PRODUCTS[viewed]},
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
        timeout_rate = 0.63 if fault and checkout == PAYMENT_SCOPE else 0.022
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
            add_event("order_confirmed", elapsed + timedelta(seconds=50), commerce, payment_links)
            stock[sku] -= 1

    for sku, units in stock.items():
        add_event("inventory_snapshot", timedelta(minutes=59),
                  {"sku": sku, "available_units": units, **PRODUCTS[sku]}, [(sku, "product")])

    window = db.execute(
        """insert into windows values (%s, %s, %s)
           on conflict (start_at) do nothing returning start_at""",
        (start, start + HOUR, len(events)),
    ).fetchone()
    if not window:
        return
    with db.cursor() as cursor:
        cursor.executemany(
            "insert into objects values (%s, 'product', %s) on conflict (id) do nothing",
            [(sku, Jsonb(attrs)) for sku, attrs in PRODUCTS.items()],
        )
        for table, data in (("objects", objects), ("events", events), ("event_objects", links)):
            with cursor.copy(f"copy {table} from stdin") as copy:
                for row in data:
                    copy.write_row(row)
