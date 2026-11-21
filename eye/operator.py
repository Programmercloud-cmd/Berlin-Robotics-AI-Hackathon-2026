"""Local operator controls for the disposable PostgreSQL payment rehearsal."""

import logging
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock, Thread

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from eye.lab import EYE_URL, local_action, port_open

log = logging.getLogger(__name__)
PHASES = {"postgres", "baseline", "discovery", "dashboard", "failed"}
LAB_URL = "http://127.0.0.1:8002/"


class Rehearsal:
    def __init__(self):
        self.lock = Lock()
        self.process = None
        self.kind = None
        self.phase = "idle"

    def status(self):
        with self.lock:
            phase = self.phase
            exit_code = self.process.poll() if self.process else None
            if exit_code is not None:
                phase = "failed"
            elif phase == "dashboard" and port_open(8000):
                phase = "ready"
            lab_ready = self.kind == "lab" and exit_code is None and port_open(8002)
            return {"phase": phase, "exit_code": exit_code, "eye_url": EYE_URL if phase == "ready" else None,
                    "lab_url": LAB_URL if lab_ready else None}

    def start(self, kind):
        with self.lock:
            if kind == "lab" and self.kind != "lab" and port_open(8002):
                raise HTTPException(409, "Port 8002 is in use. Stop the existing test lab first.")
            if self.process and self.process.poll() is None:
                if self.phase not in {"dashboard", "failed"}:
                    raise HTTPException(409, "A rehearsal is starting.")
                self._stop()
            if port_open(8000):
                raise HTTPException(409, "Port 8000 is in use. Stop the existing dashboard first.")
            if kind == "lab" and port_open(8002):
                raise HTTPException(409, "Port 8002 is in use. Stop the existing test lab first.")
            self.phase = "starting"
            self.kind = kind
            command = ["lab"] if kind == "lab" else ["demo", "--local", "--operator-progress"]
            self.process = subprocess.Popen(
                [sys.executable, "-m", "eye", *command],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            Thread(target=self._read_progress, args=(self.process,), daemon=True).start()

    def _read_progress(self, process):
        with process.stdout as output:
            for line in output:
                marker, _, phase = line.strip().partition(" ")
                if marker == "EYE_STARTUP" and phase in PHASES:
                    with self.lock:
                        if self.process is process:
                            self.phase = phase
                else:
                    log.info("%s", line.rstrip())

    def _stop(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                raise HTTPException(409, "The previous rehearsal is still shutting down.") from None

    def close(self):
        with self.lock:
            self._stop()


def make_operator_app():
    rehearsal = Rehearsal()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            rehearsal.close()

    app = FastAPI(title="Eye of the System Operator", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(Path(__file__).with_name("operator.html"))

    @app.get("/theme.css", include_in_schema=False)
    def theme():
        return FileResponse(Path(__file__).with_name("static") / "theme.css")

    @app.get("/api/rehearsal")
    def status():
        return rehearsal.status()

    @app.post("/api/rehearsal", status_code=202, dependencies=[Depends(local_action)])
    def start():
        rehearsal.start("rehearsal")
        return rehearsal.status()

    @app.post("/api/test-lab", status_code=202, dependencies=[Depends(local_action)])
    def start_lab():
        rehearsal.start("lab")
        return rehearsal.status()

    return app
