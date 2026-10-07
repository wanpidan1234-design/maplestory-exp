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
.venv\Scripts\pythonw.exe orb.py
if errorlevel 1 goto error
exit /b
:error
echo Startup failed. Please keep this window for the error details.
pause
