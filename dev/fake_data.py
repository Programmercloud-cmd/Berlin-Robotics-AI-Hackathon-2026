"""Simulated marketplace data source, used to develop and test the agent.

It provides the DuckDB fact tables (traffic, commerce, inventory, ads), a realistic
baseline (600k sessions/day, 20:00 local peak, weekend mobile +15% / web -10%, 1.8 views per
session, ATC 3.5%, checkout 55%, payment success 94%, AOV EUR 45, take rate 12%, payment cost 1.4%
of GMV + EUR 0.10 per checkout, 200 SKUs, supplier delivery every 3 days), the calendar (history
2026-08-31..2026-10-25, live from 2026-10-26) and the demo timeline in dev/scenarios.yaml.

The interface follows sentinel/contracts.py: metric(), completeness(), events(), plus step() for the loop.
Data arrives late like a real warehouse: an hour's rows become visible one hour after it closes, and
the late_partition scenario delays 40% of one hour by an extra tick.

Scenario effects are multipliers for payment_success_rate / feed_ctr /
sessions / add_to_cart_rate; absolute values for oos_exposure_share, completeness and
delivery_lag_days.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd
import yaml

from sentinel.contracts import Event, MetricNode, Segment

TZ = ZoneInfo("Europe/Berlin")
HISTORY_START = datetime(2026, 8, 31)
LIVE_START = datetime(2026, 10, 26)
END = datetime(2026, 10, 29)

COUNTRY = {"de": .40, "fr": .25, "es": .15, "nl": .10, "pl": .10}
PLATFORM = {"android": .45, "ios": .35, "web": .20}
CHANNEL = {"organic": .60, "paid": .25, "for_you": .15}
CATEGORY = {"electronics": .22, "fashion": .26, "home": .20, "beauty": .16, "toys": .16}
PRICE = {"electronics": 1.9, "fashion": 0.8, "home": 1.0, "beauty": 0.55, "toys": 0.7}  # relative AOV
FEED_IMPR = {"organic": 5.0, "paid": 5.0, "for_you": 12.0}  # feed impressions per session
ADVERTISERS = [f"adv_{i:03d}" for i in range(1, 31)]
TABLE_DIMS = {
    "fact_traffic": ["country", "platform", "channel"],
    "fact_commerce": ["country", "platform", "channel", "category"],
    "fact_inventory": ["category", "sku"],
    "fact_ads": ["country", "advertiser"],
}
SCENARIOS = Path(__file__).with_name("scenarios.yaml")


def _profile(local_hour: np.ndarray) -> np.ndarray:
    """Share of a day's sessions in each local hour: evening peak at 20:00, lunch bump, night trough."""
    h = np.arange(24)
    p = 0.08 + 1.0 * np.exp(-((h - 20) / 3.2) ** 2) + 0.45 * np.exp(-((h - 12.5) / 3.0) ** 2) \
        + 0.25 * np.exp(-((h - 16) / 4) ** 2)
    p[(h >= 1) & (h <= 5)] *= 0.35
    return (p / p.sum())[local_hour]


def _ts(x) -> pd.Timestamp:
    return pd.Timestamp(x).tz_localize(None) if pd.Timestamp(x).tzinfo else pd.Timestamp(x)


class FakeData:
    def __init__(self, nodes: list[MetricNode], formulas: dict[str, str] | None = None,
                 scenarios: list[str] | None = None, seed: int = 42, now: datetime = LIVE_START):
        self.nodes = {n.id: n for n in nodes}
        self.formulas = dict(formulas or {})
        spec = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
        self.scenarios = {s["id"]: s for s in spec}
        self.active = scenarios if scenarios is not None else [s["id"] for s in spec if not s.get("optional")]
        self.rng = np.random.default_rng(seed)
        self.now = pd.Timestamp(now)
        self._cc: dict = {}
        self._build()
        self.con = duckdb.connect()
        for name, df in self.tables.items():
            self.con.register(name, df)

    # ================================================================ contract interface
    def register_node(self, node: MetricNode, formula: str | None = None):
        """How Data learns about metrics added by the extender (extension of the data interface)."""
        self.nodes[node.id] = node
        if formula:
            self.formulas[node.id] = formula

    def schema(self) -> dict[str, dict[str, list[str]]]:
        """Data dictionary: dimension and measure columns of every fact table."""
        out = {}
        for t, dims in TABLE_DIMS.items():
            cols = [c for c in self.tables[t].columns if c not in ("ts", "_arrival")]
            out[t] = {"dimensions": [c for c in cols if c in dims], "measures": [c for c in cols if c not in dims]}
        return out

    def step(self):
        self.now += pd.Timedelta(hours=1)

    def metric(self, id: str, start: datetime, end: datetime, filters: Segment | None = None,
               group_by: list[str] | None = None) -> pd.DataFrame:
        node = self.nodes[id]
        filters, group_by = dict(filters or {}), list(group_by or [])
        bad = (set(filters) | set(group_by)) - set(node.dimensions)
        if bad:
            raise ValueError(f"{id} has no dimension(s) {sorted(bad)}; supports {node.dimensions}")
        start, end = _ts(start), min(_ts(end), self.now)
        keys = ["ts"] + group_by
        if node.value_sql is None and node.numerator_sql is None:
            return self._from_formula(node, start, end, filters, group_by)
        tables_used = []
        if node.kind in ("count", "sum"):
            t, df = self._side(node, node.value_sql, start, end, filters, group_by)
            tables_used.append(t)
            df = df.rename(columns={"v": "value"})
            df["numerator"], df["denominator"] = df["value"], 1.0
        else:
            tn, num = self._side(node, node.numerator_sql, start, end, filters, group_by)
            td, den = self._side(node, node.denominator_sql, start, end, filters, group_by)
            tables_used += [tn, td]
            df = num.rename(columns={"v": "numerator"}).merge(den.rename(columns={"v": "denominator"}),
                                                                on=keys, how="outer")
            df["value"] = df.numerator / df.denominator.where(df.denominator != 0)
        df["complete"] = self._complete_flags(df["ts"], tables_used)
        return df[keys + ["value", "numerator", "denominator", "complete"]].sort_values(keys).reset_index(drop=True)

    def completeness(self, table: str, ts: datetime) -> float:
        return float(self._completeness_series(table).get(_ts(ts), 0.0))

    def _completeness_series(self, table: str) -> pd.Series:
        """visible rows / expected rows per hour, cached per simulated `now`."""
        key = (table, self.now)
        if key not in self._cc:
            df = self.tables[table]
            got = df[df._arrival <= self.now].groupby("ts").size()
            log = self.tables["ingestion_log"]
            exp = log[log.table_name == table].set_index("ts").expected_rows
            self._cc = {k: v for k, v in self._cc.items() if k[1] == self.now}
            self._cc[key] = (got.reindex(exp.index).fillna(0) / exp).astype(float)
        return self._cc[key]

    def events(self, start: datetime, end: datetime, services: list[str] | None = None) -> list[Event]:
        end = min(_ts(end), self.now)
        out = [e for e in self._events if _ts(start) <= _ts(e.ts) < end]
        return [e for e in out if services is None or e.service in services]

    # ================================================================ query helpers
    def _side(self, node: MetricNode, sql: str, start, end, filters, group_by):
        qual = set(re.findall(r"\b(fact_[a-z]+)\.", sql))
        if len(qual) > 1:
            raise ValueError(f"expression mixes tables {qual}: {sql}")
        table = qual.pop() if qual else node.table
        expr = re.sub(r"\bfact_[a-z]+\.", "", sql)
        dims = TABLE_DIMS[table]
        for d in list(filters) + group_by:
            if d not in dims:
                raise ValueError(f"table {table} has no column {d}")
        where = ["ts >= ?", "ts < ?", "_arrival <= ?"]
        params: list = [start, end, self.now]
        for k, v in {**node.filters, **filters}.items():
            where.append(f"{k} = ?")
            params.append(v)
        gb = ", ".join(["ts"] + group_by)
        q = f"SELECT {gb}, {expr} AS v FROM {table} WHERE {' AND '.join(where)} GROUP BY {gb}"
        return table, self.con.execute(q, params).df()

    def _from_formula(self, node, start, end, filters, group_by):
        from sentinel.brain.formula import Formula
        f = Formula.parse(self.formulas[node.id])
        keys = ["ts"] + group_by
        parts = {v: self.metric(v, start, end, filters, group_by).set_index(keys) for v in f.variables}
        al = pd.concat({v: p.value for v, p in parts.items()}, axis=1, join="inner")
        df = pd.DataFrame({"value": f.evaluate({v: al[v] for v in f.variables})})
        complete = pd.concat({v: p.complete for v, p in parts.items()}, axis=1, join="inner").all(axis=1)
        df["numerator"], df["denominator"], df["complete"] = df["value"], 1.0, complete
        return df.reset_index()[keys + ["value", "numerator", "denominator", "complete"]]

    def _complete_flags(self, ts: pd.Series, tables: list[str]) -> pd.Series:
        flags = pd.Series(True, index=ts.index)
        for t in set(tables):
            flags &= ts.map(self._completeness_series(t)).fillna(0).values >= 0.95
        return flags

    # ================================================================ simulation
    def _active(self, sid: str):
        return self.scenarios[sid] if sid in self.active else None

    def _window(self, ts: pd.Series, sid: str) -> np.ndarray:
        s = self._active(sid)
        if not s:
            return np.zeros(len(ts), bool)
        st = _ts(s["start"])
        en = _ts(s["end"]) if s.get("end") else pd.Timestamp.max
        return ((ts >= st) & (ts < en)).values

    def _build(self):
        rng = self.rng
        hours = pd.date_range(HISTORY_START, END, freq="h", inclusive="left")
        local = hours.tz_localize("UTC").tz_convert(TZ)
        loc_hour = pd.Series(local.hour.values, index=hours)
        weekend = pd.Series(local.dayofweek.values >= 5, index=hours)
        prof = pd.Series(_profile(local.hour.values), index=hours)

        # ---------------- inventory first (in-stock affects add-to-cart)
        inv, instock_rate = self._inventory(hours, prof)

        # ---------------- traffic: hour x country x platform x channel
        tr = pd.MultiIndex.from_product([hours, list(COUNTRY), list(PLATFORM), list(CHANNEL)],
                                        names=["ts", "country", "platform", "channel"]).to_frame(index=False)
        wk = weekend.reindex(tr.ts).values
        mobile = tr.platform.isin(["android", "ios"]).values
        day_noise = np.exp(rng.normal(0, 0.03, len(hours)))  # daily-level wobble
        dn = pd.Series(np.repeat(day_noise.reshape(-1, 24).mean(axis=1), 24)[:len(hours)], index=hours)
        lam = (600_000 * tr.country.map(COUNTRY).values * tr.platform.map(PLATFORM).values
               * tr.channel.map(CHANNEL).values * prof.reindex(tr.ts).values * dn.reindex(tr.ts).values
               * np.where(wk, np.where(mobile, 1.15, 0.90), 1.0))
        camp = self._window(tr.ts, "autumn_campaign") & (tr.channel == "paid").values
        lam = np.where(camp, lam * 1.4, lam)
        tr["sessions"] = rng.poisson(lam)
        tr["searches"] = rng.poisson(tr.sessions * 0.6)
        tr["relevance_n"] = tr.searches
        tr["relevance_sum"] = tr.searches * 0.72 * np.exp(rng.normal(0, 0.01, len(tr)))
        tr["acquisition_cost_eur"] = np.where(tr.channel == "paid", tr.sessions * 0.35 * np.exp(rng.normal(0, .03, len(tr))), 0.0)

        # ---------------- commerce: traffic x category
        cm = tr[["ts", "country", "platform", "channel", "sessions", "searches"]].merge(
            pd.DataFrame({"category": list(CATEGORY)}), how="cross")
        n = len(cm)
        cs = cm.category.map(CATEGORY).values
        sess_c, srch_c = cm.sessions.values * cs, cm.searches.values * cs
        cm["search_views"] = rng.poisson(srch_c * 0.95)
        cm["direct_views"] = rng.poisson(sess_c * 0.75)
        cm["feed_impressions"] = rng.poisson(sess_c * cm.channel.map(FEED_IMPR).values)
        isr = instock_rate.stack().reindex(pd.MultiIndex.from_arrays([cm.ts, cm.category])).values
        oos_share = np.clip(1 - isr, 0.005, 1)
        shift = self._window(cm.ts, "for_you_model_shift") & (cm.channel == "for_you").values
        oos_share = np.where(shift, 0.35, oos_share)
        ctr = np.where(shift, 0.08 * 1.2, 0.08)
        cm["feed_oos_impressions"] = rng.binomial(cm.feed_impressions, oos_share)
        cm["feed_views"] = rng.binomial(cm.feed_impressions, ctr)
        oos_views = rng.binomial(cm.feed_views, np.clip(oos_share * np.where(shift, 1.6, 1.0), 0, 1))
        atc = 0.035 * np.clip(isr / 0.97, 0, 1.05) * np.exp(rng.normal(0, 0.02, n))
        camp_c = self._window(cm.ts, "autumn_campaign") & (cm.channel == "paid").values
        atc = np.where(camp_c, atc * 0.85, atc)  # low-intent campaign visitors
        carts_other = rng.binomial(cm.search_views + cm.direct_views, np.clip(atc, 0, 1))
        carts_feed = rng.binomial(cm.feed_views - oos_views, np.clip(atc, 0, 1))  # out-of-stock cards cannot be carted
        cm["carts"] = carts_other + carts_feed
        cm["checkouts"] = rng.binomial(cm.carts, 0.55)
        psr = np.full(n, 0.94)
        hit = self._window(cm.ts, "android_payment_de") & (cm.country == "de").values & (cm.platform == "android").values
        psr = np.where(hit, 0.94 * 0.755, psr)
        cm["orders"] = rng.binomial(cm.checkouts, psr)
        share_feed = np.divide(carts_feed, cm.carts.values, out=np.zeros(n), where=cm.carts.values > 0)
        cm["feed_orders"] = rng.binomial(cm.orders, share_feed)
        norm = sum(CATEGORY[c] * PRICE[c] for c in CATEGORY)
        aov = 45.0 * cm.category.map(PRICE).values / norm
        cm["gmv_eur"] = cm.orders * aov * np.exp(rng.normal(0, 0.04, n))
        cm["discounts_eur"] = cm.gmv_eur * 0.02 * np.exp(rng.normal(0, 0.05, n))
        cm["fees_eur"] = cm.gmv_eur * 0.12 * np.exp(rng.normal(0, 0.01, n))
        cm["payment_costs_eur"] = 0.014 * cm.gmv_eur + 0.10 * cm.checkouts
        cm = cm.drop(columns=["sessions", "searches"])

        # ---------------- ads: hour x country x advertiser
        ads = pd.MultiIndex.from_product([hours, list(COUNTRY), ADVERTISERS], names=["ts", "country", "advertiser"]
                                         ).to_frame(index=False)
        budget = dict(zip(ADVERTISERS, rng.uniform(300, 1500, len(ADVERTISERS))))
        cpc = dict(zip(ADVERTISERS, rng.uniform(0.25, 0.60, len(ADVERTISERS))))
        ads["planned_spend_eur"] = (ads.advertiser.map(budget).values * ads.country.map(COUNTRY).values
                                    * prof.reindex(ads.ts).values)
        pace = np.exp(rng.normal(0, 0.05, len(ads)))
        bug = self._window(ads.ts, "pacing_bug") & (ads.advertiser == "adv_017").values
        pace = np.where(bug, 2.4 * pace, pace)
        ads["spend_eur"] = ads.planned_spend_eur * pace
        ads["clicks"] = rng.poisson(ads.spend_eur / ads.advertiser.map(cpc).values)
        ads["impressions"] = rng.poisson(ads.clicks * 90)

        self.tables = {"fact_traffic": tr, "fact_commerce": cm, "fact_inventory": inv, "fact_ads": ads}
        self._arrivals()
        self._events = self._make_events(hours)

    def _inventory(self, hours: pd.DatetimeIndex, prof: pd.Series):
        rng = self.rng
        skus, cats, w = [], [], []
        for c in CATEGORY:
            z = 1 / np.arange(1, 41) ** 0.9
            z = z / z.sum()
            for i in range(40):
                skus.append(f"{c[:3]}_{i:02d}")
                cats.append(c)
                w.append(z[i] * CATEGORY[c])
        w = np.array(w)
        cats = np.array(cats)
        daily_units = 19_500 * w                      # units/day per SKU (one item per order on average)
        hourly = np.outer(prof.values, daily_units)   # expected units per hour
        target = daily_units * 6.0      # order-up-to: 3-day review + 2-day lead + 1-day safety
        stock = daily_units * 4.0
        pipeline: list[tuple[int, np.ndarray]] = []
        elec = cats == "electronics"
        delay = self.scenarios.get("supplier_delay_electronics") if "supplier_delay_electronics" in self.active else None
        d_start = _ts(delay["start"]) if delay else None
        rows_in, rows_del, rows_po = [], [], []
        for t, ts in enumerate(hours):
            delivered = np.zeros(len(w))
            for (arr, qty) in [p for p in pipeline if p[0] == t]:
                delivered += qty
            pipeline = [p for p in pipeline if p[0] != t]
            stock = stock + delivered
            po = np.zeros(len(w))
            if ts.hour == 6 and (ts - HISTORY_START).days % 3 == 0:
                in_transit = sum((q for _, q in pipeline), np.zeros(len(w)))
                po = np.maximum(target - stock - in_transit, 0).round()
                lead = np.full(len(w), 48)
                if d_start is not None and ts >= d_start:
                    lead = np.where(elec, 24 * delay["effects"]["delivery_lag_days"], 48)
                # routine supplier misses: some SKUs' shipments do not arrive (random, small stock-outs)
                shipped = np.where(rng.random(len(w)) < 0.06, 0, po)
                for L in np.unique(lead):
                    mask = lead == L
                    pipeline.append((t + int(L), np.where(mask, shipped, 0)))
            demand = rng.poisson(hourly[t])
            stock = np.maximum(stock - demand, 0)
            rows_in.append((stock > 0).astype(int))
            rows_del.append(delivered)
            rows_po.append(po)
        inv = pd.DataFrame({
            "ts": np.repeat(hours.values, len(w)), "category": np.tile(cats, len(hours)),
            "sku": np.tile(skus, len(hours)), "in_stock": np.concatenate(rows_in),
            "demand_weight": np.concatenate([hourly[t] for t in range(len(hours))]) + 1e-9,
            "delivered_units": np.concatenate(rows_del).astype(int), "po_units": np.concatenate(rows_po).astype(int)})
        g = inv.assign(x=inv.in_stock * inv.demand_weight).groupby(["ts", "category"])
        rate = (g.x.sum() / g.demand_weight.sum()).unstack()
        return inv, rate

    def _arrivals(self):
        """Rows become visible one hour after their hour closes; late_partition delays 40% of one hour."""
        logs = []
        lp = self._active("late_partition")
        for name, df in self.tables.items():
            arr = df.ts + pd.Timedelta(hours=1)
            if lp and lp.get("table") == name:
                hit = (df.ts >= _ts(lp["start"])) & (df.ts < _ts(lp["end"]))
                late = hit & (self.rng.random(len(df)) > lp["effects"]["completeness"])
                arr = arr.where(~late, arr + pd.Timedelta(hours=1))
            df["_arrival"] = arr
            per = df.groupby("ts").size()
            logs.append(pd.DataFrame({"table_name": name, "ts": per.index, "rows": per.values,
                                      "expected_rows": per.values,
                                      "arrived_at": df.groupby("ts")._arrival.max().values}))
        self.tables["ingestion_log"] = pd.concat(logs, ignore_index=True)

    def _make_events(self, hours) -> list[Event]:
        ev: list[Event] = []
        rng = np.random.default_rng(7)
        services = ["payments-android", "payments-ios", "payments-web", "app-android", "app-ios",
                    "web-frontend", "recs-model", "search", "ads-pacing"]
        day = HISTORY_START
        k = 0
        while day < LIVE_START:  # routine releases with no effect: realistic noise for investigation
            for s in services:
                if rng.random() < 0.18:
                    ev.append(Event(id=f"ev_h{k:04d}", ts=day + timedelta(hours=int(rng.integers(8, 18))),
                                    type="release", service=s, description=f"{s} routine release"))
                    k += 1
            day += timedelta(days=1)
        ev.append(Event(id="ev_hol_de", ts=datetime(2026, 10, 3), type="holiday", scope={"country": "de"},
                        description="Tag der Deutschen Einheit", planned=True))
        for s in self.scenarios.values():
            if s["id"] not in self.active:
                continue
            for key in ("event", "end_event"):
                e = s.get(key)
                if not e:
                    continue
                ts = _ts(s["start"] if key == "event" else s["end"]).to_pydatetime()
                scope = dict(s.get("scope") or {})
                if e.get("service", "").endswith("android"):
                    scope = {"platform": "android"}
                elif e.get("service") == "app-ios":
                    scope = {"platform": "ios"}
                ev.append(Event(id=f"ev_{s['id']}_{key}", ts=ts, type=e["type"], service=e.get("service"),
                                scope=scope, description=e.get("description", s["id"]),
                                planned=bool(e.get("planned", False)),
                                expected_effect=e.get("expected_effect") or {}))
        return sorted(ev, key=lambda e: e.ts)
