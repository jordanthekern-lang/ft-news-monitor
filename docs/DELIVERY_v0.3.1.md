# FT News Monitor v0.3.1 — Delivery Note

**Release:** v0.3.1 · **Build date:** 2026-07-08 · **Target:** land four
Settings-page usability improvements Jordan asked for after live-testing
v0.3.0, plus two bug fixes surfaced by the v0.3.0 adversarial review.

## What is new since v0.3.0

- **Nav rename: "Diagnostics" → "Settings".** The page itself is still
  titled "Settings and Diagnostics" (it hosts both), but the nav link
  now advertises the more useful side of it.
- **Toggle Claude keyword suggestions from the UI.** Previously
  required editing `SUGGEST_KEYWORDS=true` in `.env`. Now a checkbox
  under Settings → Preferences flips it live. Resolution order is
  `runtime setting > env flag > default off` — the env flag stays as
  a CLI escape hatch.
- **Translation targets follow home markets.** Instead of the
  hard-coded DE/EN skip-list, the translator derives target languages
  from `home_markets` (Portugal → keep PT + EN, Nordics → keep
  SV/NO/DA/FI + EN, DACH → keep DE + EN, …). A manual override in
  Settings lets a colleague say "actually translate DE for me too".
- **Change the Outlook mailbox from Settings.** A new Outlook block on
  `/settings` posts to the same validated `/setup/outlook` route as
  the wizard. Includes a **Test connection** button that opens the
  folder via COM, counts items, and shows a green (`✓ folder · N
  items · Ms ms`) or red (`✗ folder not found. Siblings: …`) badge.
  Same button also lands in the wizard step 6.

## What is fixed

- **Silent `items.Sort()` failure could drop newer newsletters.** In
  v0.3.0, if Outlook rejected the sort call the loop fell back to
  insertion order and the age-cutoff `break` fired at the first old
  item — silently skipping any fresh newsletters after it. Now
  tracked with a `sorted_ok` flag: on sort failure we log a warning
  and use `continue` so old items are skipped but fresh ones still
  ingest.
- **`ReceivedTime` was tagged UTC when it's actually local
  wall-clock.** A 23:30 Berlin newsletter previously appeared on the
  next day in the dashboard. Now the poller reads the local zone from
  `TIMEZONE`/`FT_TIMEZONE` (falling back to the OS local tz) and
  converts to UTC before insertion.

## Migration invariant

Zero schema changes. New `runtime_settings` keys are added lazily on
first write (`suggest_keywords_enabled`, `translation_target_langs`).
Existing `.env` files are untouched. `%LOCALAPPDATA%\FTNewsMonitor\monitor.db`
from any prior 0.x version opens under v0.3.1 without conversion.

## Rollout for Jordan

1. `dist\FTNewsMonitor-0.3.1-win64.zip` over the existing folder.
2. Right-click → Properties → Unblock → OK, then Extract All.
3. Double-click `start.bat`. Dashboard renders as before.
4. Head to `/settings`:
   - Optionally tick **"Let Claude propose new keywords while scoring
     articles"** to enable F2 without touching `.env`.
   - Under **Translation targets**, leave it on "Auto" (uses your
     home markets) unless you want a specific override.
   - Under **Newsletter mailbox (Outlook)**, click **Test connection**
     to verify the current path still resolves. If not, edit the
     folder path and test again before saving.

## Rollout for existing v0.1.0 / v0.2.0 / v0.3.0 users

Zero-impact upgrade. All new features are default-off or default-safe:

- `SUGGEST_KEYWORDS` in `.env` still works exactly as before.
- The translator's skip-list falls back to the historical DE/EN default
  when no home markets are configured (fresh installs pre-wizard).
- The Outlook block on `/settings` is optional — the wizard remains the
  first-run path.

## What is NOT shipping (deferred)

- CSRF tokens on admin POSTs (same posture as v0.3.0 — localhost only).
- Persisted "last connection test" badge on the Outlook block (each
  page load re-tests would be too expensive; the button is on-demand).
- OAuth-based Microsoft Graph fallback (unchanged from v0.3.0 stance).

## Costs

Unchanged from v0.3.0.

## Known quirks

- **Manual translation override in Nordics.** `MARKET_TO_LANGS` maps
  Nordics to `{SV, NO, DA, FI}`. That's the correct skip-list for a
  colleague there. If a German article slips into their feed and they
  want it translated to English, they can either leave it (Claude
  reads DE fine) or switch to Manual mode and remove DE from the
  skip-list.
- **Test-connection button doesn't save.** The button dry-runs the
  form's current values without persisting them — click Save Outlook
  settings afterwards to actually commit the change.

## Support

- Log file: `%LOCALAPPDATA%\FTNewsMonitor\server.log`
- Eval artefacts: `%LOCALAPPDATA%\FTNewsMonitor\eval_runs\`
- Contact **Jordan Kern** via Teams or email.
- Troubleshooting matrix: `ROLLOUT_RUNBOOK.md` §4.
