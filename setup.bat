@echo off
setlocal
cd /d "%~dp0"
echo.
echo  CheatVision setup
echo  =================
echo  First-time install: creates .venv, installs app packages, VOD OCR,
echo  training extras, ffmpeg if missing, and the optional player detector.
echo  For later GitHub updates, use update.bat.
echo  Afterwards start the app with run.bat, or the VOD inbox with watch_vod_inbox.bat.
echo.

rem ---- 1. Python -------------------------------------------------------------
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (
  python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [!!] Python was not found.
  echo      Install Python 3.11 or newer from https://www.python.org/downloads/
  echo      and tick "Add python.exe to PATH" in the installer. Then run setup.bat again.
  pause
  exit /b 1
)
for /f "tokens=*" %%v in ('%PY% --version 2^>^&1') do echo [OK] %%v

rem ---- 2. Private environment -----------------------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo [..] Creating .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [!!] Could not create .venv. Is this folder writable?
    pause
    exit /b 1
  )
)
set "VPY=.venv\Scripts\python.exe"

rem ---- 3. Packages the app, VOD clipper, and trainer need --------------------
echo [..] Installing packages. A few minutes the first time.
"%VPY%" -m pip install --upgrade pip >nul 2>nul
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [!!] pip install failed. Check your internet connection and run setup.bat again.
  pause
  exit /b 1
)
echo [..] Installing training extras (needed for the player detector and offline VOD training) ...
"%VPY%" -m pip install -r requirements-train.txt
if errorlevel 1 (
  echo [!!] Training extras failed to install. The live app can still run. Re-run setup.bat to retry.
)

rem ---- 4. ffmpeg ---------------------------------------------------------------
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [..] ffmpeg is not installed. Trying winget ...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  echo      Note: a freshly installed ffmpeg is only visible to NEW terminal windows.
  echo      When setup finishes, close this window and double-click run.bat.
)

rem ---- 5. GPU for the detector --------------------------------------------------
echo.
echo  The player detector can run on your graphics card through DirectML.
echo  Any DirectX 12 GPU works. On the development PC it is 3 to 8x faster than the CPU.
set "GPU=Y"
set /p "GPU=Run the player detector on the graphics card? [Y/n] "
if /i not "%GPU%"=="N" (
  "%VPY%" -m pip uninstall -y onnxruntime >nul 2>nul
  "%VPY%" -m pip install onnxruntime-directml
)

rem ---- 6. VOD OCR without replacing the ONNX runtime --------------------------
echo [..] Installing VOD clip OCR (--no-deps so DirectML / CPU ONNX Runtime stays put) ...
"%VPY%" -m pip install --no-deps -r requirements-vod-ocr.txt
if errorlevel 1 (
  echo [!!] VOD OCR failed to install. Re-run setup.bat to retry. The live app can still run.
)

rem ---- 7. Player detector ------------------------------------------------------
echo.
echo  The player detector lets flags be confirmed against a player box.
echo  It downloads about 300 MB once. The app runs without it, aim motion only.
set "ANSWER=Y"
set /p "ANSWER=Install the player detector now? [Y/n] "
echo.
if /i "%ANSWER%"=="N" (
  "%VPY%" tools\setup_check.py
) else (
  "%VPY%" tools\setup_check.py --get-model
)

echo.
echo Setup finished.
echo   Live app:      double-click run.bat
echo   VOD tester:    double-click watch_vod_inbox.bat
echo   Later updates: double-click update.bat
pause
