"""python -m shoply.sim --db shoply.duckdb --seed 42 [--start 2025-10-27T00:00] [--until 2026-10-29T00:00]
[--exclude id ...] [--ground-truth DIR]"""
import argparse
from datetime import datetime

from shoply.sim import params as P
from shoply.sim.calendar import write_ground_truth
from shoply.sim.simulator import Simulator


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate Shoply history into DuckDB.")
    ap.add_argument("--db", default="shoply.duckdb")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--start", default=P.YEAR_START.isoformat(),
                    help="first generated UTC hour (default: 2025-10-27, a full year of history)")
    ap.add_argument("--until", default=P.LIVE_START.isoformat(),
                    help="generate hours before this UTC timestamp (default: live start)")
    ap.add_argument("--exclude", nargs="*", default=[], help="scenario or calendar ids to leave out")
    ap.add_argument("--ground-truth", help="also write ground_truth.csv/.yaml into this directory")
    a = ap.parse_args(argv)
    sim = Simulator(a.db, seed=a.seed, exclude=a.exclude, start=datetime.fromisoformat(a.start))
    sim.run_until(datetime.fromisoformat(a.until))
    for t in ("fact_traffic", "fact_commerce", "fact_inventory", "fact_ads", "events", "ingestion_log"):
        print(t, sim.con.execute(f"SELECT COUNT(*) FROM {t}").fetchall()[0][0])
    if a.ground_truth:
        print(*write_ground_truth(a.ground_truth, sim.calendar, exclude=a.exclude))
    sim.con.close()


if __name__ == "__main__":
    main()
