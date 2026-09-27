@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
echo.
echo  CheatVision update
echo  ==================
echo  Pulls origin/main, then refreshes app packages, training extras, and VOD OCR.
echo  Inbox videos and extracted clips are not in git and are left untouched.
echo  First-time install: use setup.bat instead.
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [!!] CheatVision is not set up yet. Double-click setup.bat first.
  pause
  exit /b 1
)
set "VPY=.venv\Scripts\python.exe"

rem ---- 1. Latest code from GitHub -------------------------------------------
where git >nul 2>nul
if errorlevel 1 (
  echo [!!] Git is not installed. Cannot pull updates.
  echo      Clone https://github.com/SensoredRooster/CheatVision instead of using a ZIP.
  pause
  exit /b 1
)
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
  echo [!!] This folder is not a git clone. Cannot pull updates.
  echo      Clone https://github.com/SensoredRooster/CheatVision so update.bat can pull main.
  pause
  exit /b 1
)

echo [..] Updating from GitHub ...
git fetch origin
if errorlevel 1 (
  echo [!!] git fetch failed. Check your internet connection.
  pause
  exit /b 1
)
set "UPDATED="
git pull --ff-only origin main
if not errorlevel 1 set "UPDATED=main"
if not defined UPDATED (
  git pull --ff-only origin master
  if not errorlevel 1 set "UPDATED=master"
)
if defined UPDATED (
  echo [OK] Repo matches origin/!UPDATED!.
) else (
  echo [!!] Could not fast-forward to origin/main.
  echo      Local commits or file changes may be in the way.
  echo      Inbox videos and extracted clips were not changed.
  pause
  exit /b 1
)

rem ---- 2. Refresh packages ---------------------------------------------------
"%VPY%" -c "import onnxruntime as ort; raise SystemExit(0 if 'DmlExecutionProvider' in ort.get_available_providers() else 1)" >nul 2>nul
set "HAD_DML=%ERRORLEVEL%"

echo [..] Refreshing packages ...
"%VPY%" -m pip install --upgrade pip >nul 2>nul
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [!!] pip install failed. Check your internet connection and run update.bat again.
  pause
  exit /b 1
)
"%VPY%" -m pip install -r requirements-train.txt
if errorlevel 1 (
  echo [!!] Training extras failed to update. The live app can still run. Re-run update.bat to retry.
)

if "%HAD_DML%"=="0" (
  "%VPY%" -m pip uninstall -y onnxruntime >nul 2>nul
  "%VPY%" -m pip install onnxruntime-directml
)

echo [..] Refreshing VOD clip OCR (--no-deps so DirectML / CPU ONNX Runtime stays put) ...
"%VPY%" -m pip install --no-deps -r requirements-vod-ocr.txt
if errorlevel 1 (
  echo [!!] VOD OCR failed to update. Re-run update.bat to retry. The live app can still run.
)

if exist "data\models\yolov8n.onnx" (
  "%VPY%" tools\setup_check.py
) else (
  echo [..] Player detector is missing. Fetching it ...
  "%VPY%" tools\setup_check.py --get-model
)

echo.
echo Update finished.
echo   Live app:   double-click run.bat
echo   VOD tester: double-click watch_vod_inbox.bat
pause
