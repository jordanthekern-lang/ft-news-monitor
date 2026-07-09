# FT News Monitor v0.4.0 — Delivery Note

**Release:** v0.4.0 · **Build date:** 2026-07-08 · **Target:** Rollout-hardening
for the international rollout — shipped seed packs update automatically without
touching user edits, and existing installs survive a version upgrade with every
keyword, every golden label, every star intact.

## What is new since v0.3.2

- **Auto-reseed on startup.** Every time the app launches, it checks each
  home-market's seed pack (`seeds/dach.json`, `seeds/eu.json`, …) against a
  new `imported_seed_versions` marker in `app_settings`. If a shipped seed
  file's `seed_version` is higher than the stored value, the file is merged
  into the local config tables. Only new natural keys are inserted;
  existing rows — including any keyword, feed, or outlet the user has
  edited — are preserved verbatim. The keyword catalogue grows with each
  release without any manual step.
- **DACH and EU baseline updated.** The shipped `seeds/dach.json` and
  `seeds/eu.json` are now regenerated from Jordan's hand-curated Prod DB.
  DACH: 58 keywords / 16 feeds / 14 outlets / 16 negatives (from 45 / 16 /
  14 / 16 in v0.3.x). EU: 31 keywords / 11 feeds / 19 outlets / 8 negatives
  (from 26 / 12 / 19 / 8). `seed_version` bumped from 1 → 2 so existing
  installs pick the additions up on their next start.
- **First-launch DB migration.** On the very first packaged launch,
  `launch.py` detects Jordan's old dev-layout `data/monitor.db` next to
  the extract and copies it into the packaged
  `%LOCALAPPDATA%\FTNewsMonitor\` location. Idempotent: the target-exists
  guard prevents a second launch from clobbering user edits. Irrelevant
  for fresh installs — the source file has to exist for the migration
  to trigger.

## Migration invariant

**No schema changes.** No column adds, no table adds, no drops, no renames.
Only two things touch the DB:

1. `runtime_settings.set(IMPORTED_SEED_VERSIONS, {...})` — a plain JSON blob
   in the existing `app_settings` table. Unknown to older code but harmless.
2. `seed_import.import_seed_file(mode="merge")` — the same merge-mode call
   the wizard has always used. Only INSERTs on new natural keys.

`%LOCALAPPDATA%\FTNewsMonitor\monitor.db` from any prior 0.x version opens
under v0.4.0 without conversion.

## Rollout for existing users (Guglielmo, Jordan, anyone else on 0.3.x)

1. Extract `FTNewsMonitor-0.4.0-win64.zip` over the existing extract folder.
2. Double-click `start.bat`.
3. The startup logs show `Auto-reseed: dach v2 applied (previous v1), …`
   the very first time; subsequent launches are silent (already at
   `seed_version: 2`).
4. Dashboard, Admin-UI, and Settings behave identically to v0.3.2 —
   auto-reseed is invisible unless you look at `/admin/keywords` and
   notice the row count went up.

**Nothing to migrate manually.** The `%LOCALAPPDATA%\FTNewsMonitor\` DB
and `.env` are outside the extract folder and stay put across a re-extract.

## Rollout for new colleagues

1. Extract the ZIP.
2. Double-click `start.bat`. Browser opens `/setup`.
3. Wizard step 1: pick home markets (e.g. `Italy`, `EU` for the Italian
   pilot).
4. Wizard step 4: the seed packs matching your home markets are already
   ticked (EU is always ticked; DACH is ticked only when DACH is in your
   home markets). Click "Import selected seeds".
5. Wizard step 5+6: schedule + optional Outlook mailbox.
6. Wizard step 7: Finish. Dashboard opens.

Auto-reseed will run on every subsequent startup, but it's a no-op unless a
future release bumps `seed_version` — in which case the new rows appear on
next start.

## What is NOT shipping (deferred)

- **Refresh Seeds from UI button.** An Admin-UI trigger to force-refresh
  seed content of already-imported rows was considered and rejected — it
  would blur the seed-vs-user boundary. CLI escape hatch:
  `python scripts/import_seed.py --reset seeds/dach.json` (destructive:
  wipes the config tables first).
- **Change-set diff between two seed versions.** Would help curators
  visualise what's about to be added, but doesn't affect correctness.

## Costs

Unchanged from v0.3.2. Zero extra AI tokens; auto-reseed is pure SQLite I/O
running once per startup (~5 ms per home market with an up-to-date marker).

## Known quirks

- **`seed_version` decrease is a no-op.** If a future release accidentally
  ships a lower `seed_version` than what's already stored, the auto-reseeder
  will not "roll forward" — it needs an increase to run. This is intentional:
  it protects against downgrade weirdness.
- **`home_markets = []` skips auto-reseed entirely.** An install without
  home markets is treated as "wizard not complete" — the auto-reseeder
  never touches the DB.
- **DACH/EU baseline mismatch is possible on upgrade.** If a user hand-added
  a keyword that later became part of the shipped DACH baseline, the merge
  natural-key check dedupes it — the user's row wins (with its user-set
  priority, notes, etc.). The shipped variant is silently skipped. No
  duplicate rows are created.

## Support

- Log file: `%LOCALAPPDATA%\FTNewsMonitor\server.log` (auto-reseed log lines
  are at INFO level with prefix `Auto-reseed:`).
- Config export for support: `GET /admin/export-config`.
- Troubleshooting matrix: `ROLLOUT_RUNBOOK.md` §4.
- Rollback recipe: `UPGRADE.md`.
- Contact **Jordan Kern** via Teams or email.
