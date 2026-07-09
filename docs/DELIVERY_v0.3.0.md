# FT News Monitor v0.3.0 — Delivery Note

**Release:** v0.3.0 · **Build date:** 2026-07-08 · **Target:** replace
the v0.2.0 IMAP poller (blocked by FlixBus tenant policy) with a
COM-based Outlook desktop-app integration that needs no password, no
OAuth, and no IT ticket.

## What is new since v0.2.0

- **Newsletter ingest via Outlook-COM (F3.5).** Reads a single named
  folder from your locally running Outlook desktop app. Setup wizard
  step 6 asks for a folder path (e.g. `Posteingang/BT Verkehr`) and
  optionally a store name. No password. No OAuth. Uses the same
  Outlook session you're already logged into.
- **Diagnostic script `scripts/outlook_list_folders.py`** — prints
  every folder path in your Outlook so you can copy the exact name.

## What is removed

- **F3 IMAP poller.** `app/imap_poller.py`, the IMAP wizard fields, the
  `IMAP_*` env vars, and `scripts/imap_list_folders.py` are gone.
  Existing `.env` values are left in place but ignored.

## Migration invariant

Zero schema changes. The `source_id` in `source_health` shifts from
`imap:INBOX` to `outlook:<folder_path>` but that's a data value, not a
column. `%LOCALAPPDATA%\FTNewsMonitor\monitor.db` from v0.2.0 opens
under v0.3.0 without conversion.

## Rollout for Jordan

1. `dist\FTNewsMonitor-0.3.0-win64.zip` over the previous folder.
2. Right-click → Properties → Unblock → OK, then Extract All.
3. Double-click `start.bat`. Dashboard renders as before.
4. Wizard step 6:
   - Tick **Enable Outlook newsletter ingest**.
   - **Folder path**: `Posteingang/BT Verkehr` (or whatever your Outlook
     folder is called). If unsure, run
     `venv\Scripts\python.exe scripts\outlook_list_folders.py` and copy
     an exact line from its output.
   - **Store**: leave empty unless Outlook has multiple accounts.
   - **Save Outlook settings**.
5. Trigger **Run collection** on the dashboard. If Outlook shows a
   security popup asking to allow programmatic access, click *Allow for
   10 minutes*.
6. Verify: filtered `/dashboard` shows articles with
   `source_type='newsletter'`; `/settings` under Source health shows
   `outlook:<folder>` with a recent last_success timestamp.

## Rollout for existing v0.1.0 / v0.2.0 users

- **Guglielmo (v0.1.0 pilot)**: Zero-impact. He never enabled F3, so
  the removal of IMAP is invisible. When he wants newsletter ingest,
  same wizard flow as above.
- **v0.2.0 users who tried F3-IMAP** and didn't get an App-Password:
  their old IMAP settings in `.env` are harmless — they'll just be
  ignored. Run through the new wizard step 6 to switch to Outlook-COM.

## What is NOT shipping (deferred)

- CSRF tokens on admin POSTs (same posture as v0.2.0 — localhost only).
- OAuth-based Microsoft Graph fallback (would still need IT to
  register an app in Azure; not urgent since Outlook-COM works without).
- SSRF hardening on redirector-unwrap (still not reachable in
  production — the caller passes `follow_redirects=False`).
- URL canonicalisation applied on the RSS/Google-News insert paths
  (currently only IMAP had it; RSS side stores raw URLs).

## Costs

- F1: ~$0.30 per 100 labeled articles on Haiku 4.5, on-demand.
- F2: ~$25/year at 500 articles/day. Off by default.
- F3.5 (Outlook): zero AWS spend; reads local Outlook only.

## Known quirks

- **Outlook must be running.** If closed at collection time, the
  Outlook step fails, gets logged to `source_health`, and the rest of
  the pipeline continues normally. Next run picks up the missed
  newsletters.
- **First-run trust popup.** Click "Allow for 10 minutes". On hardened
  tenants Group Policy may block this outright — rare.
- **Folder-name language:** German Outlook = `Posteingang`, English =
  `Inbox`. The path must match your Outlook UI. Use
  `scripts/outlook_list_folders.py` if unsure.
- **CSV `relevance_score` column** still contains the composite score
  (raw + trust + feedback). Rename to `final_score` still parked.

## Support

- Log file: `%LOCALAPPDATA%\FTNewsMonitor\server.log`
- Eval artefacts: `%LOCALAPPDATA%\FTNewsMonitor\eval_runs\`
- Contact **Jordan Kern** via Teams or email.
- Troubleshooting matrix: `ROLLOUT_RUNBOOK.md` §4.
