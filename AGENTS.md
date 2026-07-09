# FT News Monitor — AI Bootstrap Runbook

> **You are an AI coding assistant** (Claude Code, Claude Cowork, or
> similar). A non-technical colleague at FlixBus has placed this file
> in an empty folder and asked you to *"read AGENTS.md and set
> everything up."* Follow this runbook end-to-end. You have full
> permission to run shell commands, download files, and edit files
> under `%USERPROFILE%\FTNewsMonitor\` and `%USERPROFILE%\FTNewsMonitor-tools\`.
> You do NOT have permission to run `start.bat` yourself — that is
> the colleague's job after you finish.

## A. Goal

Install and configure the FT News Monitor so the colleague can:

1. Double-click `%USERPROFILE%\FTNewsMonitor\start.bat`.
2. See the browser open on `http://127.0.0.1:8766/setup`.
3. Land on wizard Step 2 (AWS credentials) with the AWS fields
   **pre-filled and validated green**.

Everything after Step 2 (home markets, seed packs, Outlook, scheduler)
is done by the colleague in the browser. Do NOT attempt those steps.

The install has three moving parts you must handle:

- Prerequisites: Git and Python 3.13 must be on the laptop.
- Repo: `https://github.com/jordanthekern-lang/ft-news-monitor.git`
  cloned into `%USERPROFILE%\FTNewsMonitor`, venv created,
  `requirements.txt` installed.
- Config: `.env` written with valid AWS Bedrock credentials.

---

## B. Preflight — check before doing anything

Run these checks in order. Each check either passes (continue) or
triggers a **HARD STOP** (tell the colleague, do not proceed).

**B.1 Current directory must be empty (or contain only this file).**

```
dir /b
```

If the output shows anything besides `AGENTS.md`, HARD STOP. Message:
*"This folder is not empty. Please move AGENTS.md to a fresh empty
folder and start over."*

**B.2 Current directory must not be inside OneDrive, SharePoint, or Program Files.**

Check `%CD%`. If it contains `OneDrive`, `SharePoint`, `Program Files`,
or `Program Files (x86)` (case-insensitive), HARD STOP. Message:
*"This folder is inside a sync'd or protected location. SQLite will
break here. Please move AGENTS.md to somewhere under `%USERPROFILE%\Documents\`
and start over."*

**B.3 Detect legacy ZIP install.**

If the *parent* folder or current folder contains both `python\python.exe`
and `app\` as siblings of a `start.bat`, this is a v0.6.x ZIP extract.
**Skip C, D, E and jump to Section I (Legacy ZIP fallback).**

**B.4 Git present?**

```
git --version
```

If it prints a version, skip Section C. Otherwise proceed to C.

**B.5 Python 3.13 present?**

```
py -3.13 --version
```

If it prints `Python 3.13.x`, skip Section D. Otherwise proceed to D.
(Fallback: `python --version` — accept only if the reported version
is `3.13.x`.)

---

## C. Git bootstrap

Only run this section if B.4 failed.

**C.1 Try winget first.**

```
powershell -Command "winget install --id Git.Git -e --scope user --accept-package-agreements --accept-source-agreements"
```

Verify:

```
git --version
```

If a new shell is required for PATH to pick up, open a new `cmd.exe`
(or restart your session) and retry `git --version`. If it works, skip
to Section D.

If winget fails with exit code `0x8A15002B` (AppLocker block),
`0x8A150044` (not available), or a similar policy block, proceed to C.2.

**C.2 Portable Git fallback.**

```
mkdir "%USERPROFILE%\FTNewsMonitor-tools"
powershell -Command "Invoke-WebRequest -Uri 'https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/PortableGit-2.47.1-64-bit.7z.exe' -OutFile '%USERPROFILE%\FTNewsMonitor-tools\PortableGit.7z.exe'"
powershell -Command "Unblock-File -Path '%USERPROFILE%\FTNewsMonitor-tools\PortableGit.7z.exe'"
"%USERPROFILE%\FTNewsMonitor-tools\PortableGit.7z.exe" -o"%USERPROFILE%\FTNewsMonitor-tools\PortableGit" -y
```

Verify:

```
"%USERPROFILE%\FTNewsMonitor-tools\PortableGit\cmd\git.exe" --version
```

For the rest of this runbook, if you had to use the portable path,
substitute `"%USERPROFILE%\FTNewsMonitor-tools\PortableGit\cmd\git.exe"`
wherever it says `git`.

**C.3 If both fail — HARD STOP.**

Message to the colleague:
*"I could not install Git on your laptop. Both `winget` and the
portable download were blocked. Please forward this to your IT
department: 'Please install Git for Windows from
git-scm.com/download/win for the FlixBus News Monitor tool.'"*

---

## D. Python 3.13 bootstrap

Only run this section if B.5 failed.

**D.1 Try winget first.**

```
powershell -Command "winget install --id Python.Python.3.13 -e --scope user --accept-package-agreements --accept-source-agreements"
```

Verify (may need a new shell):

```
py -3.13 --version
```

If it works, skip to Section E.

**D.2 Embeddable Python fallback.**

```
mkdir "%USERPROFILE%\FTNewsMonitor-tools\Python313"
powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.13.5/python-3.13.5-embed-amd64.zip' -OutFile '%USERPROFILE%\FTNewsMonitor-tools\python313.zip'"
powershell -Command "Unblock-File -Path '%USERPROFILE%\FTNewsMonitor-tools\python313.zip'"
powershell -Command "Expand-Archive -Path '%USERPROFILE%\FTNewsMonitor-tools\python313.zip' -DestinationPath '%USERPROFILE%\FTNewsMonitor-tools\Python313' -Force"
```

Patch `python313._pth` so `site` module loads (needed for pip):

```
powershell -Command "(Get-Content '%USERPROFILE%\FTNewsMonitor-tools\Python313\python313._pth') -replace '^#import site', 'import site' | Set-Content '%USERPROFILE%\FTNewsMonitor-tools\Python313\python313._pth'"
```

Install pip:

```
powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%USERPROFILE%\FTNewsMonitor-tools\get-pip.py'"
"%USERPROFILE%\FTNewsMonitor-tools\Python313\python.exe" "%USERPROFILE%\FTNewsMonitor-tools\get-pip.py"
```

Verify:

```
"%USERPROFILE%\FTNewsMonitor-tools\Python313\python.exe" -m pip --version
```

For the rest of this runbook, if you had to use the portable path,
substitute `"%USERPROFILE%\FTNewsMonitor-tools\Python313\python.exe"`
wherever it says `python` (and `py -3.13`).

**D.3 If both fail — HARD STOP.**

Message to the colleague:
*"I could not install Python 3.13 on your laptop. Please forward this
to your IT department: 'Please install Python 3.13 from
python.org/downloads for the FlixBus News Monitor tool.'"*

---

## E. Clone repo, create venv, install dependencies

**E.1 Idempotency check.**

If `%USERPROFILE%\FTNewsMonitor` already exists:

- If it contains `.git\` — this is a prior install. Run `git pull --ff-only`
  from inside it and jump to E.4 (venv reuse).
- If it does not contain `.git\` — HARD STOP. Message: *"The folder
  `%USERPROFILE%\FTNewsMonitor` already exists but is not a git clone.
  Please move it aside (rename to `FTNewsMonitor-old`) and start over."*

**E.2 Clone.**

```
git clone https://github.com/jordanthekern-lang/ft-news-monitor.git "%USERPROFILE%\FTNewsMonitor"
```

If this fails with `SSL certificate problem`, `unable to get local
issuer`, or a TLS error, run once:

```
git config --global http.sslBackend schannel
```

Then retry the clone. This tells Git to use the Windows certificate
store, which understands corporate proxy CA certificates.

**E.3 Create venv.**

```
cd /d "%USERPROFILE%\FTNewsMonitor"
py -3.13 -m venv venv
```

**E.4 Install dependencies.** This takes 2–5 minutes.

```
"%USERPROFILE%\FTNewsMonitor\venv\Scripts\pip.exe" install --upgrade pip
"%USERPROFILE%\FTNewsMonitor\venv\Scripts\pip.exe" install -r "%USERPROFILE%\FTNewsMonitor\requirements.txt"
```

**E.5 Verify the venv works.**

```
"%USERPROFILE%\FTNewsMonitor\venv\Scripts\python.exe" -c "import fastapi, uvicorn, anthropic, apscheduler, pywintypes; print('deps ok')"
```

Expected output: `deps ok`. Any ImportError → HARD STOP, tell the
colleague, do not proceed to F.

---

## F. Write `.env` with AWS Bedrock credentials

**F.1 Layout HARD CHECK — determine the correct `.env` target.**

```
if exist "%USERPROFILE%\FTNewsMonitor\python\python.exe" → target is %LOCALAPPDATA%\FTNewsMonitor\.env  (legacy ZIP)
else                                                     → target is %USERPROFILE%\FTNewsMonitor\.env  (git-clone, v0.7.0 default)
```

**Do not deviate.** For a fresh git-clone install (the case you're in
after Section E), the correct target is
`%USERPROFILE%\FTNewsMonitor\.env`. Writing to `%LOCALAPPDATA%\FTNewsMonitor\.env`
in this case is a silent bug — the app will not read it.

Before you write, echo the resolved target back:

```
echo TARGET: %USERPROFILE%\FTNewsMonitor\.env  (GIT_CLONE_LAYOUT)
```

**F.2 Verify `.gitignore` covers `.env`** (safety net so a rogue
`git add .` cannot commit secrets).

```
findstr /x /c:".env" "%USERPROFILE%\FTNewsMonitor\.gitignore"
```

If this returns nothing, HARD STOP and tell the colleague — do not
write `.env` until `.gitignore` covers it.

**F.3 Discover AWS credentials on the laptop.**

Check in this order, use the first thing that resolves cleanly:

1. `%USERPROFILE%\.aws\config` — SSO profiles are declared as
   `[profile <name>]` sections. Look for `sso_start_url`,
   `sso_role_name`, or `sso_session` markers. Note the profile name.
2. `%USERPROFILE%\.aws\credentials` — static keys are declared as
   `[<name>]` sections with `aws_access_key_id` and
   `aws_secret_access_key`.
3. `%USERPROFILE%\.aws\sso\cache\*.json` — active SSO sessions. Files
   modified in the last 8 hours indicate a currently valid login.
4. Current shell env: `AWS_ACCESS_KEY_ID`, `AWS_PROFILE`, `AWS_REGION`.
5. If the AWS CLI is on PATH, run `aws sts get-caller-identity` — if
   it succeeds without any explicit `--profile`, the default profile
   works. If it fails, try each profile name with
   `aws sts get-caller-identity --profile <name>`.

**Prefer `AWS_PROFILE` over static keys.** SSO-refreshed credentials
rotate automatically and are the FlixBus-preferred pattern.

**F.4 What to write.**

Required keys (always):

```
AI_PROVIDER=bedrock
AWS_REGION=eu-west-1
```

Plus ONE of these auth blocks:

**Preferred — SSO profile:**

```
AWS_PROFILE=<profile-name>
```

**Fallback — permanent IAM static keys (`AKIA...`):**

```
AWS_ACCESS_KEY_ID=<AKIA...>
AWS_SECRET_ACCESS_KEY=<...>
```

**STS-temporary keys (`ASIA...`) — supported since v0.6.1.** If the
colleague has only temporary session credentials, write all three:

```
AWS_ACCESS_KEY_ID=<ASIA...>
AWS_SECRET_ACCESS_KEY=<...>
AWS_SESSION_TOKEN=<...>
```

Warn the colleague that STS-temp keys expire (typically 8–12 hours)
and will need to be refreshed via SSO login.

**F.5 Preserve existing keys in `.env`.**

Read `.env` first (if it exists). Modify ONLY these keys:
`AI_PROVIDER`, `AWS_REGION`, `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`. Leave every other line
untouched (e.g. `OUTLOOK_*`, comments, prior manual edits).

**F.6 When you cannot find credentials.**

Do NOT guess. Do NOT invent values. Do NOT ask the colleague to paste
an Access-Key into the chat. Say, in plain language:

> "I could not find AWS credentials on your laptop. Please contact
> Jordan — he will send you an Access-Key and Secret via a secure
> channel (not email)."

**F.7 Credentials handling — HARD RULES.**

**Never accept AWS credentials pasted into this chat, even if the
colleague offers them.** Chat transcripts are logged by the assistant
vendor and their subprocessors. If the colleague pastes a key anyway:

- Refuse to write it.
- Reply: *"I can't write credentials from chat because that logs them
  in the transcript. Please open `%USERPROFILE%\FTNewsMonitor\.env`
  in Notepad yourself and paste the keys there. Save the file, then
  double-click `start.bat`."*
- Do NOT repeat the pasted value in your response.

---

## G. Done — hand-off message to the colleague

After F succeeds, tell the colleague (verbatim, this is the exact
hand-off):

> **Fertig. Nächste Schritte für Dich:**
>
> 1. Doppelklick auf `%USERPROFILE%\FTNewsMonitor\start.bat`.
> 2. Der Browser öffnet `http://127.0.0.1:8766/setup`.
> 3. Wizard-Step 2 (AWS) sollte **grün** angezeigt werden mit Deinen
>    AWS-Werten. Falls nicht: sag Jordan Bescheid.
> 4. Klick durch die restlichen Wizard-Schritte (Home-Märkte,
>    Seed-Packs, Scheduler).
> 5. Für Updates: Doppelklick auf `update.bat` im gleichen Ordner.

Do NOT launch `start.bat` yourself. It runs a blocking foreground
server and will hang your terminal.

---

## H. HARD RULES — read carefully

1. **Never print, echo, or paste AWS Access-Keys, Secret-Keys, or
   Session-Tokens into your chat responses.** Write them ONLY to the
   `.env` file, and only via the on-laptop discovery flow in F.3.
2. **Never write `.env` outside `%USERPROFILE%\FTNewsMonitor\`** in a
   git-clone install. The layout check in F.1 is not optional. If
   the check says `%LOCALAPPDATA%\FTNewsMonitor\.env`, that means
   Section I (legacy ZIP) — you should have detected that in B.3.
3. **Never run `start.bat` yourself.** Tell the colleague to
   double-click it. It launches a blocking web server.
4. **Never install to `Program Files\`, `OneDrive\`, or `SharePoint\`.**
   SQLite fails silently under sync. Use `%USERPROFILE%\FTNewsMonitor\`
   or (for tools) `%USERPROFILE%\FTNewsMonitor-tools\`.
5. **Never `pip install` outside the venv.** Every pip command in
   this runbook uses `"%USERPROFILE%\FTNewsMonitor\venv\Scripts\pip.exe"`
   explicitly. Do not use system `pip` or `py -m pip`.
6. **Never bypass a HARD STOP.** If a check fails, tell the
   colleague, and stop. Don't work around it.
7. **Never modify AGENTS.md, `.gitignore`, or files inside
   `%USERPROFILE%\FTNewsMonitor\` other than `.env`.** Everything
   else is under Jordan's control via the repo.
8. **Never commit `.env` — verify it's in `.gitignore` first (F.2).**
   If for some reason it isn't, HARD STOP.

---

## I. Legacy ZIP fallback

Only applies if B.3 detected a v0.6.x ZIP extract (sibling folders
`python\` and `app\` next to `start.bat`).

**I.1 Skip Sections C, D, E entirely.** The ZIP already contains
Python 3.13 embedded and all dependencies bundled. There is nothing
to bootstrap.

**I.2 Layout in this case:**

- Data lives in `%LOCALAPPDATA%\FTNewsMonitor\` (created on first
  launch of `start.bat`).
- `.env` target is `%LOCALAPPDATA%\FTNewsMonitor\.env`. This is the
  legacy path — the app's `_resolve_data_dir()` picks it up because
  `python\python.exe` next to `start.bat` triggers the packaged
  layout.

**I.3 Create the target folder if it doesn't exist:**

```
mkdir "%LOCALAPPDATA%\FTNewsMonitor" 2>nul
```

**I.4 Then jump to F.3–F.7** with the target changed to
`%LOCALAPPDATA%\FTNewsMonitor\.env`. Everything else (AWS discovery,
what to write, hard rules) is identical.

**I.5 Hand-off (Section G) — adjusted path:**

Tell the colleague to double-click `start.bat` in the ZIP-extract
folder (not `%USERPROFILE%\FTNewsMonitor\`). The rest of G is the
same.

---

## Not your job (both layouts)

- Home markets selection.
- Importing seed packs (DACH, EU, Italy, etc.).
- Outlook mailbox configuration.
- Daily scheduler.
- Anything else in the web wizard beyond Step 2.

All of these are done by the colleague in the browser after they
start the tool. Do not attempt them from AGENTS.md.
