@echo off
REM ============================================================================
REM  FT News Monitor - install.bat  (v0.7.6.2)
REM
REM  Non-AI installer for colleagues who don't have Claude Code available.
REM  Two modes:
REM    B (Bedrock) - default, uses AWS SSO / static keys on this laptop.
REM    C (Cowork)  - scoring runs in Claude Cowork, no AWS needed.
REM
REM  Mirrors the AGENTS.md sections B-G in Bedrock mode. In Cowork mode
REM  the AWS discovery step is replaced by a token-generation step and
REM  the ftnews skill is left in place under the tool folder for
REM  import into Cowork.
REM
REM  Distribution: send this single file via Teams. Colleague drops it in
REM  a fresh empty folder under %USERPROFILE%\Documents\ and double-clicks.
REM  Do NOT put it in OneDrive or SharePoint (SQLite fails silently there).
REM
REM  Logs: %TEMP%\ftnm-install.log  (redacted, no secrets)
REM  Runtime: 3-15 min depending on what's already installed.
REM ============================================================================

setlocal ENABLEDELAYEDEXPANSION

set "INSTALL_LOG=%TEMP%\ftnm-install.log"
set "BOOTSTRAP_DIR=%USERPROFILE%\FTNewsMonitor-bootstrap"
set "TARGET_DIR=%USERPROFILE%\FTNewsMonitor"
set "TOOLS_DIR=%USERPROFILE%\FTNewsMonitor-tools"
set "REPO_URL=https://github.com/jordanthekern-lang/ft-news-monitor.git"
set "INSTALL_MODE="

REM Fresh log file each run so the colleague can screenshot just the latest.
echo === FT News Monitor install %DATE% %TIME% === > "%INSTALL_LOG%"

echo.
echo FT News Monitor - Installer v0.7.6.2
echo -----------------------------------
echo.
echo Log:    %INSTALL_LOG%
echo Target: %TARGET_DIR%
echo.
echo Which AI provider does this laptop use?
echo   [B] Bedrock  - AWS SSO / static keys on this laptop (default for Jordan)
echo   [C] Cowork   - Claude Cowork does the ranking; no AWS needed
echo.
set /p INSTALL_MODE="Choice (B/C, Enter for B): "
if /i "%INSTALL_MODE%"=="" set "INSTALL_MODE=B"
if /i not "%INSTALL_MODE%"=="B" if /i not "%INSTALL_MODE%"=="C" (
  echo   ERROR: Please answer B or C.
  goto :FAIL
)
echo   mode = %INSTALL_MODE% >> "%INSTALL_LOG%"
echo.

REM ============================================================================
REM  Phase 1: Preflight (AGENTS.md B.0-B.5)
REM ============================================================================

echo [1/6] Preflight checks...
echo === Phase 1: preflight === >> "%INSTALL_LOG%"

REM  B.0 - bootstrap workspace
if not exist "%BOOTSTRAP_DIR%" mkdir "%BOOTSTRAP_DIR%" 2>>"%INSTALL_LOG%"
if errorlevel 1 (
  echo   ERROR: Cannot create %BOOTSTRAP_DIR%
  echo   Please create it manually in Explorer, then re-run install.bat.
  goto :FAIL
)

REM  B.2 - reject sync'd install target
echo %USERPROFILE% | findstr /i "OneDrive SharePoint \"Program Files\"" >nul
if not errorlevel 1 (
  echo   ERROR: Your Windows profile is inside a sync'd location:
  echo     %USERPROFILE%
  echo   SQLite will corrupt there. Please contact Jordan.
  goto :FAIL
)

REM  B.3 - detect legacy ZIP install
if exist "%LOCALAPPDATA%\FTNewsMonitor\monitor.db" (
  echo   WARNING: An older ZIP-based FT News Monitor install was detected at:
  echo     %LOCALAPPDATA%\FTNewsMonitor\
  echo   This installer will set up the newer git-clone version SIDE-BY-SIDE
  echo   at %TARGET_DIR%. The old install stays untouched.
  echo   Contact Jordan afterwards if you want to migrate your history.
  echo.
)

echo   ok
echo   ok >> "%INSTALL_LOG%"

REM ============================================================================
REM  Phase 2: Git (AGENTS.md C)
REM ============================================================================

echo [2/6] Checking Git...
echo === Phase 2: git === >> "%INSTALL_LOG%"

set "GIT_EXE=git"
where git >nul 2>&1
if not errorlevel 1 (
  echo   ok (already installed)
  git --version >> "%INSTALL_LOG%" 2>&1
  goto :PHASE_PYTHON
)

echo   not found - attempting winget install...
powershell -NoProfile -Command "winget install --id Git.Git -e --scope user --accept-package-agreements --accept-source-agreements" >> "%INSTALL_LOG%" 2>&1
set "WINGET_RC=%ERRORLEVEL%"

REM  winget may install successfully but PATH isn't refreshed for THIS
REM  process. Poke the standard install location before falling back to
REM  the portable path.
if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" (
  set "GIT_EXE=%LOCALAPPDATA%\Programs\Git\cmd\git.exe"
  echo   ok (winget install detected)
  goto :PHASE_PYTHON
)
if exist "%ProgramFiles%\Git\cmd\git.exe" (
  set "GIT_EXE=%ProgramFiles%\Git\cmd\git.exe"
  echo   ok (winget install detected)
  goto :PHASE_PYTHON
)

echo   winget failed (exit %WINGET_RC%) - trying portable fallback...
if not exist "%TOOLS_DIR%" mkdir "%TOOLS_DIR%"
powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/PortableGit-2.47.1-64-bit.7z.exe' -OutFile '%TOOLS_DIR%\PortableGit.7z.exe'; Unblock-File '%TOOLS_DIR%\PortableGit.7z.exe'" >> "%INSTALL_LOG%" 2>&1
if not exist "%TOOLS_DIR%\PortableGit.7z.exe" (
  echo   ERROR: Could not install Git via winget OR portable download.
  echo   Both channels are blocked. Please contact IT to install Git for
  echo   Windows from git-scm.com/download/win.
  goto :FAIL
)
"%TOOLS_DIR%\PortableGit.7z.exe" -o"%TOOLS_DIR%\PortableGit" -y >> "%INSTALL_LOG%" 2>&1
if not exist "%TOOLS_DIR%\PortableGit\cmd\git.exe" (
  echo   ERROR: Portable Git extraction failed. See %INSTALL_LOG%.
  goto :FAIL
)
set "GIT_EXE=%TOOLS_DIR%\PortableGit\cmd\git.exe"
echo   ok (portable install at %TOOLS_DIR%\PortableGit)

:PHASE_PYTHON

REM ============================================================================
REM  Phase 3: Python 3.13 (AGENTS.md D)
REM ============================================================================

echo [3/6] Checking Python 3.13...
echo === Phase 3: python === >> "%INSTALL_LOG%"

set "PY_EXE="
py -3.13 --version >nul 2>&1
if not errorlevel 1 (
  set "PY_EXE=py -3.13"
  echo   ok (already installed)
  py -3.13 --version >> "%INSTALL_LOG%" 2>&1
  goto :PHASE_CLONE
)

echo   not found - attempting winget install...
powershell -NoProfile -Command "winget install --id Python.Python.3.13 -e --scope user --accept-package-agreements --accept-source-agreements" >> "%INSTALL_LOG%" 2>&1
set "WINGET_RC=%ERRORLEVEL%"

REM  Try to detect the winget install directly (Python often lands under
REM  %LOCALAPPDATA%\Programs\Python\Python313 for --scope user).
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
  set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  echo   ok (winget install detected)
  goto :PHASE_CLONE
)

py -3.13 --version >nul 2>&1
if not errorlevel 1 (
  set "PY_EXE=py -3.13"
  echo   ok (winget install visible via py launcher)
  goto :PHASE_CLONE
)

echo   winget failed (exit %WINGET_RC%) - IT install is required.
echo   The embeddable Python 3.13 does NOT support venv creation, which
echo   this installer needs. Please forward this to IT:
echo     "Please install Python 3.13 for the FlixBus News Monitor tool.
echo      Download: https://www.python.org/downloads/release/python-3135/"
goto :FAIL

:PHASE_CLONE

REM ============================================================================
REM  Phase 4: Clone repo (AGENTS.md E.1-E.2)
REM ============================================================================

echo [4/6] Cloning repository...
echo === Phase 4: clone === >> "%INSTALL_LOG%"

if exist "%TARGET_DIR%\.git" (
  echo   existing clone detected - running git pull --ff-only
  cd /d "%TARGET_DIR%"
  "%GIT_EXE%" pull --ff-only >> "%INSTALL_LOG%" 2>&1
  if errorlevel 1 (
    echo   git pull failed - see %INSTALL_LOG%
    goto :FAIL
  )
  echo   ok (updated)
  goto :PHASE_VENV
)

REM Target directory may exist but be empty (Explorer sometimes leaves
REM a phantom folder behind after a rename, or a previous install.bat
REM run bailed out mid-way). Check whether it has any files; if not,
REM remove it silently so git clone can proceed. Only refuse when there
REM is real content that isn't a git clone.
if exist "%TARGET_DIR%" (
  dir /a /b "%TARGET_DIR%" 2>nul | findstr /r "." >nul
  if errorlevel 1 (
    echo   removing empty %TARGET_DIR% left over from a previous run
    rmdir /q "%TARGET_DIR%" >> "%INSTALL_LOG%" 2>&1
  ) else (
    echo   ERROR: %TARGET_DIR% exists and has content but is not a git clone.
    echo   Rename it in Explorer to FTNewsMonitor-old and re-run install.bat.
    goto :FAIL
  )
)

"%GIT_EXE%" clone %REPO_URL% "%TARGET_DIR%" >> "%INSTALL_LOG%" 2>&1
if errorlevel 1 (
  REM Common corporate-proxy TLS failure - try schannel backend once.
  echo   clone failed - retrying with Windows cert store backend...
  "%GIT_EXE%" config --global http.sslBackend schannel >> "%INSTALL_LOG%" 2>&1
  "%GIT_EXE%" clone %REPO_URL% "%TARGET_DIR%" >> "%INSTALL_LOG%" 2>&1
  if errorlevel 1 (
    echo   ERROR: git clone failed. See %INSTALL_LOG%.
    echo   Hint: check VPN / corporate proxy settings for github.com access.
    goto :FAIL
  )
)
echo   ok

:PHASE_VENV

REM ============================================================================
REM  Phase 5: venv + pip install (AGENTS.md E.3-E.5)
REM ============================================================================

echo [5/6] Creating venv and installing Python dependencies...
echo === Phase 5: venv === >> "%INSTALL_LOG%"
echo   (this takes 2-5 minutes, please wait)

cd /d "%TARGET_DIR%"

if not exist "venv\Scripts\python.exe" (
  %PY_EXE% -m venv venv >> "%INSTALL_LOG%" 2>&1
  if errorlevel 1 (
    echo   ERROR: venv creation failed. See %INSTALL_LOG%.
    goto :FAIL
  )
)

"venv\Scripts\pip.exe" install --disable-pip-version-check --upgrade pip >> "%INSTALL_LOG%" 2>&1
"venv\Scripts\pip.exe" install --disable-pip-version-check -r requirements.txt >> "%INSTALL_LOG%" 2>&1
if errorlevel 1 (
  echo   ERROR: pip install failed. See %INSTALL_LOG%.
  goto :FAIL
)

"venv\Scripts\python.exe" -c "import fastapi, uvicorn, anthropic, apscheduler, pywintypes; print('deps ok')" >> "%INSTALL_LOG%" 2>&1
if errorlevel 1 (
  echo   ERROR: dependency sanity check failed. See %INSTALL_LOG%.
  goto :FAIL
)
echo   ok

REM ============================================================================
REM  Phase 6: Provider configuration (AGENTS.md F)
REM ============================================================================

if /i "%INSTALL_MODE%"=="C" goto :PHASE_COWORK

echo [6/6] Discovering AWS credentials and writing .env...
echo === Phase 6: discover_aws === >> "%INSTALL_LOG%"

"venv\Scripts\python.exe" scripts\discover_aws.py --write-env
set "DISCOVERY_RC=%ERRORLEVEL%"

if "%DISCOVERY_RC%"=="0" (
  echo   ok
  goto :DONE_BEDROCK
)
if "%DISCOVERY_RC%"=="2" (
  echo.
  echo   No AWS credentials found on this laptop.
  echo   Please contact Jordan - he will send you an Access Key
  echo   via a secure channel (password manager, not email).
  echo.
  echo   Everything else is already installed. Once you have the keys,
  echo   you can either:
  echo     a) put them in your Windows environment ^(AWS_ACCESS_KEY_ID
  echo        and AWS_SECRET_ACCESS_KEY^) and re-run install.bat, OR
  echo     b) open %TARGET_DIR%\.env in Notepad and paste them there
  echo        under AWS_ACCESS_KEY_ID= and AWS_SECRET_ACCESS_KEY=.
  goto :FAIL
)

echo   ERROR: discover_aws.py returned exit code %DISCOVERY_RC%.
echo   See the console output above for details.
goto :FAIL

:PHASE_COWORK
echo [6/6] Configuring Cowork mode (AI_PROVIDER=cowork)...
echo === Phase 6: cowork setup === >> "%INSTALL_LOG%"

"venv\Scripts\python.exe" scripts\configure_cowork.py
set "COWORK_RC=%ERRORLEVEL%"
if not "%COWORK_RC%"=="0" (
  echo   ERROR: configure_cowork.py returned exit code %COWORK_RC%.
  echo   See %INSTALL_LOG% for details.
  goto :FAIL
)
echo   ok
goto :DONE_COWORK

REM ============================================================================
:DONE_BEDROCK
echo.
echo ==================================================================
echo   Installation complete (Bedrock mode).
echo.
echo   Next steps:
echo     1. Double-click:  %TARGET_DIR%\start.bat
echo     2. Your browser opens at http://127.0.0.1:8766/setup
echo     3. Wizard step 2 (AWS) should be pre-filled and green.
echo     4. Complete the remaining wizard steps in your browser.
echo.
echo   To update later:  double-click %TARGET_DIR%\update.bat
echo ==================================================================
echo.
pause
endlocal
exit /b 0

:DONE_COWORK
echo.
echo ==================================================================
echo   Installation complete (Cowork mode).
echo.
echo   The tool will NOT score articles by itself. Instead, your
echo   Claude Cowork will do it via the ftnews skill.
echo.
echo   Autostart:  A Startup shortcut has been installed. From your
echo               next Windows login the tool will start automatically
echo               and Cowork can talk to it at any time.
echo.
echo   Next steps:
echo     1. Double-click:  %TARGET_DIR%\start.bat
echo        Your browser opens at http://127.0.0.1:8766/setup
echo        The AWS credentials step is already green (external ranking).
echo     2. Complete the remaining wizard steps in your browser.
echo     3. In Cowork:
echo        - Add %TARGET_DIR%\data as a Trusted Folder.
echo        - Import the ftnews skill from:
echo          %TARGET_DIR%\skills\ftnews\ftnews.zip
echo     4. Ask Cowork: "News-Ranking laufen lassen"
echo        (or set up a weekly scheduled task in Cowork).
echo.
echo   Full guide:  %TARGET_DIR%\docs\QUICKSTART_COWORK.md
echo   To update:   double-click %TARGET_DIR%\update.bat
echo ==================================================================
echo.
pause
endlocal
exit /b 0

:FAIL
echo.
echo Installation did NOT complete.
echo Log file: %INSTALL_LOG%
echo Please forward the log to Jordan if you need help.
echo.
pause
endlocal
exit /b 1
