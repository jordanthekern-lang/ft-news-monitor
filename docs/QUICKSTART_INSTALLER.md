# FT News Monitor — Installation via `install.bat`

> **For colleagues who don't use Claude Code.** If you have Claude Code
> installed on your laptop, use [`AGENTS.md`](../AGENTS.md) instead —
> it's faster because the AI handles everything interactively.
> `install.bat` is the fallback for Claude Cowork users and anyone
> without a local AI assistant.

## What you need

- Windows 10 or 11 laptop.
- Internet access (VPN if you're off-site — the installer clones from
  GitHub and downloads Python packages from PyPI).
- A working AWS SSO login on this laptop (if you use Claude Cowork,
  you already have this — Cowork itself requires it).

## Steps

1. **Save `install.bat`** into a fresh empty folder under
   `%USERPROFILE%\Documents\`. For example:
   `Documents\FTNewsMonitor-install\`.

   > **Do NOT** put it in OneDrive, SharePoint, or `Program Files\` —
   > SQLite fails silently under sync, and the tool won't work.

2. **Double-click `install.bat`**.

   A black terminal window opens. You'll see six phases run:

   ```
   [1/6] Preflight checks...
   [2/6] Checking Git...
   [3/6] Checking Python 3.13...
   [4/6] Cloning repository...
   [5/6] Creating venv and installing Python dependencies...
   [6/6] Discovering AWS credentials and writing .env...
   ```

   Total time: **3–15 minutes** depending on what's already installed
   on your laptop.

3. **Wait for the "Installation complete" banner.** The window won't
   close on its own — press any key when told, then close it.

4. **Double-click `%USERPROFILE%\FTNewsMonitor\start.bat`.**

   Your browser opens at `http://127.0.0.1:8766/setup`. Wizard step 2
   (AWS credentials) should be **pre-filled and green** — no need to
   paste any keys.

5. Complete the remaining wizard steps in your browser (home markets,
   seed packs, scheduler). See [`QUICKSTART.md`](QUICKSTART.md) §4 for
   details.

## What if it fails?

The installer logs everything to `%TEMP%\ftnm-install.log`. The
terminal will tell you the path. Send the last ~50 lines of that
file to Jordan.

Common causes:

| Message                                     | What it means                                                                        |
|---------------------------------------------|--------------------------------------------------------------------------------------|
| `Your Windows profile is inside a sync'd location` | You saved `install.bat` under OneDrive/SharePoint. Move it to `Documents\`.  |
| `winget failed ... IT install is required`  | Corporate policy blocks `winget`. Ask IT to install Python 3.13 or Git manually.     |
| `git clone failed`                          | VPN / corporate proxy issue. Reconnect to VPN or ask IT to allowlist `github.com`.   |
| `No AWS credentials found`                  | You're not signed into AWS on this laptop. Sign in via your usual SSO flow, retry.   |

## Updating later

Once installed, run **`update.bat`** in the `%USERPROFILE%\FTNewsMonitor\`
folder to fetch new versions. No need to re-run `install.bat`.
