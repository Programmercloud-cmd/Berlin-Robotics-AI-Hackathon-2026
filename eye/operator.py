"""Local operator controls for the test lab."""

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


class LabLauncher:
    def __init__(self):
        self.lock = Lock()
        self.process = None
        self.phase = "idle"

    def status(self):
        with self.lock:
            phase = self.phase
            exit_code = self.process.poll() if self.process else None
            if exit_code is not None:
                phase = "failed"
            elif phase == "dashboard" and port_open(8000):
                phase = "ready"
            lab_ready = self.process is not None and exit_code is None and port_open(8002)
            return {"phase": phase, "exit_code": exit_code, "eye_url": EYE_URL if phase == "ready" else None,
                    "lab_url": LAB_URL if lab_ready else None}

    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                if self.phase != "failed":
                    return
                self._stop()
            if port_open(8002):
                raise HTTPException(409, "Port 8002 is in use. Open or stop the existing test lab.")
            if port_open(8000):
                raise HTTPException(409, "Port 8000 is in use. Stop the existing dashboard first.")
            self.phase = "starting"
            self.process = subprocess.Popen(
                [sys.executable, "-m", "eye", "lab"],
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
                raise HTTPException(409, "The test lab is still shutting down.") from None

    def close(self):
        with self.lock:
            self._stop()


def make_operator_app():
    launcher = LabLauncher()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            launcher.close()

    app = FastAPI(title="Eye of the System Operator", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(Path(__file__).with_name("operator.html"))

    @app.get("/theme.css", include_in_schema=False)
    def theme():
        return FileResponse(Path(__file__).with_name("static") / "theme.css")

    @app.get("/theme.js", include_in_schema=False)
    def theme_script():
        return FileResponse(Path(__file__).with_name("static") / "theme.js", media_type="text/javascript")

    @app.get("/api/test-lab")
    def status():
        return launcher.status()

    @app.post("/api/test-lab", status_code=202, dependencies=[Depends(local_action)])
    def start_lab():
        launcher.start()
        return launcher.status()

    return app
