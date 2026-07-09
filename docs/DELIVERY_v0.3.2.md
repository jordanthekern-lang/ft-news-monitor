# FT News Monitor v0.3.2 — Delivery Note

**Release:** v0.3.2 · **Build date:** 2026-07-08 · **Target:** three
tightly-coupled newsletter improvements that make Outlook-sourced hits
first-class citizens in the dashboard.

## What is new since v0.3.1

- **Newsletter pre-screen reads the mail body.** Previously the
  pre-screen only saw the subject line — newsletters with generic
  subjects ("Weekly digest") but strong body signal fell out
  silently. Now the first ~1500 chars of the body land as
  `articles.snippet` and reach the keyword pre-screen and Claude.
- **Newsletter articles are visually identified.** Purple `✉ Newsletter`
  chip on dashboard cards and detail pages. Clicking the card title
  now opens the article detail (not `art.url`, which is just the
  first body link — often a landing page, not the mail).
- **"Open in Outlook" button.** Both on dashboard cards and prominently
  on article detail pages. Uses Outlook-COM (`GetItemFromID` +
  `.Display`) to pop the original mail open in the running Outlook
  window. Read-only against the mailbox.

## Migration invariant

Two nullable columns added to `articles` (`outlook_entry_id`,
`outlook_store_id`). Additive-only, migration is idempotent via the
existing `_ensure_column` pattern. `%LOCALAPPDATA%\FTNewsMonitor\monitor.db`
from any prior 0.x version opens under v0.3.2 without conversion.

Newsletter rows ingested with v0.3.0 / v0.3.1 have `NULL` for the new
columns. Clicking "Open in Outlook" on those returns a hint string with
the mail subject so the user can find it manually via Outlook's search.
No forced migration; the columns fill in over time as new mails arrive.

## Rollout for Jordan

1. `dist\FTNewsMonitor-0.3.2-win64.zip` over the existing folder.
2. Right-click → Properties → Unblock → OK, then Extract All.
3. Double-click `start.bat`. Dashboard renders as before — newsletter
   cards now show a purple `✉ Newsletter` chip and an
   `✉ Open in Outlook` action button.
4. Trigger **Run collection** on the dashboard. The next batch of
   newsletters is ingested with body-snippet and EntryID; clicking
   Open in Outlook on those opens the original mail.
5. Outlook must be running for the "Open in Outlook" button to work.
   If Outlook is closed, the toast surfaces a clear error message.

## Rollout for existing v0.1.0 / v0.2.0 / v0.3.0 / v0.3.1 users

Zero-impact upgrade. All changes are additive:

- Non-newsletter articles behave exactly as before — no chip, no button,
  no template change.
- Newsletter rows from prior versions get the "no EntryID stored" hint
  when the button is clicked, not a hard error.
- The pre-screen change makes more newsletters survive to Claude; this
  is a net-positive precision/recall shift, not a regression.

## What is NOT shipping (deferred)

- Force-migrate old newsletter rows to fetch EntryIDs retroactively.
  Would require re-polling every folder and heuristically matching
  subjects — not worth the code complexity for a self-healing
  problem.
- Newsletter-specific negative keywords (still uses global negkw only
  because `country=None` for newsletter rows). Documented asymmetry
  inherited from v0.2.0 F3.
- Outlook shared-mailbox open (only the primary mailbox's folder is
  polled today).

## Costs

Unchanged from v0.3.1.

## Known quirks

- **EntryID can change when a mail is moved between folders/stores.**
  The stored ID then points to nothing; Open in Outlook returns
  "may have been moved". This is expected COM behaviour.
- **HTML body → text extraction is regex-based**, not bs4. 99% of
  newsletters are clean-enough HTML that a `<tag>` strip + entity
  unescape produces useful pre-screen text. Malformed HTML may leak
  stray fragments — harmless, just noisier snippets.
- **Body-snippet cap is 1500 chars.** Enough for the pre-screen and
  the AI-scoring prompt; longer bodies are truncated with no ellipsis.

## Support

- Log file: `%LOCALAPPDATA%\FTNewsMonitor\server.log`
- Eval artefacts: `%LOCALAPPDATA%\FTNewsMonitor\eval_runs\`
- Contact **Jordan Kern** via Teams or email.
- Troubleshooting matrix: `ROLLOUT_RUNBOOK.md` §4.
