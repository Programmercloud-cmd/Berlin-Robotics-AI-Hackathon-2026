"""Private operator test lab for the locally generated OCEL stream."""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from eye import benchmark
from eye.db import connect, setup, state
from eye.lab_results import evaluate, workflow_readings
from eye.model import configured_devin
from eye.scenario import LAB_SEED, SCENARIO_PROMPT, Scenario, ScenarioReply
from eye.simulator import HOUR, traffic_cycle

log = logging.getLogger(__name__)
EYE_URL = "http://127.0.0.1:8000/"


def port_open(port):
    with socket.socket() as connection:
        connection.settimeout(0.1)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def local_action(request: Request):
    if request.headers.get("origin") not in {None, f"http://{request.headers['host']}"}:
        raise HTTPException(403, "Open the local panel to use its controls.")


def stop_process(process):
    if process and process.poll() is None:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=12)


class Lab:
    def __init__(self, devin, eye_port=8000):
        self.devin = devin
        self.eye_port = eye_port
        self.lock = Lock()
        self.stop = Event()
        self.phase = "postgres"
        self.error = None
        self.dsn = None
        self.simulator = None
        self.eye = None
        self.messages = []
        self.runs = []
        self.active = None

    def boot(self):
        try:
            from pgserver import get_server

            if port_open(self.eye_port):
                raise RuntimeError(f"Port {self.eye_port} is in use. Stop the existing Eye dashboard first.")
            print("EYE_STARTUP postgres", flush=True)
            with TemporaryDirectory(prefix="eye-lab-") as directory:
                postgres = get_server(Path(directory) / "data")
                try:
                    self.run_processes(postgres.get_uri())
                finally:
                    try:
                        stop_process(self.simulator)
                    finally:
                        try:
                            stop_process(self.eye)
                        finally:
                            postgres.cleanup()
        except Exception as exc:
            if not self.stop.is_set():
                log.exception("Test lab could not continue")
                with self.lock:
                    self.phase = "failed"
                    self.error = str(exc) if type(exc) is RuntimeError else "See the launcher terminal for details."
                print("EYE_STARTUP failed", flush=True)

    def run_processes(self, dsn):
        with connect(dsn) as db:
            setup(db)
        if self.stop.is_set():
            return
        judge_env = {**os.environ, "DATABASE_URL": dsn}
        simulator_env = {key: value for key, value in judge_env.items()
                         if key not in {"DEVIN_API_KEY", "DEVIN_ORG_ID", "MODEL_API_KEY", "OPENAI_API_KEY"}}
        with self.lock:
            self.dsn = dsn
            self.phase = "baseline"
            self.simulator = subprocess.Popen(
                [sys.executable, "-m", "eye.lab_simulator"], env=simulator_env, text=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
        print("EYE_STARTUP baseline", flush=True)
        with self.simulator.stdout as output:
            line = output.readline()
            if self.stop.is_set():
                return
            if not line or json.loads(line).get("event") != "baseline":
                raise RuntimeError("The simulator stopped while generating the baseline.")
            with self.lock:
                self.phase = "discovery"
                self.eye = subprocess.Popen(
                    [sys.executable, "-m", "eye", "serve", "--port", str(self.eye_port)], env=judge_env,
                )
            print("EYE_STARTUP discovery", flush=True)
            deadline = time.monotonic() + 180
            while not self.stop.is_set():
                if self.eye.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Eye did not finish discovering the baseline.")
                with connect(dsn) as db:
                    initialized = state(db, "baseline_through")
                if initialized and port_open(self.eye_port):
                    break
                self.stop.wait(0.3)
            if self.stop.is_set():
                return
            with self.lock:
                self.phase = "ready"
            print("EYE_STARTUP dashboard", flush=True)
            for line in output:
                self.receive(json.loads(line))
        if not self.stop.is_set():
            raise RuntimeError("The simulator process stopped.")

    def close(self, boot):
        self.stop.set()
        with self.lock:
            simulator = self.simulator
        stop_process(simulator)
        boot.join(timeout=15)

    def receive(self, message):
        with self.lock:
            if not self.active or message["id"] != self.active["id"]:
                return
            event = message.pop("event")
            self.active.update(message)
            self.active["status"] = {
                "started": "publishing", "window": "publishing", "finished": "published", "error": "failed",
            }[event]
            if event in {"error", "finished"}:
                self.active = None

    def queue(self, *, hours=None, scenario=None, plan_id=None):
        with self.lock:
            if self.phase != "ready" or self.simulator.poll() is not None or self.eye.poll() is not None:
                raise HTTPException(409, "Wait for the simulator and Eye to be ready.")
            if self.active:
                raise HTTPException(409, "Wait for the simulator to publish its current windows.")
            run = {
                "id": uuid4().hex, "status": "queued", "published": 0, "plan_id": plan_id,
                "hours": hours, "scenario": scenario,
            }
            try:
                command = {key: run[key] for key in ("id", "hours")}
                command["scenario"] = {key: value for key, value in scenario.items() if key != "premise"} if scenario else None
                self.simulator.stdin.write(json.dumps(command) + "\n")
                self.simulator.stdin.flush()
            except OSError:
                raise HTTPException(409, "The simulator is no longer accepting commands.") from None
            self.active = run
            self.runs.append(run)
            return run["id"]

    def ask(self, prompt):
        if not self.devin:
            raise HTTPException(409, "Set DEVIN_API_KEY and DEVIN_ORG_ID in .env and relaunch the lab.")
        with self.lock:
            if any(message["status"] == "pending" for message in self.messages):
                raise HTTPException(409, "Devin is already preparing a reply.")
            item = {"id": uuid4().hex, "prompt": prompt, "status": "pending"}
            history = [{"prompt": m.get("prompt"), "message": m.get("message"), "scenario": m.get("scenario")}
                       for m in self.messages[-5:]]
            self.messages.append(item)
        Thread(target=self.interpret, args=(item, history), daemon=True).start()
        return item["id"]

    def interpret(self, item, history):
        try:
            answer, provenance = self.devin.reason(
                "scenario", SCENARIO_PROMPT, {"history": history, "request": item["prompt"]},
            )
            reply = ScenarioReply.model_validate(answer)
        except Exception as exc:
            log.warning("Test lab scenario response could not be validated (%s)", type(exc).__name__)
            with self.lock:
                item.update(status="failed", message="Devin did not return a valid scenario. Try again or import a saved plan.")
            return
        with self.lock:
            data = reply.model_dump(exclude_none=True)
            if reply.scenario:
                data["scenario"].setdefault("premise", item["prompt"])
            item.update(data, status="ready", **provenance)

    def import_plan(self, scenario):
        identifier = uuid4().hex
        with self.lock:
            self.messages.append({"id": identifier, "status": "ready", "origin": "import",
                                  "message": "Review the imported scenario before applying it.",
                                  "scenario": scenario.model_dump(exclude_none=True)})
        return identifier

    def apply(self, identifier):
        with self.lock:
            plan = next((item.get("scenario") for item in self.messages if item["id"] == identifier), None)
        if not plan:
            raise HTTPException(404, "No validated scenario for that message.")
        return self.queue(scenario=plan, plan_id=identifier)

    def snapshot(self):
        with self.lock:
            phase, error, dsn = self.phase, self.error, self.dsn
            active = self.active["id"] if self.active else None
            messages = [item.copy() for item in self.messages]
            runs = [run.copy() for run in self.runs[-20:]]
            simulator_pid = self.simulator.pid if self.simulator and self.simulator.poll() is None else None
            eye_pid = self.eye.pid if self.eye and self.eye.poll() is None else None
        result = {
            "phase": phase, "error": error, "busy": bool(active), "devin_enabled": bool(self.devin),
            "messages": messages, "runs": runs[::-1], "seed": LAB_SEED,
            "simulator_pid": simulator_pid, "eye_pid": eye_pid,
        }
        if phase == "ready" and not eye_pid:
            result.update(phase="failed", error="The Eye dashboard process stopped.")
        elif phase == "ready":
            with connect(dsn) as db:
                last = db.execute("select max(start_at) as last from windows").fetchone()["last"]
                value = state(db, "last_window")
                checked = datetime.fromisoformat(value) if value else None
                for run in runs:
                    if run["scenario"] and run.get("start_at"):
                        run["evaluation"] = evaluate(db, run, checked)
            upcoming = last + HOUR
            result.update(eye_url=f"http://127.0.0.1:{self.eye_port}/", next_window=upcoming,
                          pattern_day=traffic_cycle(upcoming)[1] + 1, eye_through=checked)
        return result

    def run_workflow(self, identifier):
        with self.lock:
            selected = next((run.copy() for run in self.runs if run["id"] == identifier and run["scenario"]), None)
            dsn = self.dsn
        if not selected:
            raise HTTPException(404, "No scenario run found.")
        with connect(dsn) as db:
            return workflow_readings(db, selected)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1500)


class NormalRequest(BaseModel):
    hours: int = Field(ge=1, le=48)


def make_lab_app(eye_port=8000):
    lab = Lab(configured_devin(), eye_port)

    @asynccontextmanager
    async def lifespan(app):
        boot = Thread(target=lab.boot, daemon=True)
        boot.start()
        try:
            yield
        finally:
            lab.close(boot)

    app = FastAPI(title="Eye Test Lab", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    assets = Path(__file__).with_name("lab_static")
    app.mount("/static", StaticFiles(directory=assets), name="lab-assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(assets / "index.html")

    @app.get("/benchmark", include_in_schema=False)
    def benchmark_page():
        return FileResponse(assets / "benchmark.html")

    @app.get("/api/benchmarks")
    def benchmark_scoreboard():
        return {"checkout": benchmark.checkout(), "runs": benchmark.scoreboard()}

    @app.post("/api/benchmarks", status_code=202, dependencies=[Depends(local_action)])
    def start_benchmark():
        try:
            return {"id": benchmark.start()}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/benchmarks/commits/{sha}", response_class=PlainTextResponse)
    def get_benchmark_commit(sha: str):
        if commit := benchmark.read_commit(sha):
            return commit
        raise HTTPException(404, "Commit unavailable in this checkout.")

    @app.get("/api/benchmarks/{identifier}")
    def get_benchmark_run(identifier: str):
        if run := benchmark.read_run(identifier):
            return run
        raise HTTPException(404, "Benchmark run not found.")

    @app.get("/theme.css", include_in_schema=False)
    def theme():
        return FileResponse(Path(__file__).with_name("static") / "theme.css")

    @app.get("/theme.js", include_in_schema=False)
    def theme_script():
        return FileResponse(Path(__file__).with_name("static") / "theme.js", media_type="text/javascript")

    @app.get("/api/lab")
    def snapshot():
        return lab.snapshot()

    @app.get("/api/runs/{identifier}/workflow")
    def run_workflow(identifier: str):
        return lab.run_workflow(identifier)

    @app.post("/api/chat", status_code=202, dependencies=[Depends(local_action)])
    def chat(request: ChatRequest):
        if not request.prompt.strip():
            raise HTTPException(422, "Describe a scenario first.")
        return {"id": lab.ask(request.prompt.strip())}

    @app.post("/api/normal", status_code=202, dependencies=[Depends(local_action)])
    def normal(request: NormalRequest):
        return {"id": lab.queue(hours=request.hours)}

    @app.post("/api/plans/{identifier}/apply", status_code=202, dependencies=[Depends(local_action)])
    def apply(identifier: str):
        return {"id": lab.apply(identifier)}

    @app.post("/api/replay", status_code=202, dependencies=[Depends(local_action)])
    def replay(scenario: Scenario):
        return {"id": lab.import_plan(scenario)}

    return app
