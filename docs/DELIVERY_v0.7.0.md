# FT News Monitor v0.7.0 — Delivery Note

**Release:** v0.7.0 · **Build date:** 2026-07-09 · **Target:**
Distribution-model shift — colleagues bootstrap the tool from a single
`AGENTS.md` file plus their AI assistant (Claude Code or Cowork),
instead of receiving a ~45 MB ZIP by mail. Rollout to six markets
becomes a Teams message with one attachment.

## What is new since v0.6.1

- **AGENTS.md rewrite — one-file bootstrap.** The runbook now covers
  the full install path, not just AWS setup. Sections A–I: goal,
  preflight, Git-bootstrap (winget → portable), Python 3.13-bootstrap
  (winget → embeddable), git clone + venv + `pip install`, `.env`
  write with AWS discovery, hand-off message, HARD RULES, legacy-ZIP
  fallback. Deterministic anchors: absolute paths, pinned URLs,
  verify-after-each-step, HARD-STOP before every fallback.
- **`start.bat` — 3-layout auto-detect cascade.** Layout 1: packaged
  `python\python.exe` (ZIP). Layout 2: `venv\Scripts\python.exe`
  (git-clone). Layout 3: system `py -3.13` (dev). First that resolves
  wins. Old ZIP behavior is unchanged — Layout 1 still sets
  `FM_DATA_DIR=%LOCALAPPDATA%\FTNewsMonitor`.
- **`update.bat` — dependency refresh on requirements change.** Before
  `git pull`, hash `requirements.txt` via `certutil -hashfile … SHA256`.
  After a successful pull, hash again; if the hashes differ, run
  `venv\Scripts\pip.exe install -r requirements.txt` automatically.
  Prevents the classic "colleague updates → next `start.bat` throws
  ModuleNotFoundError" trap when Jordan adds a new dependency.
- **Docs realignment.** `QUICKSTART.md` and `UPGRADE.md` now present
  the AGENTS.md-based install as the primary path; the ZIP path is
  documented as the legacy alternative for colleagues without Claude
  Code. `ROLLOUT_RUNBOOK.md` §4.5 has the Teams-message template for
  the six-market rollout.

## Migration invariant

**Zero application-code changes.** No touch to `app/*.py`, no schema
migration, no wizard change, no `scripts/launch.py` change. This
release is purely distribution + bootstrap tooling.

Two behaviors are exploited (not modified):

1. `app/config.py::_resolve_data_dir()` — when `python\python.exe`
   does not exist next to `launch.py`, it falls back to the dev
   layout: data in `<repo>/data/`, `.env` next to the repo root.
   The git-clone install uses this path unchanged.
2. `app/setup_wizard.py::_current_state()` — reads
   `settings.aws_profile`, `.aws_access_key_id`, etc. from the loaded
   `.env` and prefills the wizard's Step 2. Writing `.env` correctly
   is sufficient to make the wizard show Step 2 green on first paint.

## Rollout for a new colleague (AGENTS.md path)

1. Jordan sends the colleague **one file** on Teams: `AGENTS.md`.
2. Colleague creates an empty folder (e.g. `Documents\FTNewsMonitor-bootstrap\`),
   drops the file in.
3. Colleague opens Claude Code (or Cowork) in that folder and types:
   *"Lies AGENTS.md und richte alles ein."*
4. Claude autonomously: checks CWD → installs Git (winget or portable)
   if missing → installs Python 3.13 if missing → clones repo to
   `%USERPROFILE%\FTNewsMonitor` → creates venv → `pip install` → finds
   AWS credentials on the laptop → writes `.env` → tells the colleague
   the exact next step.
5. Colleague double-clicks `%USERPROFILE%\FTNewsMonitor\start.bat`.
   Browser opens `http://127.0.0.1:8766/setup`. Wizard Step 2 is green.
6. Colleague clicks through Home-Markets, Seeds, Scheduler in the
   browser. Done.

## Rollout for a new colleague (legacy ZIP path)

Still works. `start.bat`'s Layout-1 branch is unchanged — the ZIP
extract still uses embedded Python, still writes data to
`%LOCALAPPDATA%\FTNewsMonitor\`. Use this for colleagues who can't
run Claude Code or Cowork.

## What is NOT shipping (deferred)

- **`docs/BOOTSTRAP.md`.** A human-readable version of AGENTS.md for
  colleagues without AI-assistant access was scoped and dropped —
  Jordan confirmed all six pilot colleagues have Claude Code/Cowork.
  If a future colleague needs it, the AGENTS.md content is easy to
  re-cast as a step-by-step human doc.
- **AWS credential rotation automation.** STS-temp keys still need to
  be refreshed manually via SSO login. Out of scope for a
  distribution-layer release.
- **Application-code refactor.** No changes to `app/*.py`. This is
  strictly additive.

## Costs

Unchanged from v0.6.1 at runtime. Zero extra AI tokens during operation.

One-time cost at install: the colleague's Claude Code / Cowork session
runs approximately 20–40 tool calls (winget checks, downloads, clone,
pip install, `.env` write). Rough token estimate: 30k–80k input/output
per install, one-time.

## Known quirks

- **Winget under AppLocker.** Corporate AppLocker rules can block
  `winget` even when it's installed. AGENTS.md handles this: sections
  C.2 and D.2 fall back to portable downloads (PortableGit,
  Python-embed) which unpack into `%USERPROFILE%\FTNewsMonitor-tools\`
  without needing installer privileges.
- **Corporate proxy TLS interception.** `git clone` may fail with
  `unable to get local issuer`. Fix in AGENTS.md E.2:
  `git config --global http.sslBackend schannel` switches Git to the
  Windows certificate store, which trusts corporate CA certs.
- **`py -3.13` not found immediately after `winget install`.** Path
  resolution needs a new shell. AGENTS.md instructs the assistant to
  open a new `cmd.exe` and retry the verify step.
- **`.env` at wrong location — LLM bias toward `%LOCALAPPDATA%`.**
  Older AGENTS.md always wrote there. Fresh git-clone installs need
  the `.env` at `%USERPROFILE%\FTNewsMonitor\.env` because
  `python\python.exe` doesn't exist, so `_resolve_data_dir()` picks
  the dev layout. Section F.1 has an explicit HARD CHECK; HARD RULE
  #2 forbids writing to `%LOCALAPPDATA%` in the git-clone case.
- **Cowork sandbox restrictions.** Claude Cowork may refuse to
  execute `.exe` downloads (e.g. PortableGit self-extract). AGENTS.md
  falls back to instructing the colleague to run winget manually if
  the AI's sandbox blocks it.

## Verification checklist (V1–V4 for pre-rollout dry run)

- **V1 — Dry run on Jordan's laptop.** Empty folder, drop AGENTS.md,
  open Claude Code, say "lies AGENTS.md und richte alles ein". Expected:
  preflight sees Git+Python already installed → skips C, D → clones →
  venv → pip install → `.env` → hand-off. Timing: 5–8 minutes.
- **V2 — Layout precedence.** Confirm `dir python\python.exe` in
  Jordan's dev tree returns nothing → `start.bat` picks Layout 2
  (venv). Data lands in `<repo>\data\`, not `%LOCALAPPDATA%`.
- **V3 — Wizard Step 2 green.** Write minimal `.env`
  (`AI_PROVIDER=bedrock`, `AWS_PROFILE=default`, `AWS_REGION=eu-west-1`).
  Run `start.bat`. Wizard Step 2 shows the AWS values prefilled — no
  red banner.
- **V4 — Update path with dep change.** In a cloned repo, run
  `update.bat` on a no-op commit → hash diff says "no change", pip
  refresh is skipped. Then bump `requirements.txt` upstream, push, run
  `update.bat` again → hash diff triggers `pip install`.

## Support

- Log file (packaged/ZIP layout): `%LOCALAPPDATA%\FTNewsMonitor\server.log`.
- Log file (git-clone layout): `%USERPROFILE%\FTNewsMonitor\data\server.log`.
- Rollout runbook: `docs/ROLLOUT_RUNBOOK.md` §4.5 (v0.7.0-specific).
- Legacy quickstart: `docs/QUICKSTART.md`.
- Contact **Jordan Kern** via Teams or email.
