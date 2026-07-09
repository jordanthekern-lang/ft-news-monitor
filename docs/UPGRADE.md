# Upgrading an existing FT News Monitor install

This document is for Jordan and the pilot colleagues who already have an
FT News Monitor install running (v0.1.0 through v0.3.2) and want to move
to a newer release without losing any of their curated content.

## What survives an upgrade

**Always safe** — these live in `%LOCALAPPDATA%\FTNewsMonitor\` (outside
the extract folder, untouched by re-extraction):

- Every article ever ingested (`articles` table).
- All user actions on articles: starred, ignored, read, AI scores.
- Every keyword, feed, outlet, negative keyword the user or the wizard
  has added (`keywords`, `feeds`, `outlets`, `negative_keywords`).
- Golden-Set labels (`golden_labels`).
- Keyword-suggestion history (`keyword_suggestions`).
- Runtime settings: home markets, min-score default, scheduler,
  translation targets, Suggest-keywords flag.
- Source health / consecutive-failure counters.
- The `.env` file with AWS/Anthropic credentials and Outlook settings.

**Replaced by the new ZIP**:

- The Python interpreter (`python\`).
- Application code (`app\`, `scripts\`, `docs\`).
- Shipped seed packs (`seeds\*.json`).

## Doing the upgrade

There are two upgrade paths — pick the one that matches how you got the
tool. Both leave your data (`monitor.db`, `.env`, settings) untouched,
so nothing is lost either way.

**Since v0.7.0, Path A is the default** for new installs bootstrapped
via `AGENTS.md` + Claude Code/Cowork. Data lives in
`%USERPROFILE%\FTNewsMonitor\data\` for these installs.

### Path A — via `update.bat` (git-based, default since v0.7.0)

Requirements: you cloned the tool via `git clone` originally (either
directly or via the AGENTS.md bootstrap) and Git is on your PATH.

1. Double-click `update.bat` in the tool folder
   (`%USERPROFILE%\FTNewsMonitor\` for AGENTS.md installs).
2. It runs `git pull --ff-only`. New code, seeds, and docs come down;
   your DB, `.env`, and `venv\` are ignored by Git and are unaffected.
3. **New in v0.7.0**: if `requirements.txt` changed in the pull,
   `update.bat` automatically runs
   `venv\Scripts\pip.exe install -r requirements.txt` to refresh the
   virtual environment. This prevents ModuleNotFoundError on the
   next `start.bat` when Jordan adds a new dependency.
4. Double-click `start.bat`.

If `update.bat` reports "not a git clone" or "git not installed", fall
back to Path B.

### Path B — via ZIP (legacy, works for every install)

1. **Optional but recommended**: back up `%LOCALAPPDATA%\FTNewsMonitor\` to
   a snapshot folder (`monitor.db`, `.env`, `MIGRATED_FROM_DEV.txt` if
   present). Zero-cost insurance.
2. Extract `FTNewsMonitor-<new-version>-win64.zip` next to the existing
   extract, or over the top of it. **Do not** delete
   `%LOCALAPPDATA%\FTNewsMonitor\`.
3. Double-click the new `start.bat`.
4. Open the browser at http://127.0.0.1:8766/ (usually pops open
   automatically).
5. Spot-check: `/admin/keywords` count is at least as high as before —
   auto-reseed only adds, never removes.

That's it. No wizard re-run, no manual DB migration, no config edit.

## When to use which path

- **Path A (`update.bat`)** for every release since v0.7.0. Handles
  both code updates and Python-dependency refresh (via the
  `requirements.txt` hash check). Fast — under a minute for most
  releases.
- **Path B (ZIP)** only if you're on the legacy ZIP install and can't
  switch to git. The vendored `python/` and `lib/` directories only
  get refreshed by re-extracting a ZIP; a Python-interpreter version
  bump (rare — currently pinned to 3.13) needs Path B for ZIP users.

The `docs/CHANGELOG.md` entry for each release calls out which path is
needed. If unsure: Path B is always safe for ZIP installs; Path A is
always safe for git-clone / AGENTS.md installs.

## What auto-reseed does on the first launch of the new version

If the new ZIP ships seed packs with a higher `seed_version` than your
install has already imported (tracked in `app_settings` under
`imported_seed_versions`), the merge-mode importer runs for each of your
home markets and **adds** any new rows. Existing rows — including anything
you hand-edited in the Admin UI — are left untouched.

The startup log (`%LOCALAPPDATA%\FTNewsMonitor\server.log`) will contain
lines like:

```
INFO app.main: Auto-reseed: dach v2 applied (previous v1),
  report={'inserted': 13, 'skipped': 45, 'markt_unioned': 0}
```

Subsequent launches with no seed-version bump are silent.

## Rolling back

Additive-only migrations are invariant across the whole 0.x line, so
downgrading is safe:

1. Stop the running server.
2. Re-extract the older ZIP (e.g. `FTNewsMonitor-0.3.2-win64.zip`) into
   the extract folder.
3. Double-click `start.bat`. The DB is opened by the old code without
   conversion. Rows added by the newer seed pack stay in the DB but are
   simply unknown to the older keyword list.

## Special case: Jordan's dev-layout DB → packaged layout

Jordan has been running the tool out of `c:\Tools\Jordan\foerder-monitor-market-edition\`,
which is the dev layout: DB is at `<repo>\data\monitor.db`. The packaged
release expects the DB at `%LOCALAPPDATA%\FTNewsMonitor\monitor.db`.

The v0.4.0 `launch.py` migrates this automatically on the first packaged
launch:

- Detects the embedded-Python layout (`python\python.exe` next to the
  extract).
- Checks that the packaged DB doesn't exist yet.
- Copies `<repo>\data\monitor.db` (or `<repo-parent>\data\monitor.db`)
  into `%LOCALAPPDATA%\FTNewsMonitor\monitor.db`.
- Writes a `MIGRATED_FROM_DEV.txt` marker file.
- Leaves the source file in place as a manual backup.

A second launch is a no-op (target-exists guard).

If for some reason the automatic migration doesn't run (e.g. your
extract sits somewhere the code doesn't look), the manual recipe:

```powershell
mkdir "$env:LOCALAPPDATA\FTNewsMonitor"
copy "<repo>\data\monitor.db" "$env:LOCALAPPDATA\FTNewsMonitor\monitor.db"
copy "<repo>\.env"           "$env:LOCALAPPDATA\FTNewsMonitor\.env"
```

## Sanity checks after upgrade

- `GET /api/stats` returns roughly the article count you had before,
  plus whatever fresh collection has added.
- `GET /admin/keywords` count ≥ your old count.
- `GET /admin/golden` shows the same labelled articles.
- The dashboard header status chip shows a recent `last_run_at` (unless
  you upgraded during a run — in that case, retrigger via the "Run
  collection" button).

If any of those fail, restore the backup and open a ticket with the
delta between your backup and current DB.
