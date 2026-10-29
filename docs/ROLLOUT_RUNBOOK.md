# FT News Monitor — Rollout Runbook

Audience: Jordan (controller), or whoever inherits this rollout later.
This is the operational playbook for shipping a release ZIP to a new
market pilot. Everything below assumes the `market-edition` branch of
`foerder-monitor-market-edition` is checked out at
`c:\Tools\Jordan\foerder-monitor-market-edition`.

---

## 1. Build a release

### Prerequisites

- Python 3.14 (or 3.13) dev environment; build_release drives an
  embedded 3.13 target regardless of the driver Python version.
- Dev venv installed with **both** requirement files:

  ```powershell
  python -m venv venv
  venv\Scripts\activate
  pip install -r requirements.txt -r requirements-dev.txt
  ```

- All tests green:

  ```powershell
  venv\Scripts\python.exe -m pytest tests/ -q
  ```

  Do not build a release from a red suite. Fix the failure or park the
  test before proceeding.

- Seed validator green:

  ```powershell
  venv\Scripts\python.exe scripts\validate_seeds.py
  ```

  This is the offline validator (schema + enum values). The
  `--network` flag additionally fetches every RSS-Feed URL — run it
  manually before a release and delete or demote any feed that
  returns HTTP != 200 or 0 entries.

### Version and build

1. Edit `VERSION` at repo root if a version bump is needed. Format is
   plain `MAJOR.MINOR.PATCH` (currently `0.1.0`).
2. Build:

   ```powershell
   venv\Scripts\python.exe build\build_release.py
   ```

3. On success the script writes
   `dist\FTNewsMonitor-<VERSION>-win64.zip`. Expected size is 30–50 MB.
   The script fails loudly if the ZIP is < 20 MB (missing deps) or
   > 120 MB (crud crept in) — do not hand-edit the size thresholds; if
   they trip, investigate what changed.
4. Optional dry run inventory before committing to a build:

   ```powershell
   venv\Scripts\python.exe build\build_release.py --dry-run
   ```

If you want to timestamp the shipped file, rename the ZIP after the
build (e.g. append `_2026-07-15`). Do **not** rename inside the ZIP —
the extract folder name is used by the app to detect the packaged
layout (via the `python\python.exe` sibling).

---

## 2. AWS provisioning per user

The tool calls Anthropic Claude via **Amazon Bedrock cross-region
inference profiles**. Every pilot user needs their own AWS access key
pair, scoped as tightly as possible.

### IAM policy

Attach a policy that only grants `bedrock:InvokeModel` against the
specific Haiku 4.5 EU inference profile ARNs. Do **not** accept an
`AdministratorAccess`, `PowerUserAccess`, or full-Bedrock policy from
IT even if they offer it "for simplicity" — a leaked key at that
scope is a much larger blast radius than a leaked InvokeModel-only key.
The minimum needed is roughly:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel"],
    "Resource": [
      "arn:aws:bedrock:eu-west-1:*:inference-profile/eu.anthropic.claude-sonnet-5-5-*",
      "arn:aws:bedrock:eu-central-1::foundation-model/anthropic.claude-sonnet-5-5-*",
      "arn:aws:bedrock:eu-west-3::foundation-model/anthropic.claude-sonnet-5-5-*",
      "arn:aws:bedrock:eu-north-1::foundation-model/anthropic.claude-sonnet-5-5-*"
    ]
  }]
}
```

Refine the exact ARNs against `aws bedrock list-inference-profiles
--region eu-west-1` before signing off — the exact profile-ID suffix
varies by tenancy.

### Region

Default to `eu-west-1` (Dublin). The wizard also allows
`eu-central-1`, `eu-north-1`, `eu-west-3`. Match the region to the
inference profile the IAM policy grants — a mismatch is the most
common cause of "test connection" failures.

### Handover mechanism

Both the Access Key ID and the Secret Access Key are secrets. Do not
send them in a single email or Teams chat message. Two options in order
of preference:

1. **Password manager share** (1Password / Bitwarden / LastPass). One-
   time link per user, expires in 24 h. Preferred default.
2. **Sealed envelope through corporate IT**. Fallback when the user
   does not have a password manager provisioned yet.

The tool itself was reviewed against SSM Parameter Store as a third
option; SSM was ruled out for now because it requires the pilot user to
already have IAM console access to fetch the parameter, which most PA
colleagues do not have. **Runbook default: password-manager one-time
share.**

Never paste plaintext keys into a Teams DM, a Confluence page, or a
Notion doc.

### Budget alarm

Once the pilot user has been active for a few days, set a **CloudWatch
billing alarm** at `$50/month` scoped to the user's usage tag (or
account, if you provisioned dedicated). Opus 4.7 costs roughly 5x per
token what Haiku 4.5 does (input $5 / output $25 per 1M tokens vs
Haiku's $1 / $5), so the threshold sits above expected steady-state and
a trip flags a runaway loop or a stuck test-connection button. If the alarm fires, first check whether the scheduler is
enabled and misfiring; then check for a runaway manual run via the
header **Run collection** button.

---

## 3. Italy install session checklist (Guglielmo)

### Before the session

- Build the ZIP (see section 1).
- Provision his AWS key pair (section 2).
- Send the ZIP through Teams; send the keys via password-manager share.
  Send them separately so a compromised Teams account does not leak
  both halves.
- Send him `docs/QUICKSTART.md` so he can pre-read.

### During the session (30 min screenshare)

1. Walk him through Properties → Unblock → Extract All. Verify the
   `FTNewsMonitor-0.1.0` folder appeared with `start.bat` visible.
2. Double-click `start.bat`. Confirm terminal opens and browser lands
   on `/setup`.
3. Wizard step 1: `Italy` + `EU` ticked. Save.
4. Wizard step 2: paste keys (`AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY`), region `eu-west-1`, model
   `eu.anthropic.claude-sonnet-5-5`. Save.
5. Wizard step 3: click **Test AI now**. Wait for the green
   confirmation with model + latency. If red: check the region first;
   then re-verify the pasted key values did not gain a trailing space.
6. Wizard step 4: `Italy` and `EU` pre-ticked. Click **Import selected**.
   Confirm non-zero `keywords` / `feeds` / `outlets` inserted counts
   per market (Italy should show `keywords 29+ / feeds 12+ / outlets 18+`;
   EU should show `keywords 26+ / feeds 12+ / outlets 19+`).
7. Wizard step 5: tick the "Run collection automatically every day at"
   box, set `06:00`, click **Save schedule**.
8. Click **Finish and open dashboard** → dashboard loads with the header
   chip showing `Last run: never · Next: 06:00`.
9. Click **Run collection** in the header. First run takes ~5 min. When
   the "Run complete" toast appears, review the top few articles
   together. Star 2–3, ignore 2–3 — this seeds the feedback loop so
   he does not see a "no learning yet" state on day one.

### After the session

- Send him `docs/ADMIN_GUIDE.md` by email.
- Book a two-week follow-up. Agenda for the follow-up: review his
  starred set, look at feed health, discuss any keywords he wants
  added.

---

## 4. Troubleshooting matrix

| Symptom                                             | Likely cause                                                              | Action                                                                                                                     |
|-----------------------------------------------------|---------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------|
| Test AI red · `AccessDenied` / `AccessDeniedException` | IAM policy scoped to the wrong region or wrong inference-profile ARN.  | Compare the wizard's Region against the ARN region in the policy. Fix the policy; re-test.                                 |
| Test AI red · `SSLError` / `certificate`             | Corporate proxy re-signs TLS. `truststore` should handle it.              | Confirm the user has not cleared their Windows cert store recently. If yes, IT needs to re-push the internal root CA.       |
| Test AI red · `Could not connect to the endpoint`    | Bedrock not enabled in the requested region for their AWS account.        | Enable in the AWS console (Bedrock → Model access), or switch region.                                                       |
| "No feeds" — dashboard empty after first run         | Seed import step was skipped, or feeds are geoblocked from home network.  | Re-run wizard step 4. If still empty, run `scripts\validate_seeds.py --network` from inside the extract to probe live URLs. |
| Wizard loops back to `/setup` after Finish           | `%LOCALAPPDATA%\FTNewsMonitor\monitor.db` corrupted (OneDrive is a common cause). | Back up `.env`, delete `monitor.db`, restart `start.bat`. Move the extract folder off OneDrive.                             |
| Header chip `Last run: never` after schedule fires  | Terminal window was closed before the scheduled time.                     | Leave the terminal window open overnight, or use a Windows Task Scheduler entry to run `start.bat` at login.                |
| `A collection run is already in progress` (409)     | Manual run overlapped with the scheduled run.                             | Wait 2 min and retry. Only one run at a time is allowed by design (per-process lock).                                       |
| ZIP flagged by SmartScreen every launch             | `Unblock` step in Quickstart §2 was skipped, or antivirus quarantined the exe. | Right-click ZIP → Properties → Unblock and re-extract. Loop in local IT if the antivirus is deleting the extracted files.  |

If a symptom is not in the matrix, first grab
`%LOCALAPPDATA%\FTNewsMonitor\server.log` (rotates at 5 MB × 3 backups —
usually 1–3 days of history). The last 100 lines almost always show the
cause.

---

## 4.5. Distributing v0.7.0+ via AGENTS.md

Since v0.7.0, the default rollout path is **one file** on Teams —
`AGENTS.md` (~15 kB). The colleague's AI assistant (Claude Code or
Cowork) does all the install work: winget/portable bootstrap of Git
and Python 3.13, `git clone`, `pip install`, `.env` write. No more
ZIP mails per install, no more screen-share marathons.

### Pre-conditions on the colleague's laptop

- Claude Code CLI installed **OR** Claude Cowork Web/Desktop access.
- Corporate laptop with permission to install user-scope apps via
  `winget` (or, if blocked, permission to run portable executables
  under `%USERPROFILE%\`).
- A folder under `%USERPROFILE%\Documents\` (not OneDrive/SharePoint)
  where they can drop `AGENTS.md`.

### Teams-message template

Send the colleague this message plus `AGENTS.md` attached:

```
Hey <Name>,

kurze Anleitung für den News-Monitor:

1. Leg einen leeren Ordner an unter
   Dokumente\FTNewsMonitor-bootstrap\
   (NICHT in OneDrive oder SharePoint).

2. Speicher die angehängte AGENTS.md in dem Ordner.

3. Öffne Claude Code (oder Claude Cowork Web) und mach den Ordner
   zum Arbeitsverzeichnis.

4. Sag Claude: "Lies AGENTS.md und richte alles ein."

Claude installiert dann alles Nötige (Git, Python) und clont den
Monitor nach %USERPROFILE%\FTNewsMonitor\. Dauert 5–15 Minuten je
nachdem was schon installiert ist.

Am Ende sagt Claude Dir "Doppelklick auf start.bat". Dann öffnet
sich der Browser auf dem Setup-Wizard, AWS-Step sollte schon grün
sein. Home-Märkte / Seed-Packs / Scheduler klickst Du selbst im
Browser (5 Minuten).

WICHTIG: Klebe niemals AWS-Keys in den Claude-Chat. Er findet sie
selbst auf Deinem Laptop; falls nicht, ping mich für einen sicheren
Kanal.

Melde Dich falls was hakt.
Grüße
Jordan
```

### Push a new release for AGENTS.md-based installs

Same flow as §6b (git-based distribution), just simpler for
colleagues:

1. Bump `VERSION`, add CHANGELOG entry, commit.
2. Tag: `git tag v<version>`.
3. `git push origin market-edition && git push origin v<version>`.
4. Notify all colleagues (single Teams broadcast): *"neue Version
   verfügbar — Doppelklick auf update.bat im FTNewsMonitor-Ordner."*

`update.bat` handles both code pull **and** `pip install` when
`requirements.txt` changes (v0.7.0 hash-diff feature). No more
"colleagues get ModuleNotFoundError after update" traps.

### First-install verification (V1 dry-run)

Before the first colleague install, run V1 on your own laptop:

1. Create a temp empty folder (`Documents\ftnm-v070-test\`).
2. Copy `AGENTS.md` into it.
3. Open Claude Code in that folder.
4. Say: *"Lies AGENTS.md und richte alles ein."*
5. Watch for: preflight passes → clone → venv → pip install →
   `.env` at `%USERPROFILE%\FTNewsMonitor\.env` (not `%LOCALAPPDATA%!`)
   → hand-off message.
6. Double-click `start.bat` → browser → wizard step 2 green.

If any step trips, patch `AGENTS.md` and iterate. Do not roll out
until V1 is clean.

### Known limitations vs. ZIP path

- Colleague must have working Claude Code / Cowork access.
- First install takes longer than a ZIP extract (5–15 min vs.
  2 min) because of downloads.
- Claude may need to be re-prompted mid-flow if its sandbox refuses
  a specific step (typically an `.exe` download). AGENTS.md's HARD
  STOP messages tell the colleague what to do in that case.

### §4.5c Distribution via `install.bat` (no AI assistant, v0.7.5+)

For colleagues who only have **Claude Cowork** — which runs in an
isolated Linux sandbox and cannot access the Windows shell — send
`install.bat` from the repo root instead of `AGENTS.md`. It is
mechanically equivalent (same preflight, winget bootstrap, portable
fallbacks, `git clone`, `venv`, `pip`, AWS discovery, `.env` write)
but runs without an LLM.

Distribution message template:

```
Hey <Name>,

kurze Anleitung fuer den News-Monitor:

1. Speicher die angehaengte install.bat in einem leeren Ordner unter
   Documents\FTNewsMonitor-install\
   (NICHT in OneDrive oder SharePoint).

2. Doppelklick auf install.bat. Fenster oeffnet sich, laeuft 3-15 Min.

3. Wenn "Installation complete" steht: Doppelklick auf
   %USERPROFILE%\FTNewsMonitor\start.bat.

4. Browser oeffnet auf dem Setup-Wizard, AWS-Step ist gruen.
   Home-Maerkte / Seed-Packs / Scheduler klickst Du selbst
   (5 Minuten).

WICHTIG: Kleb niemals AWS-Keys in irgendeinen Chat. install.bat
findet sie selbst auf Deinem Laptop; falls nicht, meldet er sich.

Melde Dich falls was hakt.
Gruesse
Jordan
```

The installer's AWS discovery walks (in order):
1. `AWS_PROFILE` environment variable
2. `~/.aws/config` SSO profiles (only when SSO cache < 8h old)
3. `~/.aws/config` non-SSO profiles
4. `~/.aws/credentials` static-key profiles
5. `AWS_ACCESS_KEY_ID` environment variables

Every candidate is verified via `sts:GetCallerIdentity` before being
written — a stale SSO profile or a bad key never lands in `.env`.

Cowork users always hit path 2: Cowork itself signs in with AWS SSO,
so the SSO cache is guaranteed to be fresh.

Push the same release-tag flow as §4.5 (bump VERSION, tag, push,
notify). Colleagues on `install.bat` use `update.bat` for subsequent
updates — same as AGENTS.md users.

### §4.5d Cowork-only distribution (no AWS on the laptop, v0.7.6+)

For colleagues who have **Claude Cowork 3P** but **no AWS access at
all**, `install.bat` now offers a `[C]` mode. The tool still runs
locally, but the AI scoring is delegated to their Cowork session via
the shipped `ftnews` skill. No AWS creds, no Anthropic API keys.

Distribution message template:

```
Hey <Name>,

Anleitung fuer den News-Monitor (Cowork-Variante):

1. Speicher die angehaengte install.bat in einem leeren Ordner unter
   Documents\FTNewsMonitor-install\
   (NICHT in OneDrive oder SharePoint).

2. Doppelklick auf install.bat. Wenn sie fragt B/C, tippe C und Enter.
   Laeuft 3-15 Min.

3. Wenn "Installation complete (Cowork mode)" steht, mach das:
   - Oeffne Cowork
   - Settings -> Trusted Folders -> Add:
     %USERPROFILE%\FTNewsMonitor\data\
   - Settings -> Skills -> Import:
     %USERPROFILE%\FTNewsMonitor\skills\ftnews\

4. Doppelklick auf %USERPROFILE%\FTNewsMonitor\start.bat.
   Browser oeffnet sich, Setup-Wizard durchklicken (Home-Maerkte,
   Seed-Packs, Scheduler). AWS-Step ist gruen (nichts eintragen).

5. Sag Cowork: "News-Ranking laufen lassen".
   Cowork startet den Rest von selbst und meldet sich, wenn fertig.

Volle Anleitung: docs/QUICKSTART_COWORK.md
Melde Dich falls was hakt.
Gruesse
Jordan
```

Verification checklist for Jordan before rollout:

1. Run `install.bat`, pick `C`. Confirm `.env` gets
   `AI_PROVIDER=cowork` + `COWORK_API_TOKEN=<hex>`.
2. Confirm `data/cowork_token.txt` exists and matches the .env token.
3. Start `start.bat`. Dashboard shows the "N articles waiting for
   Cowork ranking" badge (initially 0).
4. Trigger a scrape via `POST /run-collection` (or the dashboard
   button). Confirm articles land with `ai_filtered=0`.
5. In Cowork, import the skill folder, ask *"News-Ranking laufen
   lassen"*. Confirm articles appear scored in the dashboard.
6. If the HTTP path fails (Cowork can't reach `127.0.0.1:8766`), the
   skill falls back to file-bridge — confirm the badge still clears
   after Cowork completes.

Trade-off: unlike Bedrock mode, ranking is not automatic. Colleague
must trigger it (or set up a weekly scheduled task inside Cowork —
see `docs/SKILL_INSTALL.md`).

---

## 5. Release cadence and seed refresh

Roughly every six weeks:

1. Ask each active market curator to send their `/admin/export-config`
   JSON download.
2. Fold their improvements into the shipped seed packs
   (`seeds/<market>.json`) — normally by comparing their export to the
   current shipped file and cherry-picking the additions.
3. **Bump `seed_version`** at the top of the modified seed file (v0.4.0+).
   Auto-reseed on the users' next startup only fires when the shipped
   version exceeds their `imported_seed_versions` marker. Forgetting the
   bump means the changes never propagate — the merge would still be
   idempotent, but the version check short-circuits before it runs.
4. Re-run `scripts\validate_seeds.py --network` on the updated packs.
   Delete or demote any feed that has died since the last release.
5. Bump `VERSION`, rebuild, and re-distribute to the pilot users. On
   their next launch, the auto-reseeder merges the additions into their
   local DB — user edits stay untouched. See `docs/UPGRADE.md` for what
   survives an upgrade.

Track defect reports either in a shared doc or as GitHub issues if the
repo lands on GitHub. Reference the specific version (`0.1.0`,
`0.1.1`, …) in every ticket so a user's log lines are matchable to
code.

---

## 6. Rollback

There is no shared server, no shared database, and no shared config —
each install is a self-contained folder plus a
`%LOCALAPPDATA%\FTNewsMonitor\` state directory. If a shipped ZIP has a
critical bug:

- Send the previous ZIP to the affected user(s).
- Ask them to extract to a **new** folder (do not overwrite the broken
  extract — leave it in place so you can grep the log if needed).
- Their existing `%LOCALAPPDATA%\FTNewsMonitor\monitor.db` and `.env`
  carry over automatically — the DB and secrets are outside the ZIP
  extract on purpose so a re-extract preserves state.
- Delete or archive the broken extract folder.

For a schema-level bug that has corrupted the DB itself, backing up
`monitor.db` first is worth 30 seconds — a rollback that also nukes the
user's stars and history is a much less pleasant conversation.

**Migration invariant.** Rollback is safe as long as migrations remain
**additive** — new columns / new tables only, driven by the idempotent
`ALTER TABLE ... ADD COLUMN` block in `app/database.py::init_db`. If a
future release **drops** or **renames** anything in `monitor.db` (a
column, a table, a value enum), the release note **must** include a
manual rollback step:
1. Stop the app on the affected machine.
2. Copy `%LOCALAPPDATA%\FTNewsMonitor\monitor.db` to `monitor.db.bak-<version>`.
3. Only then extract the older ZIP.

Without that dump, the older extract's schema expectations won't match
the on-disk state and it will fail to start, or worse, silently corrupt
rows.

---

## 6b. Git-based distribution (v0.6.0+)

Since v0.7.0 the repo lives at
`github.com/jordanthekern-lang/ft-news-monitor.git`, default branch
`main`, as a flat single-project layout (start.bat, requirements.txt,
app/ directly at root). Colleagues who have Git installed update via
`update.bat`; new installs are done via `AGENTS.md` + Claude
Code/Cowork (see §4.5).

The previous multi-project repo at
`github.com/jordanthekern-lang/ft-news-monitor-alt` (formerly
`Jordan`) is preserved for history reference — tags `v0.4.x`–`v0.6.1`
and legacy sibling projects (`foerder-monitor/`, `foerder-monitor-bedrock/`)
live there. Not touched by v0.7.0+ pushes.

**How to push a new release (Path A, code-only):**

1. Bump `VERSION`, add CHANGELOG entry, commit.
2. Tag: `git tag v<version>`.
3. `git push origin market-edition && git push origin v<version>`.
4. Notify colleagues on Path A: "run `update.bat`". No ZIP handoff.

**When Path A is not enough:**

- New Python dependency in `requirements.txt` → `lib/` needs a re-vendor
  → colleagues need Path B (new ZIP).
- Python interpreter version bump → `python/` needs re-vendor → new ZIP.
- Otherwise (code-only, seed-only, doc-only): `update.bat` suffices.

**First install for a Git-capable colleague** — see
`docs/CONTRIBUTING.md#path-a-direct-git-clone-dev-style`. Most FlixBus
colleagues don't have Git on their corporate laptop, so this remains
the exception. When rolling out to a new market, default to Path B for
the first install; if the colleague later installs Git, they can
migrate to Path A by turning the extract folder into a clone
(`git init && git remote add origin ... && git fetch && git reset
--hard v<version>`).

**Repo hygiene before pushing:**

- Never commit `.env`, `data/*.db*`, or ZIPs from `dist/`. The
  `.gitignore` covers them; verify with `git status` before committing.
- Run `git log --all -p | Select-String "aws_secret|access_key"` before
  the first push to a public remote; must be empty.

---

## 7. Key files and paths

| Path                                                                     | Purpose                                                                    |
|--------------------------------------------------------------------------|----------------------------------------------------------------------------|
| `VERSION`                                                                | Single source of truth for the release version (read by build_release).   |
| `requirements.txt`                                                       | Runtime deps vendored into the ZIP.                                        |
| `requirements-dev.txt`                                                   | Test-only deps (`pytest`, `respx`) — not shipped.                          |
| `build\build_release.py`                                                 | ZIP builder. Idempotent.                                                   |
| `scripts\launch.py`                                                      | The startup wrapper `start.bat` invokes.                                   |
| `scripts\validate_seeds.py`                                              | Offline + `--network` seed validator.                                      |
| `app\config.py::_resolve_data_dir`                                       | Where the DB / .env / logs live. Dev: `data/`. Packaged: `%LOCALAPPDATA%\FTNewsMonitor`. |
| `%LOCALAPPDATA%\FTNewsMonitor\monitor.db`                                | User's SQLite DB (articles + feedback + settings).                         |
| `%LOCALAPPDATA%\FTNewsMonitor\.env`                                      | User's wizard-written secrets.                                             |
| `%LOCALAPPDATA%\FTNewsMonitor\server.log`                                | Rotated log (5 MB × 3 backups).                                            |
| `dist\FTNewsMonitor-<VERSION>-win64.zip`                                 | The artefact you ship.                                                     |
