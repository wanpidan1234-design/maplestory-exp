@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Python environment missing. Run start.cmd once to install dependencies.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "quiz_helper.py"
