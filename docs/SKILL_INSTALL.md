# Installing the `ftnews` skill in Claude Cowork

The `ftnews` skill lives in your tool folder at
`%USERPROFILE%\FTNewsMonitor\skills\ftnews\`. It contains two files:

- `SKILL.md` — the instructions Cowork follows when the skill is
  invoked (fetch pending articles, rank them, push scores back).
- `README.md` — human-readable overview of what the skill does.

You only need to install the skill once per laptop. `update.bat`
keeps the file on disk up to date; re-import inside Cowork whenever
the version bumps if you want the new logic live immediately.

## One-time setup

### 1. Trust the data folder

Cowork's Linux sandbox can only read/write folders you explicitly
trust. The skill needs to read the local security token and (in
fallback mode) exchange JSON with the tool.

1. Open **Cowork → Settings → Trusted Folders**.
2. Click **Add folder…**
3. Pick `%USERPROFILE%\FTNewsMonitor\data\`.
4. Save.

### 2. Import the skill

1. Open **Cowork → Settings → Skills**
   (or whatever the equivalent panel is called in your Cowork build).
2. Click **Import skill…**
3. Point Cowork at `%USERPROFILE%\FTNewsMonitor\skills\ftnews\`.
4. Confirm.

Cowork reads the frontmatter `description` field and now
auto-discovers the skill when a user says
*"News-Ranking laufen lassen"*, *"rank the news"*, or similar.

### 3. Verify

Type in Cowork: *"News-Ranking laufen lassen"*.

You should see Cowork:

1. Check the tool health (`GET /api/health`).
2. Trigger the scrape (`POST /run-collection`).
3. Poll `/pipeline-status` until idle.
4. Fetch pending articles (`GET /api/articles/pending`).
5. Score each one and post them back (`POST /api/articles/scores`).
6. Confirm: *"Fertig. N Artikel bewertet."*

If Cowork reports "I couldn't reach the FT News Monitor", double-click
`%USERPROFILE%\FTNewsMonitor\start.bat` and try again.

## Weekly automatic runs

The tool scrapes on its own schedule (see the wizard's Step 5), but
the Cowork ranking needs Cowork to be open when it fires. Two options
to automate:

### Option A — Cowork native scheduler (preferred)

Some Cowork builds have a built-in *Scheduled skills* panel:

1. Open **Cowork → Settings → Scheduled skills** (or the equivalent).
2. Add a new task.
3. Skill: `ftnews`. Cadence: weekly, e.g. Monday 07:00.
4. Save.

Cowork wakes itself up, runs the skill, and closes. Nothing else to
configure.

### Option B — Windows Task Scheduler fallback

If your Cowork build has no scheduling UI, use Windows Task
Scheduler to open Cowork with a prompt:

1. **Start → Task Scheduler → Create Basic Task…**
2. Name: `FT News Monitor - weekly Cowork rank`.
3. Trigger: weekly, e.g. Monday 07:00.
4. Action: *Start a program*.
5. Program:
   `"C:\Program Files\Claude-3p\Claude-3p.exe"` (verify the exact
   install path on your laptop — Company Portal may have installed it
   under `%LOCALAPPDATA%\Claude-3p` instead).
6. Arguments:
   `--prompt "Run ftnews now"`
7. Save.

When the task fires, Cowork opens, runs the skill, and stays open. You
can either close it manually afterwards or let it sit until the next
run.

## Updating the skill

`update.bat` (the tool's normal update flow) pulls the latest
`SKILL.md`. Cowork usually reloads skills on next launch; if you want
the new version live immediately, re-import the folder in Cowork.

## Uninstalling

- **Remove the skill:** *Cowork → Settings → Skills → ftnews →
  Delete*.
- **Revoke folder access:** *Cowork → Settings → Trusted Folders →
  remove `FTNewsMonitor\data`.*
- **Turn off scheduled runs:** delete the Task Scheduler task (or
  disable the Cowork scheduled skill).

Uninstalling the skill leaves the tool itself running. Switch the
tool back to Bedrock/anthropic mode by editing the `AI_PROVIDER`
line in `%USERPROFILE%\FTNewsMonitor\.env`.
