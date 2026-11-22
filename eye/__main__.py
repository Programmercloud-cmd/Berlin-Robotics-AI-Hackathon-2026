import argparse
import logging
import os
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import sleep

import uvicorn
from dotenv import load_dotenv

from eye import demo, investigation, runtime, simulator
from eye.db import connect, setup
from eye.model import configured_model
from eye.web import make_app

log = logging.getLogger(__name__)


def seed(dsn, fresh=False):
    with connect(dsn) as db:
        setup(db)
        if db.execute("select exists (select 1 from windows) as populated").fetchone()["populated"]:
            if fresh:
                raise RuntimeError("The regression check requires an empty Eye database.")
        else:
            log.info("Generating historical marketplace events")
            simulator.seed(db)
    runtime.initialize(dsn)


def main():
    parser = argparse.ArgumentParser(prog="python -m eye")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("operator", help="open the test lab launcher on port 8001")
    commands.add_parser("lab", help="run the test lab and its local simulator on port 8002")
    initialize = commands.add_parser("init", help="seed baseline and discover the marketplace")
    initialize.add_argument("--search", action="store_true", help=argparse.SUPPRESS)
    simulate = commands.add_parser("simulate", help="emit committed marketplace windows")
    simulate.add_argument("--windows", type=int, default=0, help="0 runs continuously")
    simulate.add_argument("--delay", type=float, default=2.0)
    simulate.add_argument("--traffic", type=float, default=1.0)
    simulate.add_argument("--payment-disruption", action="store_true")
    simulate.add_argument("--search", action="store_true", help=argparse.SUPPRESS)
    simulate.add_argument("--search-disruption", action="store_true")
    serve = commands.add_parser("serve", help="run monitoring, dashboard and MCP")
    serve.add_argument("--port", type=int, default=8000)
    checks = commands.add_parser("check", help="run the payment regression in local PostgreSQL")
    checks.add_argument("--with-search", action="store_true", help="also verify the search regression")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pgserver").setLevel(logging.WARNING)
    dsn = os.getenv("DATABASE_URL")

    if args.command == "operator":
        from eye.operator import make_operator_app

        uvicorn.run(make_operator_app(), host="127.0.0.1", port=8001, access_log=False)
        return
    if args.command == "lab":
        from eye.lab import make_lab_app

        uvicorn.run(make_lab_app(), host="127.0.0.1", port=8002, access_log=False)
        return
    if args.command == "check":
        try:
            from pgserver import get_server
        except ImportError:
            parser.error("Install requirements-local.txt to run the checks.")
        with TemporaryDirectory(prefix="eye-postgres-") as directory:
            server = get_server(Path(directory) / "data")
            try:
                run_checks(server.get_uri(), args)
            finally:
                server.cleanup()
        return
    if not dsn:
        parser.error("Set DATABASE_URL in .env or the environment.")
    if args.command == "init":
        seed(dsn)
    elif args.command == "simulate":
        with connect(dsn) as db:
            last = db.execute("select max(start_at) as value from windows").fetchone()["value"]
        if last is None:
            parser.error("Run python -m eye init before simulating.")
        emitted = 0
        while not args.windows or emitted < args.windows:
            last += timedelta(hours=1)
            with connect(dsn) as db:
                simulator.emit(db, last, traffic=args.traffic, fault=args.payment_disruption,
                               search_disruption=args.search_disruption, staged=True)
                for cutoff in simulator.publication_ticks(last, drain=emitted + 1 == args.windows):
                    simulator.publish_through(db, last, cutoff)
                    db.commit()
                    sleep(args.delay / 12)
            emitted += 1
    else:
        with connect(dsn) as db:
            setup(db)
        uvicorn.run(make_app(dsn, configured_model()), host="127.0.0.1", port=args.port)


def run_checks(dsn, args):
    model = configured_model()
    seed(dsn, fresh=True)
    stop = Event()
    workers = [
        Thread(target=runtime.watch, args=(dsn, stop, model), daemon=True),
        Thread(target=investigation.watch_reports, args=(dsn, stop, model), daemon=True),
        Thread(target=investigation.watch_branches, args=(dsn, stop), daemon=True),
    ]
    for worker in workers:
        worker.start()
    try:
        demo.run(dsn, stop, model, delay=0, search=args.with_search,
                 verify_devin=os.getenv("EYE_MODEL") == "devin")
    finally:
        stop.set()
        for worker in workers:
            worker.join()


if __name__ == "__main__":
    main()
