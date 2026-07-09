@echo off
REM FT News Monitor launcher — 3-layout auto-detect cascade.
REM
REM Layout 1 (Packaged ZIP): python\python.exe next to start.bat.
REM   Uses embedded 3.13. Sets FM_DATA_DIR=%LOCALAPPDATA%\FTNewsMonitor
REM   so the app writes data outside the extract folder (Program Files-safe).
REM Layout 2 (Git-clone install, v0.7.0 default): venv\Scripts\python.exe
REM   next to start.bat. Does NOT set FM_DATA_DIR — app/config.py falls to
REM   the dev layout (<repo>\data\, .env next to start.bat).
REM Layout 3 (Dev fallback): system Python 3.13 via `py -3.13`. Same
REM   dev-layout behavior as Layout 2.
REM
REM Layout precedence matters: if a v0.7.0 ZIP is ever extracted over
REM a stale venv, Layout 1 must win because its embedded 3.13 is pinned.
setlocal
cd /d "%~dp0"

REM ----- Layout 1: packaged ZIP -----------------------------------------
if exist "python\python.exe" (
  if not defined FM_DATA_DIR set "FM_DATA_DIR=%LOCALAPPDATA%\FTNewsMonitor"
  "python\python.exe" scripts\launch.py
  goto :after_launch
)

REM ----- Layout 2: git-clone install with venv --------------------------
if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" scripts\launch.py
  goto :after_launch
)

REM ----- Layout 3: system Python 3.13 (dev fallback) --------------------
where py >nul 2>&1
if not errorlevel 1 (
  py -3.13 -c "import sys; sys.exit(0)" >nul 2>&1
  if not errorlevel 1 (
    py -3.13 scripts\launch.py
    goto :after_launch
  )
)

REM ----- No interpreter found -------------------------------------------
echo.
echo FT News Monitor: no Python interpreter found in this folder.
echo.
echo Expected one of:
echo   1. python\python.exe        (packaged ZIP release)
echo   2. venv\Scripts\python.exe  (git-clone install)
echo   3. py -3.13 on PATH         (dev machine)
echo.
echo If this is a fresh git clone, run these once from this folder:
echo   py -3.13 -m venv venv
echo   venv\Scripts\pip.exe install --upgrade pip
echo   venv\Scripts\pip.exe install -r requirements.txt
echo.
echo Then double-click start.bat again. If none of that works, contact Jordan.
pause
endlocal
exit /b 1

:after_launch
REM launch.py returns here either after a clean shutdown OR because the
REM single-instance guard aborted (another server is already running).
REM Either way we want the terminal to stay open so the user sees WHY
REM the window is closing — a silent flash-close is the #1 UX complaint.
echo.
if errorlevel 1 (
  echo FT News Monitor exited with an error.
) else (
  echo FT News Monitor exited. See the last few lines above for the reason.
  echo Common cause: another instance is already running — check the taskbar.
)
if defined FM_DATA_DIR (
  echo Log: %FM_DATA_DIR%\server.log
) else (
  echo Log: %~dp0data\server.log
)
pause
endlocal
