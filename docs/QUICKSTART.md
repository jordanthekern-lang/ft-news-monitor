# FT News Monitor — Quickstart

## What is this?

FT News Monitor scans European news sources every day for updates on
long-distance-coach fleet transformation — funding programmes, EU and
national policy, tenders, competitor moves, technology. You configure it
once, then it runs on your laptop every morning and shows you what is
worth reading before your Public Affairs team meeting.

You do not need Python, admin rights, or a developer background. The tool
runs entirely on your laptop and stores everything locally.

---

## 1. Install (primary path — AGENTS.md + Claude Code/Cowork)

Jordan sends you **one file** on Teams: `AGENTS.md` (about 15 kB).

1. Create a fresh empty folder somewhere under
   `%USERPROFILE%\Documents\` (not OneDrive, not SharePoint, not
   Program Files — SQLite fails silently under sync).
   Example: `Documents\FTNewsMonitor-bootstrap\`.
2. Drop `AGENTS.md` into that folder.
3. Open **Claude Code** (or **Claude Cowork**) in that folder.
4. Type: *"Lies AGENTS.md und richte alles ein."*

Claude will autonomously check your laptop, install Git and Python 3.13
if missing (via `winget` or portable downloads), clone the repo to
`%USERPROFILE%\FTNewsMonitor`, create a virtual environment, install
Python dependencies, find your AWS credentials on the laptop, and
write `.env`. Takes about 5–15 minutes depending on what's already
installed. Claude will tell you what it's doing at each step.

**Do NOT** paste any AWS Access-Keys or Secrets into the chat with
Claude. It will find them on your laptop; if it can't, it will tell
you to contact Jordan for a secure delivery.

## 1b. Alternative install path — `install.bat` (for Cowork users)

If you use **Claude Cowork** (not Claude Code), the AGENTS.md flow
above won't work — Cowork runs in a Linux sandbox that can't reach your
Windows shell. Jordan sends you a single file `install.bat` instead.

Save it in a fresh empty folder under `%USERPROFILE%\Documents\`, double-click,
and wait 3-15 minutes. It handles Git + Python bootstrapping, clones the
repo, installs dependencies, and configures the AI provider.

`install.bat` asks you upfront which AI provider this laptop uses:

- **B (Bedrock)** — if you have AWS SSO on this laptop. The installer
  discovers your credentials automatically. Full details:
  [QUICKSTART_INSTALLER.md](QUICKSTART_INSTALLER.md).
- **C (Cowork)** — if you have Claude Cowork 3P but no AWS. The tool
  runs locally but the AI ranking is delegated to your Cowork session
  via the shipped `ftnews` skill. Full details:
  [QUICKSTART_COWORK.md](QUICKSTART_COWORK.md).

## 2. Alternative install path (ZIP, for laptops without Claude Code)

If you don't have Claude Code or Cowork, Jordan can send you the
classic ZIP (`FTNewsMonitor-0.7.0-win64.zip`, about 45 MB):

- Save it somewhere **stable** under `%USERPROFILE%\Documents\`.
- **Do not** put it in `OneDrive`, `SharePoint`, or `Program Files`.
- Right-click the ZIP → **Properties** → tick **Unblock** → **OK**
  (this clears the Windows Mark-of-the-Web flag).
- Right-click the ZIP → **Extract All…** → pick the target folder.
  Delete any trailing nested suffix Windows auto-suggests so you get
  a single-level extract.
- The extracted folder contains `start.bat` and subfolders (`python\`,
  `app\`, `seeds\`, `lib\`, …). Data will land in
  `%LOCALAPPDATA%\FTNewsMonitor\` on first launch.

## 3. First launch

Double-click **`start.bat`** — in `%USERPROFILE%\FTNewsMonitor\` for
the Claude-based install, or in the extracted ZIP folder for the ZIP
path.

- A black terminal window opens. Leave it running — that **is** the app.
  Closing it stops the tool.
- After a few seconds your default browser opens at the URL printed on
  the first line of the terminal (usually
  `http://127.0.0.1:8766/setup`, but may be up to `:8775` if `:8766` is
  already busy — `start.bat` scans 8766–8775 and binds to the first
  free port).
- If Windows SmartScreen asks — click **More info → Run anyway**.

## 4. First-run setup wizard

Five short steps, roughly five minutes:

**Step 1 — Home markets.** Tick the country or region you cover
(e.g. `Italy`), plus `EU`. You can change this later.

> **If you used the AGENTS.md path (Section 1 above):** Step 2 is
> already filled in and shows green. Skim it, then click through to
> Step 3. No need to paste any AWS keys yourself.

**Step 2 — AI credentials (only if you used the ZIP path).** Paste
the values Jordan sends you separately:

| Field                   | Value                                                       |
|-------------------------|-------------------------------------------------------------|
| Auth mode               | Static AWS keys                                             |
| `AWS_ACCESS_KEY_ID`     | (from Jordan)                                               |
| `AWS_SECRET_ACCESS_KEY` | (from Jordan)                                               |
| Region                  | `eu-west-1` (default)                                       |
| Model                   | Leave the default `eu.anthropic.claude-sonnet-5-5`          |

The Advanced section (Provider / ANTHROPIC_API_KEY) stays untouched.
Click **Save credentials**.

**Step 3 — Test AI connection.** Click **Test AI now**. A green line
appears with the model name and a latency reading. A red line means the
keys or the region are wrong — send the red text to Jordan.

**Step 4 — Import seed packs.** Your market plus `EU` are pre-ticked.
Click **Import selected**. You will see a line like
`Italy: imported · keywords 29+/0s · feeds 12+/0s · outlets 18+/0s`.
Non-zero numbers on the left of the slash mean it worked.

**Step 5 — Daily schedule.** Tick the box, set a time (e.g. `06:00` —
before your morning market call). Click **Save schedule**.

Finally, click **Finish and open dashboard**.

> **Day-1 tip.** The dashboard is empty on the very first launch — the
> collector hasn't run yet. Click **Run collection** in the top nav (it
> also fires automatically about two minutes after the app starts if the
> scheduler is on and the last run is older than 20 hours).
>
> **Your first run is special:** it does a one-off historical sweep
> going back **5 years** to populate the dashboard with the past
> catalogue of relevant articles. That takes **15–30 minutes** and
> can pull in thousands of articles. Leave the terminal window open,
> refresh the dashboard occasionally. All later runs use a 3-month
> window and finish in 2–4 minutes.

## 5. Everyday use

Every morning:

1. Double-click `start.bat` (or leave the terminal window from the day
   before still running — the scheduler catches up on any missed days).
2. Wait a minute if the app has just launched — it fires a catch-up run
   about two minutes after start if the previous run is more than 20
   hours old.
3. The dashboard opens automatically. Use the filter bar at the top:
   - **Min. score** slider — start at `6`. Lower it to see more; raise
     it (`8+`) for signal-only.
   - **Time window** — 30 days by default.
   - **Status** — `All`, `New` (unseen), or `Starred`.
4. Skim the list. Click article titles to open the source. Click **☆ Star**
   for things worth revisiting or forwarding; **Ignore** for things you
   never want to see again from this outlet.

The star/ignore signals feed back into the model over time — the tool
learns which outlets you actually value.

## 6. Confluence and email exports

Once your filter view shows what you want to share:

- **Copy as Markdown** — copies the filtered list to your clipboard as
  Confluence-friendly markdown. Paste directly into your market-call
  recap page.
- **Download Markdown** — same content, saved as
  `ft-news-monitor_YYYY-MM-DD.md`.
- **Download CSV** — Excel-friendly CSV with a UTF-8 byte-order mark so
  Italian and Nordic accents display correctly on double-click.

## 7. Troubleshooting

| Symptom                                     | What to do                                                                                     |
|---------------------------------------------|-----------------------------------------------------------------------------------------------|
| Browser did not open                        | Open the URL printed on the first terminal line (usually `http://127.0.0.1:8766`, may be up to `:8775` if that port is busy). |
| "Test AI now" shows red                     | Copy the red text and send to Jordan. Usually wrong AWS keys or corporate-proxy TLS.          |
| Terminal window closes immediately          | Open the server log (`%LOCALAPPDATA%\FTNewsMonitor\server.log` for ZIP installs, `%USERPROFILE%\FTNewsMonitor\data\server.log` for Claude-installed), send the last ~50 lines to Jordan. |
| Zero articles today                         | Normal on quiet weekends. If three days at zero, open `/settings` and check the config.        |
| Dashboard is empty on Monday morning        | The scheduler triggers a catch-up run ~2 minutes after launch. Refresh the page after 5 min. |
| "A collection run is already in progress"   | Wait 1–2 minutes and click again. The manual and scheduled runs share a lock.                 |

If in doubt, close the terminal window, restart `start.bat`, and try
again. Nothing persists in memory — your data is on disk (either
`%LOCALAPPDATA%\FTNewsMonitor\monitor.db` for ZIP installs, or
`%USERPROFILE%\FTNewsMonitor\data\monitor.db` for Claude-installed)
and survives every restart.

For deeper configuration (curating your own keywords, tuning excludes,
exporting the config) see `ADMIN_GUIDE.md`.
