@echo off
REM FT News Monitor updater (Git-based, with dependency refresh).
REM Pulls the latest code from GitHub. Your database, .env, home markets,
REM starred articles, and golden-set labels are untouched — they live in
REM %LOCALAPPDATA%\FTNewsMonitor\ (ZIP layout) or <repo>\data\ (git-clone
REM layout) and are outside the fetched code.
REM
REM Pre-conditions: git on PATH, the extracted folder is a git clone of
REM the FT News Monitor repository (not a plain ZIP). If you got the tool
REM as a ZIP, ask Jordan for a new ZIP release instead.
REM
REM v0.7.0 addition: if requirements.txt changed after the pull, the
REM script re-runs `venv\Scripts\pip install -r requirements.txt` so the
REM colleague doesn't hit ModuleNotFoundError on next start.bat.
setlocal
cd /d "%~dp0"

REM Suppress the credential prompt window that pops up when git can't
REM auth (public repo doesn't need it; setting this avoids a scary popup).
set GIT_TERMINAL_PROMPT=0

where git >nul 2>&1
if errorlevel 1 (
  echo Git is not installed on this laptop.
  echo Please contact Jordan for a new ZIP release.
  pause
  exit /b 1
)

if not exist ".git" (
  echo This folder is not a git clone — it looks like an extracted ZIP.
  echo Please contact Jordan for a new ZIP release, or set up a git clone
  echo per docs\UPGRADE.md.
  pause
  exit /b 1
)

REM Snapshot requirements.txt hash BEFORE the pull so we can detect a
REM change and refresh the venv only when needed. certutil is native to
REM Windows — no PowerShell dependency.
set "REQ_BEFORE=%TEMP%\ftnm-req-before.txt"
set "REQ_AFTER=%TEMP%\ftnm-req-after.txt"
if exist "requirements.txt" (
  certutil -hashfile "requirements.txt" SHA256 > "%REQ_BEFORE%" 2>nul
)

echo Fetching latest version from GitHub...
REM Capture stderr to a scratch file so we can pattern-match on the
REM failure mode and give the user a targeted hint.
set "UPDATE_ERR=%TEMP%\ftnewsmonitor-update-err.txt"
git pull --ff-only 2> "%UPDATE_ERR%"
set "PULL_RC=%ERRORLEVEL%"
if "%PULL_RC%"=="0" goto :ok

echo.
echo Update failed. Details:
type "%UPDATE_ERR%"
echo.

REM findstr /c: matches a literal substring, case-insensitive with /i.
findstr /i /c:"could not resolve host" /c:"unable to access" /c:"connection refused" "%UPDATE_ERR%" >nul
if not errorlevel 1 (
  echo Hint: this looks like a network problem — VPN off, or the
  echo       corporate proxy is blocking github.com. Reconnect to
  echo       VPN and try again, or contact IT.
  goto :fail
)

findstr /i /c:"ssl certificate" /c:"tls" /c:"unable to get local issuer" "%UPDATE_ERR%" >nul
if not errorlevel 1 (
  echo Hint: your corporate proxy is intercepting HTTPS. Ask IT to run:
  echo       git config --global http.sslBackend schannel
  echo       or set the corporate CA in Git^^^^'s trust store.
  goto :fail
)

findstr /i /c:"authentication failed" /c:"could not read username" /c:"terminal prompts disabled" "%UPDATE_ERR%" >nul
if not errorlevel 1 (
  echo Hint: Git needs credentials for a private repo. If the repo is
  echo       supposed to be public, contact Jordan. If not, run
  echo       'git config credential.helper manager' once and pull again.
  goto :fail
)

findstr /i /c:"not possible to fast-forward" /c:"non-fast-forward" /c:"divergent branches" "%UPDATE_ERR%" >nul
if not errorlevel 1 (
  echo Hint: local changes to tracked files are blocking the pull.
  echo       Contact Jordan — do not delete anything yourself.
  goto :fail
)

findstr /i /c:"index.lock" /c:"another git process" "%UPDATE_ERR%" >nul
if not errorlevel 1 (
  echo Hint: another Git command is running (or crashed earlier).
  echo       Close any open Git windows and try again in 30 seconds.
  goto :fail
)

echo Hint: unknown failure. Please copy the error above and send it to Jordan.
goto :fail

:ok
del "%UPDATE_ERR%" >nul 2>&1

REM ------- Dependency refresh: only if requirements.txt actually changed
if not exist "requirements.txt" goto :done
if not exist "%REQ_BEFORE%"      goto :done

certutil -hashfile "requirements.txt" SHA256 > "%REQ_AFTER%" 2>nul
fc /b "%REQ_BEFORE%" "%REQ_AFTER%" >nul 2>&1
if not errorlevel 1 goto :done

echo.
echo requirements.txt changed — refreshing Python dependencies...
if not exist "venv\Scripts\pip.exe" (
  echo   No venv found next to update.bat — skipping dependency refresh.
  echo   If this is a git-clone install, run once from this folder:
  echo     py -3.13 -m venv venv
  echo     venv\Scripts\pip.exe install -r requirements.txt
  goto :done
)

"venv\Scripts\pip.exe" install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo Dependency refresh failed. Try manually:
  echo   venv\Scripts\pip.exe install -r requirements.txt
  goto :fail
)

:done
del "%REQ_BEFORE%" >nul 2>&1
del "%REQ_AFTER%"  >nul 2>&1
echo.
echo Update complete. You can now double-click start.bat to launch.
pause
endlocal
exit /b 0

:fail
del "%UPDATE_ERR%" >nul 2>&1
del "%REQ_BEFORE%" >nul 2>&1
del "%REQ_AFTER%"  >nul 2>&1
pause
endlocal
exit /b 1
