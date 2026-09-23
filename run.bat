@echo off
cd /d "%~dp0"
set "CHEATVISION_LOG_DIR=%LOCALAPPDATA%\CheatVision\logs"
if not exist "%CHEATVISION_LOG_DIR%" mkdir "%CHEATVISION_LOG_DIR%" >nul 2>&1
set "CHEATVISION_LAUNCH_LOG=%CHEATVISION_LOG_DIR%\launcher.log"
echo [%date% %time%] CheatVision launcher started>>"%CHEATVISION_LAUNCH_LOG%"
if not exist ".venv\Scripts\python.exe" (
  echo CheatVision is not set up yet. Double-click setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py 2>>"%CHEATVISION_LAUNCH_LOG%"
if errorlevel 1 (
  echo.
  echo CheatVision stopped with an error. Run: .venv\Scripts\python.exe tools\setup_check.py
  pause
)

echo [%date% %time%] CheatVision launcher finished>>"%CHEATVISION_LAUNCH_LOG%"
