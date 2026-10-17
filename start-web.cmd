@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto error
)
if not exist ".venv\ready" (
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto error
  echo ready> .venv\ready
)
start "" ".venv\Scripts\pythonw.exe" "web_start.py" --hosted %*
exit /b
:error
echo Startup failed. Install Python 3.10 or newer, then try again.
pause
