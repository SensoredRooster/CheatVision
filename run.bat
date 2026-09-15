@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo CheatVision is not set up yet. Double-click setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py
if errorlevel 1 (
  echo.
  echo CheatVision stopped with an error. Run: .venv\Scripts\python.exe tools\setup_check.py
  pause
)
