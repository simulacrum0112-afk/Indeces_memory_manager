@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.cmd first to create the local Python environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m indeces knowledge
if errorlevel 1 pause
