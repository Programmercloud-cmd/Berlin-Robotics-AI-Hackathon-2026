@echo off
REM Sentinel demo 2 - WORKFLOW VIEW: every hour is one execution of the agent's workflow; nodes light up step by step.
REM Double-click, wait for the browser, start the screen recorder, then click "Play story" (or pick an execution).
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist "state\demo\events.jsonl" (
  echo Recording the agent run once ^(about 1 minute^) ...
  python -m sentinel.demo record
)
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8766/workflow"
python -m sentinel.demo play --port 8766 %*
