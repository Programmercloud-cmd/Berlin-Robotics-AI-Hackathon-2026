@echo off
REM Sentinel demo 1 - ARCHITECTURE VIEW: the agent's components light up while the recorded run replays.
REM Double-click, wait for the browser, start the screen recorder, then press F5 to replay from the start (about 2 min).
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist "state\demo\events.jsonl" (
  echo Recording the agent run once ^(about 1 minute^) ...
  python -m sentinel.demo record
)
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8765/"
python -m sentinel.demo play --port 8765 %*
