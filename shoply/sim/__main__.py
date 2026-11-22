"""python -m shoply.sim --db shoply.duckdb --seed 42 [--until 2026-10-29T00:00] [--exclude id ...]"""
import argparse
from datetime import datetime

from shoply.sim import params as P
from shoply.sim.simulator import Simulator


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate Shoply history into DuckDB.")
    ap.add_argument("--db", default="shoply.duckdb")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--until", default=P.LIVE_START.isoformat(),
                    help="generate hours before this UTC timestamp (default: live start)")
    ap.add_argument("--exclude", nargs="*", default=[], help="scenario ids to leave out")
    a = ap.parse_args(argv)
    sim = Simulator(a.db, seed=a.seed, exclude=a.exclude)
    sim.run_until(datetime.fromisoformat(a.until))
    for t in ("fact_traffic", "fact_commerce", "fact_inventory", "fact_ads", "events", "ingestion_log"):
        print(t, sim.con.execute(f"SELECT COUNT(*) FROM {t}").fetchall()[0][0])
    sim.con.close()


if __name__ == "__main__":
    main()
