# Pilot Rehearsal Checklist — FT News Monitor v0.1.0

Hand-executed on Jordan's laptop **before** the Guglielmo install
session. Confirms the pipeline works end-to-end with real Italy config.
Total wall time ≈ 25 minutes. On any failure, see
`ROLLOUT_RUNBOOK.md` §4 troubleshooting matrix, then
`%LOCALAPPDATA%\FTNewsMonitor\server.log`.

## A. Fresh install smoke (~10 min)

1. Extract `FTNewsMonitor-0.1.0-win64.zip` into a **scratch folder**
   (e.g. `C:\Temp\ftnm-pilot-rehearsal\`), right-click Unblock first.
2. Double-click `start.bat`. Wizard opens at `/setup`.
3. Complete the wizard with **Italy** + **EU** markets, real Italy AWS
   keys (1Password), shipped `italy` + `eu` seeds.
4. **Test AI now** → expect green line with model + latency ms.
5. **Finish and open dashboard**, then **Run collection**. Within
   ~5 min at least one Italian article should appear.
   - Fails → check `server.log` for AWS / feedparser errors.

## B. Export smoke (~3 min)

1. Filter: market=`Italy`, min_score=`6`, days=`7`.
2. **Download Markdown**: verify H1 date is `dd.mm.yyyy`, articles
   grouped `## Italy` (and `## EU`), ⭐ glyph on starred rows.
3. Paste MD into a scratch Confluence page — bullets render, links click.
4. **Download CSV**: open in Excel, confirm UTF-8 BOM detected (Italian
   `è`/`à` display correctly, not `Ã¨`).
5. **Copy as Markdown**: paste into Notepad, matches the MD.
   - Fails on Copy → confirm URL is `http://127.0.0.1` (Copy needs a
     secure context; falls back to a toast otherwise).

## C. Scheduler smoke (~3 min)

1. `/settings` → set daily time to **now + 2 minutes**, save.
2. Header chip "Next run" updates immediately (no restart needed).
3. Wait 2–3 min with app running → "Last run" chip flips to the fresh
   timestamp, log shows a scheduled run.
   - Fails → confirm `Europe/Rome` timezone chip on `/settings`.

## D. Feedback smoke (~2 min)

1. **⭐ Star** 3 articles, **Ignore** 2 (any topic).
2. Click **Run collection** again.
3. Filter status=`All`. Hover the score chip on a fresh article from a
   starred domain — the breakdown must show a `feedback_bonus`.

## E. Update-safety smoke (~3 min)

1. Close the terminal window (Ctrl-C).
2. Re-extract the ZIP **over the same folder** ("Replace files").
3. Relaunch `start.bat`. Starred articles from D are still starred — the
   DB lives in `%LOCALAPPDATA%\FTNewsMonitor\`, outside the extract.

## F. Setup-completed guard (~1 min)

1. With wizard done, navigate manually to `/setup`. It must **render**
   (not 302 to `/`) — the wizard is intentionally re-openable for
   later seed updates.

## G. Timezone sanity (~1 min)

1. `/settings` → confirm timezone is **`Europe/Rome`** (or the wizard's
   IT default for this install).
2. Header "Next run" tooltip shows a plausible Rome wall-clock time, not
   UTC.
   - Off → the wizard defaulted to `Europe/Berlin`; edit via `/settings`
     and save (scheduler reloads on save).

---

**Sign-off:** all seven sections green → release is pilot-ready. Send
the ZIP + `QUICKSTART.md` to Guglielmo, schedule the 30-min screenshare,
follow `ROLLOUT_RUNBOOK.md` §3.
