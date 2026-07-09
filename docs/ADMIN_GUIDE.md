# FT News Monitor — Admin Guide

Audience: the Public Affairs colleague who curates seed data for their
market. You know your industry vocabulary in-country; you are not a
developer. This guide covers what each admin table means, how to tune
the tool, and how to send your improvements back to Jordan.

If you have not run the first-time setup yet, start with
`QUICKSTART.md`.

---

## Concepts

The tool has four kinds of configuration you edit through the browser:

**Keywords** — search terms the collector uses to build Google News and
Bing News queries every day. A keyword has:

- a **search term** (the word or phrase),
- a **language** (`IT`, `EN`, `FR`, …) — this decides which language
  edition of Google/Bing News is queried,
- optional **markets** (Italy, EU, …) — if empty, the keyword is
  considered "global" and used in every home-market run,
- a **keyword priority** (Low / Medium / High) — controls whether the
  keyword is queried and whether its tokens gate the pre-screen. **High**
  = the keyword is included in daily collector queries **and** its tokens
  feed the pre-screen (articles must contain at least one High-priority
  token to reach the AI). **Medium** = included in collector queries but
  **not** in the pre-screen token set. **Low** = silently skipped — not
  queried and not pre-screened. (Trust bonus is a separate, outlet-side
  mechanism — see **Outlets** below.)
- optional **themes** and **notes**.

**Combo keywords** use `+` to require two tokens on the same result.
`autocar+subvention` means "must contain both". Use these to sharpen
noisy words. Example: `autocar` alone gives ~200 hits/day (many
off-topic coach-tour ads); `autocar+subvention` gives ~5 hits/day of
actual funding news.

**Feeds** — RSS feeds (or Google News query URLs) polled directly. A
feed has a **type** (`RSS-Feed`, `Google News Query`, or
`Website-Scrape`), an **URL or query**, and the same market / language /
priority / theme fields as keywords. For every `Google News Query` row,
the collector also queries Bing News automatically — no separate feed
row is needed. Feeds that fail on the last run are counted in the
"Failing on last run" tile on the dashboard.

**Outlets** — trusted publisher records. When an article's URL matches
a known outlet's domain, the article gets a **trust bonus** on top of
the AI's relevance score — this is what shows up as `(+X trust)` on the
dashboard score chip. **Outlet priority** (Low/Medium/High) controls the
size of that bonus. Use outlets to reward reliable sources without
changing the model. (Note: outlet priority and keyword priority are
independent — they live on different tables and do different things.)

**Excludes** (negative keywords) — plain substrings. Any article whose
title or snippet contains a negative keyword is dropped in the pre-filter
step, never reaches the AI, and does not cost tokens. Use for
"tourism-coach", "wedding rental", competitor product press releases,
and other reliably-off-topic terms.

**Markets** are `DACH`, `Benelux`, `France`, `Italy`, `Nordics`,
`Portugal`, `EU`. Rows with an empty markets list apply to every home
market. Rows tagged with a specific market only run when that market is
in the dashboard's home-markets setting.

---

## The `/admin/` pages

Four browser pages, one per table:

- `/admin/keywords`
- `/admin/feeds`
- `/admin/outlets`
- `/admin/negative-keywords`

Each page shows a filterable list. The **Market** dropdown at the top
narrows to rows tagged with the chosen market (plus untagged global
rows). Every row has **Edit** and **Delete** buttons.

**Adding a keyword.** Go to `/admin/keywords → + Add keyword`. Fill in
the search term and language; tick your market(s); pick a priority;
save.

**Verifying it worked.** After the next collection run (either the
scheduled fire or the header **Run collection** button), open the
dashboard, filter by that market plus your minimum score, and skim the
top results. If nothing shows up, try:

- lowering the minimum score to 3 or 4 on the dashboard,
- widening the time window to 60 or 90 days,
- adding a variant (singular / plural / synonym) and re-running.

**Importing a seed pack file.** The first-run wizard imports the shipped
seed pack for your market once. If Jordan sends you an updated pack
later (e.g. after folding in your improvements), you can re-import via
the wizard: open `/setup`, jump to step 4 ("Import seed packs"), tick
the markets, click Import. Existing rows are preserved — the merge is
safe to run twice.

---

## Recall knobs

Three settings on `/settings` shape how much you see:

**Default min. score.** The dashboard opens with this score threshold.
Six is a good default — most operators leave it there. Drop to 4 or 5
when scouting a new topic; raise to 8+ for signal-only weeks.

**Pre-screen bypass days.** Articles newer than this many days always
go to the AI, even if the keyword pre-screen would drop them. Keeps
recent-but-fringe topics from being silently filtered. Default is 7 days
— leave it unless the pipeline is producing too few articles overall.

**Home markets.** Ticked here decides which markets the daily pipeline
scopes. Leave `EU` on if you want European-level coverage alongside
your home market.

---


## Historical backfill (v0.5.0)

Regular collection runs use a **3-month** time-window so each daily
pull is fast and cheap. On a fresh install (or after adding a large
batch of new keywords) that 3-month window misses everything older.
Run the backfill script **once** to populate historical article
bestand back to 5 years:

```
python scripts/backfill.py --window 5y
```

Options:

- `--markets Italy,EU` — restrict to specific markets (default: home markets).
- `--dry-run` — collector only, skip AI (saves Bedrock cost).
- `--window 1y` (or `3m`, `6m`, ...) — narrower sweep if 5y is overkill.

The script uses the same lock as scheduled runs, so calling it while
the scheduler is firing safely returns `already_running`. Runtime:
about 15-30 minutes for a full DACH+EU catalogue (92 keywords × 3
aggregators). Regular daily runs are **not** affected — they continue
to use the 3-month default from `COLLECTOR_TIME_WINDOW`.

## Source health

The **Sources** card on the dashboard sidebar shows how many feeds
answered on the last run:

- **Active** — feeds that returned at least one entry.
- **Failing on last run** — feeds that returned 0 entries or errored.

Non-zero "Failing" for two or three consecutive days means a feed URL
has gone stale (typical: the outlet moved from `/rss` to `/feed.xml`,
or shut the feed off entirely). Options:

- **Update** the row on `/admin/feeds → Edit` with the new URL.
- **Demote** the row to Low priority if you want to keep it around but
  stop trusting it.
- **Delete** if the outlet is definitively gone.

Ask Jordan before deleting a feed that came from the shipped seed pack
— chances are other markets rely on the same URL.

---

## Feedback loop

Every star and ignore click feeds two mechanisms:

- **Prompt few-shots.** Recent stars and ignores are shown to Claude as
  examples on the next scoring run. This shifts what the model
  considers relevant for your market — no config change required.
- **Domain adjustment.** From three clicks per domain onwards, the score
  is auto-adjusted by −2 to +2 based on the ratio. A domain you keep
  starring will drift upward; a domain you keep ignoring will drift
  downward. The current adjustments are visible under
  `/settings → Learning from your clicks`.

Star aggressively for the first two weeks — it is the fastest way to
teach the tool your market's tone.

---

## Exporting your config back to Jordan

`/admin/export-config` downloads your entire local configuration
(keywords + feeds + outlets + excludes) as a JSON file named
`config-export_YYYY-MM-DD.json`.

The file is in the same format as the shipped seed packs — Jordan can
merge it directly into `seeds/<market>.json` for the next release. When
you have accumulated ~10 curator improvements you consider worth
sharing, email the export file to Jordan.

## What happens when a new version arrives

Extracting a new ZIP over the existing folder never touches your DB or
`.env`. On the first launch of the new version:

- If the shipped `seeds/<your-market>.json` files declare a higher
  `seed_version` than what your install has already imported, the new
  rows are merged in automatically. Nothing you have added or edited is
  overwritten.
- The startup log at `%LOCALAPPDATA%\FTNewsMonitor\server.log` shows a
  line like `Auto-reseed: dach v2 applied (previous v1)` the first time.
- Everything else — dashboard content, golden labels, stars, runtime
  settings — is exactly as you left it.

If you want to prevent auto-reseed for a specific market (e.g. because
you've curated it into a very different direction than the shipped
pack), remove the market from your Home markets list in Settings. The
auto-reseeder only touches seed packs for markets that are currently in
Home markets.

---

## What NOT to do

- **Renaming a keyword only affects future runs.** Renaming changes the
  queries the collector fires and the tokens the pre-screen accepts;
  already-collected article rows are untouched (there is no
  keyword-to-article back-reference in the DB). If a rename is really a
  concept change rather than a typo fix, prefer **delete + add** so the
  old row's history stays legible in your seed export.
- **Do not commit AWS keys anywhere.** The wizard writes them to a
  local `.env` file; nothing else needs them.
- **Do not delete a feed simply because it produced no hits on a quiet
  day.** Check the "Failing on last run" tile for three consecutive
  days before assuming a source is dead.
- **Do not paste comma-separated keywords into a single row.** Each
  keyword is one row — the pipeline sends one query per keyword. If
  you want an OR condition, add multiple rows.
- **Do not enable a market you do not staff.** The tool queries the
  outlet feeds for every ticked home market on every run; unstaffed
  markets pull in noise nobody reviews.

---

## Reaching Jordan

For anything that feels like a bug (wrong article showing up, feed
counts wildly off, the AI-test button going red suddenly), zip up
`%LOCALAPPDATA%\FTNewsMonitor\server.log` and send it along with a
one-line description. Do not send screenshots of the dashboard —
Jordan cannot reproduce a state from a screenshot.
