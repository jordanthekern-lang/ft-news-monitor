# FT News Monitor — Setup for Cowork users (no AWS needed)

> This guide is for colleagues who use **Claude Cowork 3P** on their
> Windows laptop and don't have (or don't want to use) an AWS account.
> The tool still runs locally, but the AI ranking happens inside your
> Cowork session instead of via AWS Bedrock. If you have AWS SSO on
> your laptop, use [QUICKSTART_INSTALLER.md](QUICKSTART_INSTALLER.md)
> instead — it's simpler because the tool scores articles itself.

## What you need

- Windows 10 or 11 laptop with **Claude Cowork 3P** installed
  (Company Portal delivers it).
- Internet access (VPN if you're off-site — the installer clones
  from GitHub and downloads Python packages from PyPI).
- Nothing else — no AWS, no API keys.

## Step 1 — Run `install.bat` in Cowork mode

1. Save `install.bat` (from Jordan on Teams) into a fresh empty folder
   under `%USERPROFILE%\Documents\`. Don't put it in OneDrive or
   SharePoint.
2. Double-click `install.bat`.
3. When it asks *"Which AI provider does this laptop use? [B/C]"*
   type **`C`** and press Enter.
4. Wait 3–15 minutes while it installs Git, Python 3.13, the tool
   itself, generates a local security token, and adds a Windows
   Startup shortcut so the tool auto-launches on every login. When
   the console says *"Installation complete (Cowork mode)"*, press
   any key to close it.

## Step 2 — Wire up Cowork

1. Open **Claude Cowork 3P** from your Start menu.
2. Add the tool's data folder as a **Trusted Folder** so Cowork can
   read the security token and (in fallback mode) exchange JSON with
   the tool.
   - In Cowork: *Settings → Trusted Folders → Add folder…*
   - Point it at `%USERPROFILE%\FTNewsMonitor\data\`.
3. Import the ranking skill so Cowork knows how to score articles.
   - In Cowork: *Fähigkeit hochladen* / *Skills → Import*.
   - Upload the file `%USERPROFILE%\FTNewsMonitor\skills\ftnews\ftnews.zip`.
4. Optional but recommended — set up a weekly auto-run.
   See [SKILL_INSTALL.md](SKILL_INSTALL.md) for the exact steps.

## Step 3 — Start the tool (once)

1. Double-click `%USERPROFILE%\FTNewsMonitor\start.bat`.
   A black terminal window opens; leave it running.
2. Your browser opens at `http://127.0.0.1:8766/setup`.
   Step 2 (AWS credentials) is **pre-filled and green** — it just
   says "Cowork ranking mode is active". Nothing to enter here.
3. Complete the remaining wizard steps (home markets, seed packs,
   scheduler) — see [QUICKSTART.md](QUICKSTART.md) §4.

From now on the tool starts automatically at every Windows login (an
autostart shortcut was installed by `install.bat`). You can minimise the
terminal window, but don't close it — closing it stops the server. If
you ever accidentally close it, just double-click `start.bat` again or
restart your laptop.

## Step 4 — Get your first ranking

You have two ways to trigger a ranking run:

### On demand
Type in Cowork: *"News-Ranking laufen lassen"*
(or *"rank the news please"* — the skill auto-discovers).

Cowork will:
1. Check that the tool is running (it should be — autostart handles that).
2. Trigger a scrape.
3. Wait for the scrape to finish.
4. Fetch the newly-collected articles.
5. Rank each one against the FlixBus Public Affairs criteria.
6. Push the scores back to the tool.
7. Tell you *"Fertig. N Artikel bewertet. Öffne
   http://127.0.0.1:8766/ um die Ergebnisse zu sehen."*

You then open the dashboard link and use it exactly like the standard
tool — filter, star, export, everything.

If Cowork says *"Der FT News Monitor läuft gerade nicht"*, the terminal
got closed. Double-click `start.bat` and re-run the skill.

### Scheduled
If you added a weekly scheduled task in Cowork (Step 2.4), it fires
automatically. Autostart keeps the server up between reboots, so the
scheduled scrape and Cowork ranking will keep filling the dashboard
even if you don't touch anything for a week.

## What if it fails?

The installer logs to `%TEMP%\ftnm-install.log`. Send the last ~50
lines to Jordan if `install.bat` errors out.

Common issues:

| Message | What it means |
|---|---|
| `Your Windows profile is inside a sync'd location` | You saved `install.bat` under OneDrive/SharePoint. Move it to `Documents\`. |
| `winget failed ... IT install is required` | Corporate policy blocks winget. Ask IT to install Python 3.13 or Git manually. |
| `git clone failed` | VPN / corporate proxy issue. Reconnect to VPN or ask IT to allowlist `github.com`. |
| Cowork says *"Der FT News Monitor läuft gerade nicht"* | The tool isn't running (window got closed, or autostart didn't fire yet). Double-click `start.bat` yourself and re-run the skill. |
| Dashboard says *"N articles waiting for Cowork ranking"* and doesn't clear | Trigger the skill in Cowork manually. |

## Updating later

Run `update.bat` in the `%USERPROFILE%\FTNewsMonitor\` folder to fetch
new versions. The `ftnews` skill file is included in the update; if
you want the newest skill live immediately, re-import it in Cowork.

## Difference vs. the Bedrock install

| | Bedrock mode | Cowork mode |
|---|---|---|
| Needs AWS on this laptop | Yes | No |
| Scoring happens... | ...inside the tool via AWS Bedrock | ...inside your Cowork session |
| Ranking cadence | Automatic — the tool schedules it | Requires Cowork to be running when you (or the schedule) trigger it |
| Setup wizard AWS step | Fill in keys or profile | Skipped — already green |
| Update path | `update.bat` | `update.bat` |
