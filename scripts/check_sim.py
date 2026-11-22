"""Simulator sanity check: same seed, with vs without android_payment_de + autumn_campaign.

Builds the 8-week history, runs 30 live hours (Mon 2026-10-26 00:00 .. Tue 06:00 UTC) and prints the
scenario A/B numbers plus check_formula for every formula in formulas.yaml.

    python scripts/check_sim.py [--seed 42]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shoply.graph import DATA_DIR  # noqa: E402
from shoply.graph.check import check_formula  # noqa: E402
from shoply.sim.scenarios import load_scenarios  # noqa: E402
from shoply.sim.simulator import Simulator  # noqa: E402
from shoply.warehouse.semantic import Semantic  # noqa: E402

SCENARIOS = ("android_payment_de", "autumn_campaign")
LIVE_HOURS = 30
MON = datetime(2026, 10, 26)
FAULT = (MON + timedelta(hours=15), MON + timedelta(hours=21))
CAMPAIGN = (MON + timedelta(hours=6), MON + timedelta(hours=12))
LIVE = (MON, MON + timedelta(hours=LIVE_HOURS))
FORMULA_WINDOW = (MON - timedelta(days=7), LIVE[1])


def run(seed: int, with_scenarios: bool) -> Simulator:
    scenarios = [s for s in load_scenarios() if s.id in SCENARIOS] if with_scenarios else None
    sim = Simulator(":memory:", seed=seed, scenarios=scenarios)
    sim.build_history()
    for _ in range(LIVE_HOURS):
        sim.step()
    return sim


def total(sem: Semantic, metric: str, window, filters=None) -> float:
    """Whole-window value: counts and sums add up; ratios are SUM(numerator) / SUM(denominator), never a mean."""
    f = sem.metric(metric, *window, filters)
    if sem.graph.metric(metric).kind in ("count", "sum"):
        return float(f["numerator"].sum())
    return float(f["numerator"].sum() / f["denominator"].sum())


def cart_abandonment(sim: Simulator, window) -> float:
    orders, carts = sim.con.execute("SELECT SUM(orders), SUM(carts) FROM fact_commerce WHERE ts >= ? AND ts < ?",
                                    list(window)).fetchall()[0]
    return 1 - orders / carts


def row(label: str, a: float, b: float, fmt: str) -> str:
    delta = f"{a / b - 1:+.1%}" if b else "n/a"
    return f"  {label:<34}{format(a, fmt):>12}{format(b, fmt):>12}{delta:>9}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=42)
    seed = ap.parse_args().seed
    with_s, without = run(seed, True), run(seed, False)
    sw, sb = Semantic(with_s.con), Semantic(without.con)
    head = f"  {'':<34}{'with':>12}{'without':>12}{'delta':>9}"

    print(f"seed {seed}; scenarios: {', '.join(SCENARIOS)}; live {LIVE[0]:%a %Y-%m-%d %H:%M} .. "
          f"{LIVE[1]:%a %H:%M} UTC\n")
    print("Payment success, Mon 15:00-21:00 UTC\n" + head)
    for country, platform in [("de", "android"), ("de", "ios"), ("fr", "android")]:
        f = {"country": country, "platform": platform}
        print(row(f"{country.upper()} x {platform}", total(sw, "payment_success_rate", FAULT, f),
                  total(sb, "payment_success_rate", FAULT, f), ".2%"))

    print("\nCampaign, Mon 06:00-12:00 UTC\n" + head)
    print(row("paid sessions", total(sw, "sessions", CAMPAIGN, {"channel": "paid"}),
              total(sb, "sessions", CAMPAIGN, {"channel": "paid"}), ",.0f"))
    print(row("orders", total(sw, "orders", CAMPAIGN), total(sb, "orders", CAMPAIGN), ",.0f"))

    print(f"\nOverall, {LIVE_HOURS} live hours\n" + head)
    print(row("conversion (orders / sessions)", total(sw, "conversion_rate", LIVE),
              total(sb, "conversion_rate", LIVE), ".2%"))
    print(row("cart abandonment (1 - orders/carts)", cart_abandonment(with_s, LIVE),
              cart_abandonment(without, LIVE), ".1%"))

    print(f"\ncheck_formula, {FORMULA_WINDOW[0]:%Y-%m-%d} .. {FORMULA_WINDOW[1]:%Y-%m-%d %H:%M} UTC, with scenarios")
    ok = True
    for f in yaml.safe_load((DATA_DIR / "formulas.yaml").read_text()):
        r = check_formula(f["output"], f["expr"], *FORMULA_WINDOW, sem=sw)
        ok &= r.holds
        failing = ", ".join(s.segment or "total" for s in r.failing_slices) or "-"
        print(f"  {'PASS' if r.holds else 'FAIL'}  {f['output']} = {f['expr']}\n"
              f"        median err {r.median_rel_error:.2e}, within tol {r.share_within:.1%}, "
              f"{len(r.slices)} slices, failing: {failing}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
