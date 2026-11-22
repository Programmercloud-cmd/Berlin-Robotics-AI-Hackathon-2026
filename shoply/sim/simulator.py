"""Shoply marketplace simulator (Data component).

Generates one hour at a time, for history and for the live run alike, and writes
additive counts and EUR sums into DuckDB. Scenario effects multiply (or, for a
few parameters, replace) the simulator's own parameters, never its outputs.

Randomness: every hour draws from its own `SeedSequence([seed, hour_index])`
(hour_index = hours since 2026-08-31 00:00 UTC; earlier hours of the year use
`[seed, 2**32 - 1, -hour_index]`). Each draw site gets a child
stream of that sequence and samples by inverse CDF from a fixed number of
uniforms, so a scenario that changes one row's parameter leaves the random
numbers of every other row untouched (common random numbers): A/B runs with and
without scenarios differ only where the scenarios act.
"""
from __future__ import annotations

import json
import pickle
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, NamedTuple
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd
from scipy import stats

from shoply.contracts import Event, seg_key
from shoply.sim import params as P
from shoply.sim.calendar import DEFAULT_CALENDAR, Calendar, load_calendar
from shoply.sim.scenarios import DEFAULT_PATH, Scenario, ScenarioSet, load_scenarios
from shoply.warehouse.schema import COLUMNS, create_schema

HOUR = timedelta(hours=1)
STATE_VERSION = 2

CO = list(P.COUNTRIES)
PL = list(P.PLATFORMS)
CH = list(P.CHANNELS)
CAT = list(P.CATEGORIES)
ADV = list(P.ADVERTISERS)
TZ = ZoneInfo(P.TIMEZONE)


def hour_curve() -> np.ndarray:
    """Relative session intensity per local hour (mean 1, circular)."""
    h = np.arange(24)

    def bump(center, width):
        d = np.minimum(np.abs(h - center), 24 - np.abs(h - center))
        return np.exp(-0.5 * (d / width) ** 2)

    w = (P.HOUR_CURVE_FLOOR + bump(P.PEAK_LOCAL_HOUR, P.HOUR_CURVE_PEAK_WIDTH)
         + P.HOUR_CURVE_LUNCH_WEIGHT * bump(P.LUNCH_LOCAL_HOUR, P.HOUR_CURVE_LUNCH_WIDTH))
    return w / w.mean()


HOURLY = hour_curve()


def hour_index(ts: datetime) -> int:
    return int((ts - P.HISTORY_START) // HOUR)


def _views_per_session(ch: str) -> float:
    return (P.SEARCHES_PER_SESSION[ch] * P.SEARCH_CTR + P.DIRECT_VIEWS_PER_SESSION[ch]
            + P.FEED_IMPRESSIONS_PER_SESSION[ch] * P.FEED_CTR * (1 - P.FEED_OOS_BASE_SHARE))


def expected_daily_orders() -> np.ndarray:
    """Weekday orders per category at baseline (planner's starting forecast)."""
    vps = sum(P.CHANNELS[c] * _views_per_session(c) for c in CH)
    return np.array([P.DAILY_SESSIONS * vps * P.CATEGORY_VIEW_SHARE[c] * P.CATEGORY_ADD_TO_CART[c]
                     * P.CHECKOUT_RATE * P.PAYMENT_SUCCESS_RATE for c in CAT])


class Local(NamedTuple):
    hour: int
    weekend: bool
    day: date


class HourRNG:
    """Random draws of one hour; one child stream of SeedSequence([seed, hour]) per draw site."""

    def __init__(self, seed: int, hi: int):
        self.entropy = [int(seed), int(hi)] if hi >= 0 else [int(seed), 2**32 - 1, -int(hi)]

    def gen(self, site: str) -> np.random.Generator:
        return np.random.default_rng(np.random.SeedSequence(self.entropy, spawn_key=(zlib.crc32(site.encode()),)))

    def uniform(self, site: str, shape) -> np.ndarray:
        return np.clip(self.gen(site).random(shape), 1e-12, 1 - 1e-12)

    def normal(self, site: str, shape) -> np.ndarray:
        return self.gen(site).standard_normal(shape)

    def poisson(self, site: str, lam) -> np.ndarray:
        lam = np.clip(np.asarray(lam, dtype=float), 0, None)
        out = stats.poisson.ppf(self.uniform(site, lam.shape), np.where(lam > 0, lam, 1.0))
        return np.where(lam > 0, out, 0).astype(np.int64)

    def binomial(self, site: str, n, p) -> np.ndarray:
        n = np.asarray(n, dtype=np.int64)
        p = np.clip(np.broadcast_to(np.asarray(p, dtype=float), n.shape), 0, 1)
        ok = (n > 0) & (p > 0)
        out = stats.binom.ppf(self.uniform(site, n.shape), np.where(ok, n, 1), np.where(ok, p, 0.5))
        return np.where(ok, out, 0).astype(np.int64)

    def gamma(self, site: str, shape, scale) -> np.ndarray:
        shape = np.asarray(shape, dtype=float)
        scale = np.broadcast_to(np.asarray(scale, dtype=float), shape.shape)
        ok = (shape > 0) & (scale > 0)
        out = stats.gamma.ppf(self.uniform(site, shape.shape), np.where(ok, shape, 1.0),
                              scale=np.where(ok, scale, 1.0))
        return np.where(ok, out, 0.0)

    def split(self, site: str, n: np.ndarray, shares: np.ndarray) -> np.ndarray:
        """Multinomial split of each n into len(shares) parts, as sequential binomials."""
        out = np.zeros((len(n), len(shares)), dtype=np.int64)
        left, rest = np.asarray(n, dtype=np.int64).copy(), 1.0
        for j, s in enumerate(shares[:-1]):
            out[:, j] = self.binomial(f"{site}.{j}", left, s / rest)
            left -= out[:, j]
            rest -= s
        out[:, -1] = left
        return out


def _normalised(country: dict, platform: dict, co: np.ndarray, pl: np.ndarray) -> np.ndarray:
    norm = sum(P.COUNTRIES[c] * P.PLATFORMS[p] * country[c] * platform[p] for c in CO for p in PL)
    return np.array([country[c] * platform[p] for c, p in zip(co, pl)]) / norm


class Simulator:
    """Data-side simulator: `build_history()`, `step()`, `inject(id)`, `save_state()` / `load_state()`."""

    def __init__(self, db: str | Path | duckdb.DuckDBPyConnection = ":memory:", seed: int = 42,
                 scenarios: str | Path | list[Scenario] | None = DEFAULT_PATH,
                 exclude: Iterable[str] = (), start: datetime = P.HISTORY_START):
        if start.minute or start.second or start.microsecond:
            raise ValueError(f"start must be a whole UTC hour, got {start}")
        if start < P.YEAR_START:
            raise ValueError(f"start must not be before {P.YEAR_START}")
        self.con = db if isinstance(db, duckdb.DuckDBPyConnection) else duckdb.connect(str(db))
        create_schema(self.con)
        if self.con.execute("SELECT COUNT(*) FROM ingestion_log").fetchall()[0][0]:
            raise ValueError("warehouse already contains data; use a fresh database or Simulator.load_state")
        self.seed = int(seed)
        if scenarios is None:
            library: list[Scenario] = []
        elif isinstance(scenarios, (str, Path)):
            library = load_scenarios(scenarios)
        else:
            library = list(scenarios)
        self.library = {s.id: s for s in library}
        self.calendar: Calendar = load_calendar(DEFAULT_CALENDAR)
        exclude = set(exclude)
        unknown = exclude - set(self.library) - self.calendar.ids
        if unknown:
            raise ValueError(f"unknown scenario or calendar ids in exclude: {sorted(unknown)}")
        self.scenarios = ScenarioSet([s for s in [*self.calendar.scenarios, *library]
                                      if s.id not in exclude and s.id.removesuffix("_before") not in exclude])
        self.start = start
        self.now = start                         # next hour to generate
        self._buf: dict[str, list[pd.DataFrame]] = {t: [] for t in COLUMNS}
        self._late: list[tuple[datetime, str, datetime, pd.DataFrame]] = []  # (due, table, ts, rows)
        self._build_grids()
        self._build_world()

    # ------------------------------------------------------------------ setup
    def _build_grids(self):
        t = [(c, p, h) for c in CO for p in PL for h in CH]
        self.tg = {"country": np.array([x[0] for x in t]), "platform": np.array([x[1] for x in t]),
                   "channel": np.array([x[2] for x in t])}
        self.nt = len(t)
        self.cg = {k: np.repeat(v, len(CAT)) for k, v in self.tg.items()}
        self.cg["category"] = np.tile(np.array(CAT), self.nt)
        self.nc = self.nt * len(CAT)
        self.cat_idx_c = np.tile(np.arange(len(CAT)), self.nt)
        self.ig = {"category": np.repeat(np.array(CAT), P.SKUS_PER_CATEGORY)}
        self.ni = len(CAT) * P.SKUS_PER_CATEGORY
        self.skus = np.array([f"{c}_{i:02d}" for c in CAT for i in range(1, P.SKUS_PER_CATEGORY + 1)])
        a = [(c, v) for c in CO for v in ADV]
        self.ag = {"country": np.array([x[0] for x in a]), "advertiser": np.array([x[1] for x in a])}
        self.na = len(a)
        co, pl, ch = self.tg["country"], self.tg["platform"], self.tg["channel"]
        self.t_share = (np.array([P.COUNTRIES[c] for c in co]) * np.array([P.PLATFORMS[p] for p in pl])
                        * np.array([P.CHANNELS[h] for h in ch]))
        self.t_weekend = np.where(np.isin(pl, list(P.MOBILE)), P.WEEKEND_MULT["mobile"], P.WEEKEND_MULT["web"])
        self.c_atc = np.repeat(_normalised(P.COUNTRY_ADD_TO_CART, P.PLATFORM_ADD_TO_CART, co, pl), len(CAT)) \
            * np.tile([P.CATEGORY_ADD_TO_CART[c] for c in CAT], self.nt)
        self.c_aov = np.repeat(_normalised(P.COUNTRY_AOV, P.PLATFORM_AOV, co, pl), len(CAT)) \
            * np.tile([P.CATEGORY_AOV[c] for c in CAT], self.nt)
        self.c_take = np.tile([P.CATEGORY_TAKE_RATE[c] for c in CAT], self.nt)

    def _build_world(self):
        rng = HourRNG(self.seed, 0).gen("world")
        k = P.SKUS_PER_CATEGORY
        zipf = 1.0 / np.arange(1, k + 1) ** P.SKU_ZIPF_EXPONENT
        zipf = zipf / zipf.sum()
        true = np.tile(zipf, len(CAT)) * rng.lognormal(0.0, P.DEMAND_FORECAST_ERROR_SD, self.ni)
        self.sku_weight = true / np.repeat(true.reshape(len(CAT), k).sum(axis=1), k)   # true demand share
        self.forecast = np.repeat(expected_daily_orders(), k) * np.tile(zipf, len(CAT))  # planner, units/day
        self.stock = np.round(self.forecast * (P.ORDER_UP_TO_DAYS * P.ORDER_UP_TO_SAFETY
                                               - P.DELIVERY_CYCLE_DAYS)).astype(np.int64)
        self.in_stock = (self.stock > 0).astype(np.int32)
        self.pending_po: list[tuple[datetime, int, np.ndarray]] = []   # (placed, category, units)
        first_po = self.start.replace(hour=P.PO_HOUR_UTC, minute=0, second=0, microsecond=0)
        if first_po < self.start:
            first_po += timedelta(days=1)
        self._po_anchor = first_po               # POs at anchor + k * cycle
        self._sales_acc = np.zeros(self.ni, dtype=np.int64)
        self._sales_hours = 0
        # ads: daily budgets per advertiser (sum 25k EUR), split across countries by session share
        bw = rng.lognormal(0.0, P.AD_BUDGET_SD, len(ADV))
        self.adv_budget = P.TOTAL_DAILY_AD_BUDGET * bw / bw.sum()
        self.adv_cpc = P.AD_CPC_MEAN * rng.lognormal(0.0, 0.25, len(ADV))
        adv_i = np.array([ADV.index(a) for a in self.ag["advertiser"]])
        self.ad_budget_row = self.adv_budget[adv_i] * np.array([P.COUNTRIES[c] for c in self.ag["country"]])
        self.ad_cpc_row = self.adv_cpc[adv_i]
        self.ad_cum_spend = np.zeros(self.na)
        self.ad_day: date | None = None

    @staticmethod
    def _cat_slice(ci: int) -> slice:
        return slice(ci * P.SKUS_PER_CATEGORY, (ci + 1) * P.SKUS_PER_CATEGORY)

    def in_stock_rate(self) -> np.ndarray:
        """Demand-weighted in-stock rate per category, sum(in_stock * w) / sum(w)."""
        return (self.in_stock * self.sku_weight).reshape(len(CAT), -1).sum(axis=1)

    # ------------------------------------------------------------- public API
    def build_history(self, until: datetime = P.LIVE_START) -> None:
        """Generate history from `start` to the live start (calendar and past incidents, no demo scenario)."""
        self.run_until(until)

    generate_history = build_history

    def run_until(self, until: datetime, flush_every: int = 24 * 7) -> None:
        if until.tzinfo is not None:
            until = until.astimezone(timezone.utc).replace(tzinfo=None)
        n = 0
        while self.now < until:
            self._generate_hour(self.now)
            self.now += HOUR
            n += 1
            if n % flush_every == 0:
                self.flush()
        self.flush()

    def step(self) -> datetime:
        """Generate and write the hour `self.now`; return its timestamp."""
        ts = self.now
        self._generate_hour(ts)
        self.now += HOUR
        self.flush()
        return ts

    def inject(self, scenario_id: str) -> Scenario:
        """Start a scenario from the library at the next generated hour, keeping its duration."""
        if scenario_id not in self.library:
            raise KeyError(f"unknown scenario {scenario_id!r}")
        s = self.library[scenario_id].shifted(self.now, f"@{self.now:%Y-%m-%dT%H}")
        self.scenarios.add(s)
        return s

    def save_state(self, path: str | Path) -> None:
        """Write everything needed to continue the run (not the warehouse) to `path`."""
        self.flush()
        state = {k: v for k, v in self.__dict__.items() if k != "con"}
        state["_fingerprint"] = self._fingerprint()
        with open(path, "wb") as f:
            pickle.dump({"version": STATE_VERSION, "state": state}, f)

    @classmethod
    def load_state(cls, path: str | Path, db: str | Path | duckdb.DuckDBPyConnection) -> "Simulator":
        """Continue a run saved with `save_state` on the warehouse it was writing to."""
        with open(path, "rb") as f:
            data = pickle.load(f)
        if data.get("version") != STATE_VERSION:
            raise ValueError(f"unsupported state version {data.get('version')!r}")
        sim = cls.__new__(cls)
        sim.__dict__.update(data["state"])
        sim.con = db if isinstance(db, duckdb.DuckDBPyConnection) else duckdb.connect(str(db))
        create_schema(sim.con)
        last = sim.con.execute("SELECT MAX(ts) FROM ingestion_log").fetchall()[0][0]
        expected = sim.now - HOUR if sim.now > sim.start else None
        if last != expected:
            raise ValueError(f"warehouse ends at {last}, state expects {expected}")
        if sim.__dict__.pop("_fingerprint", None) != sim._fingerprint():
            raise ValueError("warehouse was not written by this run (row counts or totals differ)")
        return sim

    def _fingerprint(self) -> tuple:
        """Row counts and totals that tell apart warehouses of different runs ending at the same hour."""
        out = [self.seed]
        for t in COLUMNS:
            out.append(self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchall()[0][0])
        out += self.con.execute("SELECT SUM(sessions) FROM fact_traffic").fetchall()[0]
        out += self.con.execute("SELECT SUM(orders), ROUND(SUM(gmv_eur), 2) FROM fact_commerce").fetchall()[0]
        out += self.con.execute("SELECT ROUND(SUM(spend_eur), 2) FROM fact_ads").fetchall()[0]
        return tuple(out)

    def flush(self) -> None:
        for table, frames in self._buf.items():
            if not frames:
                continue
            df = pd.concat(frames, ignore_index=True)[COLUMNS[table]]
            self._buf[table] = []
            if table == "events":
                self.con.executemany(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [tuple(None if (isinstance(v, float) and np.isnan(v)) else v for v in r)
                     for r in df.itertuples(index=False, name=None)])
            else:
                self._append(table, df)

    def _append(self, table: str, df: pd.DataFrame) -> None:
        self.con.append(table, df)
        self.con.unregister("__append_df")

    # --------------------------------------------------------------- one hour
    @staticmethod
    def _local(ts: datetime) -> Local:
        lt = ts.replace(tzinfo=timezone.utc).astimezone(TZ)
        return Local(lt.hour, lt.weekday() >= 5, lt.date())

    def _generate_hour(self, ts: datetime) -> None:
        r = HourRNG(self.seed, hour_index(ts))
        loc = self._local(ts)
        self._deliver_late(ts)
        self._write_events(ts, r)
        traffic = self._traffic(ts, r, loc)
        delivered = self._inventory_start(ts)
        commerce, cat_orders = self._commerce(ts, r, traffic)
        inventory = self._inventory_end(ts, r, cat_orders, delivered)
        ads = self._ads(ts, r, loc)
        for table, df in (("fact_traffic", traffic["df"]), ("fact_commerce", commerce),
                          ("fact_inventory", inventory), ("fact_ads", ads)):
            self._ingest(table, ts, df, r)

    # traffic ---------------------------------------------------------------
    def _traffic(self, ts, r: HourRNG, loc: Local) -> dict:
        g, n, sc = self.tg, self.nt, self.scenarios
        co, ch = g["country"], g["channel"]
        mult = lambda e: sc.multiplier(e, ts, g, n)  # noqa: E731
        lam = (P.DAILY_SESSIONS / 24 * HOURLY[loc.hour] * self.t_share
               * (self.t_weekend if loc.weekend else 1.0) * self.calendar.growth(loc.day)
               * mult("sessions"))
        k = P.SESSION_GAMMA_SHAPE
        sessions = r.poisson("sessions", lam * r.gamma("sessions.noise", np.full(n, k), 1 / k))
        searches = r.poisson("searches", sessions * np.array([P.SEARCHES_PER_SESSION[h] for h in ch])
                             * mult("searches"))
        rel = P.SEARCH_RELEVANCE * mult("search_relevance")
        relevance_sum = np.clip(searches * rel + r.normal("relevance", n) * P.SEARCH_RELEVANCE_SD
                                * np.sqrt(searches), 0, searches)
        search_ctr = P.SEARCH_CTR * rel / P.SEARCH_RELEVANCE * mult("search_ctr")
        search_clicks = r.binomial("search_clicks", searches, search_ctr)
        acq = np.where(ch == "paid", sessions * P.ACQUISITION_COST_PER_PAID_SESSION * mult("acquisition_cpc"), 0.0)
        df = pd.DataFrame({"ts": ts, "country": co, "platform": g["platform"], "channel": ch,
                           "sessions": sessions, "searches": searches,
                           "relevance_sum": relevance_sum, "relevance_n": searches,
                           "acquisition_cost_eur": acq})
        return {"df": df, "sessions": sessions, "search_clicks": search_clicks}

    # commerce --------------------------------------------------------------
    def _commerce(self, ts, r: HourRNG, traffic):
        g, n, sc, k = self.cg, self.nc, self.scenarios, len(CAT)
        cat_share = np.array([P.CATEGORY_VIEW_SHARE[c] for c in CAT])
        ch = g["channel"]
        sess = np.repeat(traffic["sessions"], k)
        cshare = np.tile(cat_share, self.nt)
        mult = lambda e: sc.multiplier(e, ts, g, n)  # noqa: E731

        search_views = r.split("search_views", traffic["search_clicks"], cat_share).reshape(-1)
        direct_views = r.poisson("direct_views", sess * np.array([P.DIRECT_VIEWS_PER_SESSION[h] for h in ch])
                                 * cshare * mult("direct_views"))
        feed_impr = r.poisson("feed_impressions", sess * np.array([P.FEED_IMPRESSIONS_PER_SESSION[h] for h in ch])
                              * cshare * mult("feed_impressions"))

        isr = self.in_stock_rate()[self.cat_idx_c]
        oos_base = np.clip(P.FEED_OOS_BASE_SHARE + P.FEED_OOS_SENSITIVITY * (1 - isr), 0, 1)
        oos_set = sc.override("oos_exposure_share", ts, g, n, np.nan).astype(float)
        oos_share = np.where(np.isnan(oos_set), oos_base, oos_set)
        oos_share = np.where(isr <= 0, 1.0, oos_share)      # nothing in stock: every impression is OOS
        feed_oos = r.binomial("feed_oos", feed_impr, oos_share)
        feed_ctr = P.FEED_CTR * mult("feed_ctr")
        fv_oos = r.binomial("feed_clicks.oos", feed_oos, feed_ctr)     # cannot convert
        fv_ok = r.binomial("feed_clicks.ok", feed_impr - feed_oos, feed_ctr)
        feed_views = fv_oos + fv_ok

        atc = self.c_atc * mult("add_to_cart_rate")
        atc_nonfeed = atc * isr / P.BASE_IN_STOCK_RATE
        chk = P.CHECKOUT_RATE * mult("checkout_rate")
        pay = P.PAYMENT_SUCCESS_RATE * mult("payment_success_rate")
        carts_n = r.binomial("carts", search_views + direct_views, atc_nonfeed)
        carts_f = r.binomial("carts.feed", fv_ok, atc)
        chk_n = r.binomial("checkouts", carts_n, chk)
        chk_f = r.binomial("checkouts.feed", carts_f, chk)
        ord_n = r.binomial("orders", chk_n, pay)
        ord_f = r.binomial("orders.feed", chk_f, pay)
        self._cap_to_stock(r, carts_n, carts_f, chk_n, chk_f, ord_n, ord_f)
        carts, checkouts, orders = carts_n + carts_f, chk_n + chk_f, ord_n + ord_f

        aov = self.c_aov * mult("avg_order_value")
        a = 1.0 / P.ORDER_VALUE_CV ** 2                     # sum of `orders` Gamma(a, aov / a) order values
        gmv = r.gamma("gmv", orders * a, aov / a)
        discounts = gmv * P.DISCOUNT_RATE * mult("discount_rate")
        fees = gmv * self.c_take * mult("take_rate")
        pay_costs = P.PAYMENT_COST_RATE * gmv + P.PAYMENT_COST_PER_CHECKOUT * checkouts

        df = pd.DataFrame({
            "ts": ts, "country": g["country"], "platform": g["platform"], "channel": ch,
            "category": g["category"], "search_views": search_views, "feed_views": feed_views,
            "direct_views": direct_views, "feed_impressions": feed_impr,
            "feed_oos_impressions": feed_oos, "carts": carts, "checkouts": checkouts,
            "orders": orders, "feed_orders": ord_f, "gmv_eur": gmv,
            "discounts_eur": discounts, "fees_eur": fees, "payment_costs_eur": pay_costs})
        cat_orders = np.bincount(self.cat_idx_c, weights=orders, minlength=k).astype(np.int64)
        return df, cat_orders

    def _cap_to_stock(self, r: HourRNG, carts_n, carts_f, chk_n, chk_f, ord_n, ord_f) -> None:
        """Demand beyond a category's stock never converts: drop it from carts, checkouts and orders."""
        k = len(CAT)
        cat_stock = self.stock.reshape(k, -1).sum(axis=1)
        over = np.bincount(self.cat_idx_c, weights=ord_n + ord_f, minlength=k) > cat_stock
        if not over.any():
            return
        for ci in np.nonzero(over)[0]:
            rng = r.gen(f"stockout.{ci}")             # per category: other categories keep their draws
            rows = np.nonzero(self.cat_idx_c == ci)[0]
            kept = rng.multivariate_hypergeometric(np.concatenate([ord_n[rows], ord_f[rows]]), int(cat_stock[ci]))
            frac = 1 - cat_stock[ci] / (ord_n[rows].sum() + ord_f[rows].sum())
            for (ca, ck, od), drop in (((carts_n, chk_n, ord_n), ord_n[rows] - kept[:len(rows)]),
                                       ((carts_f, chk_f, ord_f), ord_f[rows] - kept[len(rows):])):
                failed = rng.binomial(ck[rows] - od[rows], frac)
                abandoned = rng.binomial(ca[rows] - ck[rows], frac)
                od[rows] -= drop
                ck[rows] -= drop + failed
                ca[rows] -= drop + failed + abandoned

    # inventory -------------------------------------------------------------
    def _inventory_start(self, ts) -> np.ndarray:
        """Deliveries due this hour arrive before customers shop."""
        delivered = np.zeros(self.ni, dtype=np.int64)
        lag = self.scenarios.override("delivery_lag_days", ts, self.ig, self.ni,
                                      float(P.DELIVERY_LAG_DAYS)).astype(float)
        keep = []
        for placed, ci, units in self.pending_po:
            sl = self._cat_slice(ci)
            if ts >= placed + timedelta(days=float(lag[sl.start])):
                delivered[sl] += units
            else:
                keep.append((placed, ci, units))
        self.pending_po = keep
        self.stock += delivered
        self.in_stock = (self.stock > 0).astype(np.int32)
        return delivered

    def _inventory_end(self, ts, r: HourRNG, cat_orders, delivered) -> pd.DataFrame:
        sold = np.zeros(self.ni, dtype=np.int64)
        for ci in range(len(CAT)):                    # orders go to in-stock SKUs by true demand
            rng = r.gen(f"inventory.{ci}")
            sl = self._cat_slice(ci)
            left = int(cat_orders[ci])
            while left > 0:
                avail = self.stock[sl] - sold[sl]
                w = self.sku_weight[sl] * (avail > 0)
                if w.sum() == 0:
                    break
                draw = np.minimum(rng.multinomial(left, w / w.sum()), avail)
                sold[sl] += draw
                left -= int(draw.sum())
        self.stock -= sold
        po = np.zeros(self.ni, dtype=np.int64)
        if ts.hour == P.PO_HOUR_UTC:
            if self._sales_hours >= 24:               # daily EWMA of observed sales (lost sales are invisible)
                observed = self._sales_acc * 24 / self._sales_hours
                self.forecast = (1 - P.FORECAST_EWMA_ALPHA) * self.forecast + P.FORECAST_EWMA_ALPHA * observed
                self._sales_acc[:] = 0
                self._sales_hours = 0
            since = ts - self._po_anchor
            if since >= timedelta(0) and since % timedelta(days=P.DELIVERY_CYCLE_DAYS) == timedelta(0):
                on_order = np.zeros(self.ni, dtype=np.int64)
                for _, ci, units in self.pending_po:
                    on_order[self._cat_slice(ci)] += units
                target = P.ORDER_UP_TO_DAYS * P.ORDER_UP_TO_SAFETY * self.forecast
                po = np.maximum(0, np.round(target - self.stock - on_order)).astype(np.int64)
                for ci in range(len(CAT)):
                    self.pending_po.append((ts, ci, po[self._cat_slice(ci)].copy()))
        self._sales_acc += sold
        self._sales_hours += 1
        self.in_stock = (self.stock > 0).astype(np.int32)
        return pd.DataFrame({"ts": ts, "category": self.ig["category"], "sku": self.skus,
                             "in_stock": self.in_stock.copy(), "demand_weight": self.sku_weight,
                             "delivered_units": delivered, "po_units": po})

    # ads -------------------------------------------------------------------
    def _ads(self, ts, r: HourRNG, loc: Local) -> pd.DataFrame:
        g, n, sc = self.ag, self.na, self.scenarios
        if self.ad_day != loc.day:                    # budgets reset at local midnight
            self.ad_cum_spend[:] = 0.0
            self.ad_day = loc.day
        planned = self.ad_budget_row * HOURLY[loc.hour] / 24
        cpc = self.ad_cpc_row * sc.multiplier("ad_cpc", ts, g, n)
        expected_clicks = planned * sc.multiplier("ad_spend", ts, g, n) / cpc
        impressions = r.poisson("ad_impressions", expected_clicks / P.AD_CTR)
        clicks = r.binomial("ad_clicks", impressions, P.AD_CTR)      # ~ Poisson(planned / cpc)
        click_cost = cpc * np.exp(0.03 * r.normal("ad_cost", n))
        remaining = np.clip(self.ad_budget_row - self.ad_cum_spend, 0, None)
        capped = np.minimum(clicks, np.floor(remaining / click_cost)).astype(np.int64)
        # an ad stops serving once its budget runs out, so impressions shrink with the capped clicks
        served = np.where(clicks > 0, capped / np.maximum(clicks, 1), (remaining >= click_cost).astype(float))
        impressions = np.round(impressions * served).astype(np.int64)
        clicks = capped
        spend = clicks * click_cost
        self.ad_cum_spend += spend
        return pd.DataFrame({"ts": ts, "country": g["country"], "advertiser": g["advertiser"],
                             "impressions": impressions, "clicks": clicks, "spend_eur": spend,
                             "planned_spend_eur": planned})

    # events and ingestion ----------------------------------------------------
    def _background_releases(self, ts, r: HourRNG) -> list[Event]:
        """About BACKGROUND_RELEASES_PER_DAY harmless releases (red herrings)."""
        u = r.uniform("releases", 2)
        if u[0] >= P.BACKGROUND_RELEASES_PER_DAY / 24:
            return []
        services = list(P.RELEASE_SERVICES)
        svc = services[int(u[1] * len(services))]
        return [Event(id=f"release_{svc}_{ts:%Y%m%dT%H}", ts=ts, type="release", service=svc,
                      scope=dict(P.RELEASE_SERVICES[svc]), description=f"{svc} routine release",
                      planned=False)]

    def _write_events(self, ts, r: HourRNG) -> None:
        evs: list[Event] = self.scenarios.events_at(ts) + self._background_releases(ts, r)
        if evs:
            self._buf["events"].append(pd.DataFrame([{
                "id": e.id, "ts": e.ts, "type": e.type, "service": e.service,
                "scope": seg_key(e.scope), "description": e.description, "planned": e.planned,
                "expected_effect": json.dumps(e.expected_effect)} for e in evs]))

    @staticmethod
    def _arrival(ts, rng: np.random.Generator) -> datetime:
        lo, hi = P.ARRIVAL_DELAY_MIN
        return ts + HOUR + timedelta(minutes=int(rng.integers(lo, hi + 1)))

    def _ingest(self, table: str, ts: datetime, df: pd.DataFrame, r: HourRNG) -> None:
        rng = r.gen(f"ingest.{table}")
        expected = len(df)
        c = self.scenarios.table_completeness(table, ts)
        arrived = self._arrival(ts, rng)
        if c < 1.0:
            keep_n = int(round(c * expected))
            order = rng.permutation(expected)
            now_rows = df.iloc[np.sort(order[:keep_n])]
            self._late.append((ts + HOUR, table, ts, df.iloc[np.sort(order[keep_n:])]))
            df = now_rows
        self._buf[table].append(df)
        self._buf["ingestion_log"].append(pd.DataFrame([{
            "table_name": table, "ts": ts, "rows": len(df), "expected_rows": expected,
            "arrived_at": arrived}]))

    def _deliver_late(self, now: datetime) -> None:
        due = [x for x in self._late if x[0] <= now]
        if not due:
            return
        self._late = [x for x in self._late if x[0] > now]
        self.flush()
        rng = HourRNG(self.seed, hour_index(now)).gen("ingest.late")
        for _, table, ts, rows in due:
            self._append(table, rows[COLUMNS[table]])
            self.con.execute(
                "UPDATE ingestion_log SET rows = rows + ?, arrived_at = ? WHERE table_name = ? AND ts = ?",
                [len(rows), self._arrival(now, rng), table, ts]).fetchall()


def build(db: str | Path, seed: int = 42, until: datetime = P.LIVE_START,
          exclude: Iterable[str] = (), start: datetime = P.YEAR_START) -> Simulator:
    """Full year of history (2025-10-27 .. 2026-10-25) by default."""
    sim = Simulator(db, seed=seed, exclude=exclude, start=start)
    sim.run_until(until)
    return sim
