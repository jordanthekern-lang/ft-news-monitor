# Contributing / Release Process

Audience: Jordan (release owner), or whoever inherits the FT News Monitor
release channel. Colleagues who just use the tool don't need this file —
see `QUICKSTART.md` and `UPGRADE.md` instead.

## Repository layout

- `origin` remote: `github.com/jordanthekern-lang/ft-news-monitor.git` (since v0.7.0)
- Active branch: `main`
- Tags: one per shipped release, format `v<MAJOR>.<MINOR>.<PATCH>`.
- Historical remote (v0.4.x–v0.6.1, multi-project layout):
  `github.com/jordanthekern-lang/ft-news-monitor-alt.git`, branch
  `market-edition`. Preserved read-only for tag history.

## Cutting a release

1. Land the last commit for the release on `market-edition` locally.
   Confirm the test suite is green:

   ```powershell
   venv\Scripts\python.exe -m pytest tests/ -q
   ```

2. Bump `VERSION` at the repo root. Add a corresponding `## [<version>]`
   section to `docs/CHANGELOG.md` — include a one-line note whether the
   release is code-only (Path A / `update.bat` works) or ships new
   dependencies / an interpreter bump (Path B / ZIP re-extract).

3. Build the ZIP if you're distributing via Path B as well:

   ```powershell
   venv\Scripts\python.exe build\build_release.py
   ```

   Output: `dist\FTNewsMonitor-<version>-win64.zip`.

4. Commit the version bump + CHANGELOG:

   ```powershell
   git commit -m "market-edition: v<version> — <short summary>"
   ```

5. Tag the release:

   ```powershell
   git tag v<version>
   ```

6. Push branch + tags to `origin`:

   ```powershell
   git push origin main
   git push origin v<version>
   ```

7. If Path B is needed, upload the ZIP wherever you distribute (Teams /
   OneDrive / GitHub release). Path A colleagues only need step 6.

## First install for a new colleague (v0.6.0+)

Two roads. Path B is the safe default; Path A only works for colleagues
whose corporate laptop has Git + Python 3.13 installed (rare at
FlixBus). See `docs/ROLLOUT_RUNBOOK.md` for the pilot session
checklist.

### Path A — direct git clone (dev-style)

```powershell
git clone https://github.com/jordanthekern-lang/ft-news-monitor.git C:\Tools\FTNewsMonitor
cd C:\Tools\FTNewsMonitor
py -3.13 -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python.exe scripts\launch.py
```

Updates use `update.bat` from then on.

### Path B — ZIP (default)

Send the colleague `dist\FTNewsMonitor-<version>-win64.zip`. They
extract it, double-click `start.bat`, and go through the wizard. When
you ship a code-only follow-up release later, they either extract the
new ZIP over the old folder (Path B again) or — if they installed Git
in the meantime — they can start using `update.bat` (Path A).

## Secret hygiene

- **Never commit** `.env`, `data/monitor.db`, or `%LOCALAPPDATA%`
  content. The `.gitignore` covers these paths, but double-check
  `git status` before every commit.
- **AWS credentials never live in the repo** — they belong in the
  per-user `.env` in `%LOCALAPPDATA%\FTNewsMonitor\`.
- Before the first push to a public / shared remote, run:

  ```powershell
  git log --all -p | Select-String -Pattern "aws_secret|access_key|password"
  ```

  Must be empty. If not: rewrite history with `git filter-repo` before
  pushing.

## Skipping-hooks policy

The commit workflow does not use `--no-verify` or `--no-gpg-sign`
unless the user has explicitly asked for it. If a pre-commit hook
fails, fix the underlying issue and create a new commit — do not
amend a hook-blocked commit.
