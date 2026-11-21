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

from eye import demo, runtime, simulator
from eye.db import connect, setup
from eye.model import configured_model
from eye.web import make_app

log = logging.getLogger(__name__)


def seed(dsn, fresh=False, search=False):
    with connect(dsn) as db:
        setup(db)
        if db.execute("select exists (select 1 from windows) as populated").fetchone()["populated"]:
            if fresh:
                raise RuntimeError("The reproducible demo requires an empty Eye database.")
        else:
            log.info("Generating historical marketplace events")
            simulator.seed(db, search=search)
    runtime.initialize(dsn)


def main():
    parser = argparse.ArgumentParser(prog="python -m eye")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="seed baseline and discover the marketplace")
    initialize.add_argument("--search", action="store_true", help="include search history")
    simulate = commands.add_parser("simulate", help="emit committed marketplace windows")
    simulate.add_argument("--windows", type=int, default=0, help="0 runs continuously")
    simulate.add_argument("--delay", type=float, default=2.0)
    simulate.add_argument("--traffic", type=float, default=1.0)
    simulate.add_argument("--payment-disruption", action="store_true")
    simulate.add_argument("--search", action="store_true")
    simulate.add_argument("--search-disruption", action="store_true")
    serve = commands.add_parser("serve", help="run monitoring, dashboard and MCP")
    demonstration = commands.add_parser("demo", help="run the reproducible end-to-end scenario")
    demonstration.add_argument("--check", action="store_true", help="verify and exit")
    demonstration.add_argument("--delay", type=float, default=1.0)
    demonstration.add_argument("--with-search", action="store_true", help="also run the search scenario")
    for command in (serve, demonstration):
        command.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pgserver").setLevel(logging.WARNING)
    dsn = os.getenv("DATABASE_URL")

    if args.command == "demo" and not dsn:
        try:
            from pgserver import get_server
        except ImportError:
            parser.error("Install requirements-local.txt or set DATABASE_URL to run the demo.")
        with TemporaryDirectory(prefix="eye-postgres-") as directory:
            server = get_server(Path(directory) / "data")
            try:
                run_demo(server.get_uri(), args)
            finally:
                server.cleanup()
        return
    if not dsn:
        parser.error("Set DATABASE_URL in .env or the environment.")
    if args.command == "demo":
        run_demo(dsn, args)
    elif args.command == "init":
        seed(dsn, search=args.search)
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
                               search=args.search or args.search_disruption,
                               search_disruption=args.search_disruption)
            emitted += 1
            sleep(args.delay)
    else:
        with connect(dsn) as db:
            setup(db)
        uvicorn.run(make_app(dsn, configured_model()), host="127.0.0.1", port=args.port)


def run_demo(dsn, args):
    model = configured_model()
    seed(dsn, fresh=True, search=args.with_search)

    if args.check:
        stop = Event()
        worker = Thread(target=runtime.watch, args=(dsn, stop, model), daemon=True)
        worker.start()
        try:
            demo.run(dsn, stop, model, delay=0, search=args.with_search,
                     verify_devin=os.getenv("EYE_MODEL") == "devin")
        finally:
            stop.set()
            worker.join()
        return

    def scenario(stop):
        try:
            demo.run(dsn, stop, model, args.delay, search=args.with_search)
        except RuntimeError:
            if not stop.is_set():
                log.exception("Demo interrupted")

    uvicorn.run(make_app(dsn, model, scenario), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
