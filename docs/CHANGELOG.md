# Changelog — FT News Monitor

All notable changes to this project are documented here.
Format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

## [0.7.4] — 2026-07-27

Live progress bar in the dashboard. Runs used to disappear behind a
"Running…" button for 5–10 min with no visible state; users couldn't
tell whether the collector was working, stuck on GDELT retries, or
had actually crashed. The bar now shows the current phase
(Collecting / Prefilter / Translating / AI scoring), the sub-step
inside that phase ("Keywords 47/134: Wasserstoffbus"), and elapsed
seconds.

### Added

- **`app/progress.py`** — module-level singleton state, best-effort
  telemetry (never raises, never blocks the pipeline). Reset on every
  `start_run`; snapshot() reads a JSON-serialisable dict.
- **`GET /pipeline-status`** — 200 always (also when idle), returns
  the snapshot so the dashboard poller doesn't need to special-case
  HTTP status codes.
- **Progress strip under the header** (`app/templates/base.html`) —
  hidden by default; opens on "Run collection" click and also
  auto-detects an in-flight run on page load (scheduler-triggered,
  second tab). Polls every 2 s.
- **10 new tests** covering the reporter's phase/step/error paths and
  the `/pipeline-status` endpoint (idle and running snapshots).

### Wired in

- `app/pipeline.py::run_pipeline` calls `start_run`, `set_phase` for
  each of the four stages, and `finish_run` (also on the exception
  path).
- `app/collector.py::run` reports per-feed/keyword/outlet sub-steps.
  Keywords carry a `_progress_done` / `_progress_total` offset so the
  bar shows run-wide progress ("47/199") not just per-phase.

### Not changed

- The pipeline lock, cross-process lock, and `/run-collection`
  contract stay identical.
- The bar uses phase weights (Collect 55 % / Prefilter 5 % /
  Translate 15 % / AI scoring 25 %) — not a real ETA, just smooth
  motion. When actual timing on a colleague's install proves the mix
  is off, tweak `PHASE_WEIGHTS` in `base.html`.

## [0.7.3] — 2026-07-27

GDELT default flipped to off. Measurement on Jordan's live DB (30-day
window): GDELT contributed 280 URLs no other source saw — but 0 of
them scored ≥6 in the AI filter. All 280 were noise the pipeline
throws away anyway. Meanwhile GDELT's 429 backoff was eating ~30
minutes per run (v0.7.2's throttle helped but didn't rescue the ratio).

With the v0.7.0 distribution model where each colleague installs
locally, Google News + Bing already run from the right country and
return local-language content directly. GDELT's original v0.5.0
rationale (fill in Italy/Portugal coverage when the crawler runs from
a DE laptop) doesn't apply anymore.

### Changed

- `app/collector.py::_gdelt_enabled` default flipped from `true` to
  `false`. Existing installs pick this up automatically on the next
  `update.bat` — no `.env` edit needed.

### Kept

- The `GDELT_ENABLED=true` env flag still works. If a specific market
  turns out to have thin Google/Bing coverage (e.g. Nordics small-town
  press), re-enable per install.
- The v0.7.2 throttle + backoff logic stays in place — if you flip
  the flag back on, the collector still won't burn Bedrock budget on
  429 storms.

### Expected impact

- Runs drop from ~35–40 min to ~5–10 min for a DACH+EU install.
- Zero loss of relevant articles based on the 30-day measurement.

## [0.7.2] — 2026-07-27

Collector fixes on top of v0.7.1. Live run on 2026-07-27 showed 100 %
of Google News calls returning `503` and ~90 % of GDELT calls returning
`429`, silently wiping ~90 keyword sweeps from the run. Only Bing and
the curated RSS feeds were still contributing hits. Also finishes the
Google-News-side dedup that v0.7.1 had explicitly deferred.

### Fixed

- **Google News 503 across the board** (`app/collector.py`). The v0.7.0
  User-Agent (`Mozilla/5.0 (Foerder-Monitor; FlixBus PA) Python/httpx
  feedparser`) tripped Google's bot filter. Verified with `curl` on
  2026-07-27: any UA that self-identifies as a script gets 503; a
  regular Chrome UA gets 200. Swapped to a stock
  `Mozilla/5.0 (Windows NT 10.0; …) Chrome/128 …` string with an
  `X-Contact: FlixBus PA foerder-monitor` header for outlet operators
  who inspect their access logs.
- **GDELT 429 storm** (`app/collector.py`). New `_fetch_gdelt_url`
  wrapper serialises GDELT calls (asyncio.Semaphore(1)), enforces a
  5.5 s minimum inter-request interval, and retries once with
  exponential backoff on `429`/`503`. Non-retryable errors (500s,
  network) still short-circuit to `None` — callers already handle that.

### Added

- **Batched async Google-News unwrap** (`app/url_canon.py`,
  `app/collector.py`). New public `batch_unwrap_google_news(client,
  urls)` coroutine runs up to 10 concurrent HEAD requests on the
  caller's existing `httpx.AsyncClient`, deduplicating identical
  wrappers so each is probed once. The collector calls it after every
  Google-News RSS fetch and hands the resulting `{wrapper: publisher}`
  map into `_store_entries`, so different keyword queries returning the
  same underlying story collapse via `UNIQUE(url)`. This finishes the
  Google-News side of the v0.7.1 dedup work that had been explicitly
  deferred.
- **10 new tests** covering the batch unwrap (5), GDELT throttle +
  backoff (4), and a UA-regression guard (1). All 418 pass.

### Rollout notes

- No schema change. Path A (`update.bat` git-pull) works.
- Wall-clock impact: GDELT throttle adds ~5.5 s per keyword query. For
  90 keywords that's ~8 minutes of GDELT wait, but previously ~85 of
  those 90 calls were 429-failing anyway — net effect is more articles
  in exchange for a slower run. If it becomes a problem in the pilot,
  set `GDELT_ENABLED=false` in `.env` to skip GDELT entirely.
- Batched Google-News unwrap adds ~1–2 s per keyword query (10-way
  parallel HEAD across ~10 wrapper URLs). Cheap compared to the RSS
  fetch itself.

## [0.7.1] — 2026-07-09

Duplicate-hunt release. On Jordan's first live install v0.7.0 the DB
had grown to 3157 rows, of which 1308 (41%) were the same article
under different aggregator wrapper URLs. Dashboard search results
showed the Windrose story 32 times, one Fahrverbot piece 20 times,
etc. All rooted in Bing News' ``apiclick.aspx?url=<real>&tid=<rand>``
wrappers minting a fresh ``tid=`` per keyword query, so the DB's
``UNIQUE(url)`` constraint never fired.

### Fixed

- **`app/url_canon.py`** — new `_unwrap_bing_news()` extracts the real
  target URL from Bing's ``url=`` query parameter (pure string, no
  network). New `_unwrap_google_news()` is present but currently
  network-only (`follow_redirects=True`); the collector calls it with
  `follow_redirects=False` for speed, so Google News wrappers still
  duplicate today. v0.7.2 will add batched async unwrap for those.
- **`app/collector.py`** — every RSS/Bing/GNews entry now runs through
  `url_canon.canonicalise_url(link, follow_redirects=False)` before
  `insert_article`. The dedup guarantee: same target URL with different
  `tid=`/`oc=` values collapses to one canonical form; the existing
  `UNIQUE(url)` constraint fires and the second insert is skipped.
- **`scripts/dedupe_existing.py`** (new) — one-shot cleanup of
  historical wrapper duplicates. Marks dupes as
  `is_filtered_out=1, filter_reason='v0.7.1 dedup'` (reversible via
  `--undo`; no physical DELETE). Canonicalises the survivor row's URL
  so dashboard "Open" links point to the publisher, not the wrapper.
  Run: `python scripts/dedupe_existing.py --dry-run`, then `--apply`.
- **`tests/test_url_canon.py`** — 11 new tests: Bing URL parse, edge
  cases (non-Bing host, wrong path, missing param, nested loop,
  case-insensitivity), GNews guards, and the core dedup-collapse
  assertion.

### Not fixed yet (deferred to v0.7.2)

- Google News RSS wrappers still create duplicates. The unwrap function
  is written but running it synchronously (per-article HEAD, 3s
  timeout) would add ~50 min to a typical run. Needs a batched async
  variant with a concurrency cap.
- Title-based cross-publisher dedup (same story on autoflotte.de +
  prreach.com + businesswire.com). Would catch ~10% additional cases
  but requires a fuzzy-match strategy and manual-override handling.

## [0.7.0] — 2026-07-09

Distribution-model shift: colleagues bootstrap the tool from a single
`AGENTS.md` file plus their AI assistant (Claude Code / Cowork).
No more ~45 MB ZIP mails per install. Rein additiv — zero application-
code changes; only distribution + bootstrap tooling touched.

### Added

- **`AGENTS.md` (rewrite)** — 9 sections (A: Goal, B: Preflight,
  C: Git-bootstrap, D: Python 3.13-bootstrap, E: clone + venv +
  `pip install`, F: `.env` write, G: hand-off message, H: HARD RULES,
  I: legacy-ZIP fallback). Deterministic anchors: absolute paths,
  pinned URLs (PortableGit-2.47.1, python-3.13.5-embed, get-pip.py),
  verify-after-each-step, HARD-STOP before every fallback.
- **`docs/DELIVERY_v0.7.0.md`** — design journal and V1–V4
  verification plan.

### Changed

- **`start.bat`** — 3-layout auto-detect cascade. Layout 1: packaged
  `python\python.exe` (ZIP, sets `FM_DATA_DIR=%LOCALAPPDATA%\FTNewsMonitor`).
  Layout 2: `venv\Scripts\python.exe` (git-clone, dev layout).
  Layout 3: system `py -3.13`. Clear fallback message when no
  interpreter is found.
- **`update.bat`** — dependency refresh on requirements change.
  `certutil -hashfile requirements.txt SHA256` snapshot before/after
  the pull; `fc /b` compares; on diff, `venv\Scripts\pip.exe install
  -r requirements.txt` runs automatically. Prevents "colleague updates
  → next `start.bat` throws ModuleNotFoundError" trap.
- **`docs/QUICKSTART.md`** — AGENTS.md path is now the primary
  install path; ZIP path stays as legacy alternative.
- **`docs/UPGRADE.md`** — Path A (git-clone + `update.bat`) is the
  default for new installs from v0.7.0 onward.
- **`docs/ROLLOUT_RUNBOOK.md`** — new §4.5 "Distributing v0.7.0+ via
  AGENTS.md" with a Teams-message template for the six-market rollout.
- **`VERSION`** — 0.6.1 → 0.7.0.
- **Default `ANTHROPIC_MODEL`** — `eu.anthropic.claude-haiku-4-5`
  → `eu.anthropic.claude-opus-4-7`. The prior alias (`claude-haiku-4-5`
  without version suffix) returned `400: The provided model
  identifier is invalid` on Jordan's Bedrock tenancy — the Opus 4.7
  cross-region inference profile ID is exactly `eu.anthropic.claude-opus-4-7`
  (verified via `list-inference-profiles`) so it works without a
  versioned suffix. Updated in `app/config.py`, `.env.example`,
  `README.md`, `docs/QUICKSTART.md`, `docs/ROLLOUT_RUNBOOK.md`
  (IAM policy ARN template), and test fixtures.

### Not changed

- Zero touches to `scripts/launch.py`, `build/build_release.py`,
  DB schema, wizard flow. The git-clone install works because
  `app/config.py::_resolve_data_dir()` falls back to the dev layout
  when `python\python.exe` is missing — no code change needed to
  support that. The only `app/*.py` touch is the one-line default-
  model swap above.

### Pinned URLs (Jordan: check quarterly)

- `github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/PortableGit-2.47.1-64-bit.7z.exe`
- `python.org/ftp/python/3.13.5/python-3.13.5-embed-amd64.zip`
- `bootstrap.pypa.io/get-pip.py`
- `github.com/jordanthekern-lang/ft-news-monitor.git` (repo URL)

## [0.6.1] — 2026-07-08

Full-repo review release: 52 confirmed findings from an 8-lens adversarial
audit, this ships fixes for the 3 critical + 12 major + selected minor
items. No new features; hardens the security posture and the corporate-
Windows rollout experience. Path A (`update.bat`) delivery works — no
new Python dependency, no interpreter bump.

### Security (critical — fix before public repo)

- **[C1] `AGENTS.md`** — removed the "if the user pastes credentials in
  chat, silently write them to `.env`" fallback. Added a HARD RULES
  section explicitly forbidding chat-to-`.env` credential writes, plus
  a caveat about STS-temporary keys (ASIA-prefixed) not being
  supported yet on the static-keys path.
- **[C2] Origin/Referer CSRF guard** in `app/main.py`. State-changing
  requests (POST/PUT/PATCH/DELETE) whose Origin or Referer isn't
  `127.0.0.1` / `localhost` / `testserver` → 403. Blocks browser-
  driven drive-by CSRF (e.g. a rogue tab issuing
  `POST /run-collection` to burn Bedrock budget).
- **[C3] TrustedHostMiddleware** in `app/main.py`. Only loopback Host
  headers accepted. Blocks DNS rebinding attacks that would otherwise
  turn `/admin/export-config` and `/settings` into cross-origin-
  readable endpoints once a rogue DNS TTL flips.
- **[C4] `SECURITY.md`** and **`docs/THREAT_MODEL.md`** — document the
  localhost-only single-user posture, in-scope vs. out-of-scope
  threats, and the code enforcement for T1 (CSRF) and T2 (rebinding).

### Major (fix before Italian pilot)

- **[M1] `app/providers.py`** — `ProfileNotFound` and other credential-
  resolution failures now raise `CredentialResolutionError` with a
  user-actionable message (e.g. "AWS profile 'typo-xyz' not found").
  The wizard's Test-AI button surfaces the message verbatim. First-run
  UX blocker resolved.
- **[M1] `test_connection` friendly errors** — raw Bedrock exception
  messages (`AccessDenied`, `ExpiredToken`, `InvalidSignatureException`,
  connection refused) get translated into actionable hints. AWS ARNs
  and account IDs are redacted before returning to the response so DNS
  rebinding can't scrape them.
- **[M2] `AWS_SESSION_TOKEN` forwarded** on the static-keys path. STS-
  temporary keys (`ASIA…`) can now be used via the static-keys mode.
  Requires the corresponding `AWS_SESSION_TOKEN` env-var / `.env` line.
- **[M3] Dashboard empty-state branches on `source_health`** — when
  ≥90% of sources are failing (typical corporate-proxy / VPN-off
  symptom), the empty-state now says "check with IT about proxy" instead
  of the misleading "widen your filters".
- **[M4] `scripts/launch.py`** — sets `NO_PROXY=127.0.0.1,localhost` on
  startup, and the readiness probe uses a proxy-bypassing opener.
  Windows GPO-configured `HTTP_PROXY` no longer sends loopback traffic
  through the corporate proxy.
- **[M5] `POST /settings/update`** — when the user adds a new home
  market via Settings, auto-reseed fires for the new market so
  keywords/feeds/outlets are available on the very next Run collection
  click (previously required a restart).
- **[M6] `app/pipeline.py`** — cross-process file lock via
  `pipeline.lock` in `DATA_DIR`. Previously the `asyncio.Lock` only
  guarded a single interpreter; the dashboard server + CLI script now
  serialise correctly. Stale-lock recovery: files older than 6h or
  pointing at a dead PID are auto-cleared.
- **[M7] `scripts/launch.py`** — single-instance guard via a Windows
  session-local named mutex (`Local\FTNewsMonitor-single-instance`).
  Double-clicking `start.bat` while the server is running now opens
  the browser at the existing server (port published in
  `DATA_DIR/port.txt`) instead of spinning up a second uvicorn.
- **[M8] `scripts/launch.py`** — atomic dev-DB migration. `shutil.copy2`
  now writes to `monitor.db.migrating` first, then `os.replace()`s
  into place — mid-copy hibernate can no longer leave a truncated
  target DB.
- **[M9] `scripts/backfill.py`** — lock file now records `pid=` and
  `started_at=`. Stale-lock detection (age >6h OR PID absent) auto-
  clears zombies from killed or hibernated backfills.
- **[M10] `update.bat`** — friendly error hints. Pattern-matches on
  `git`'s stderr for common failures (proxy blocking github.com, SSL
  cert issues, non-fast-forward divergence, auth prompts) and prints
  a targeted next-step. `GIT_TERMINAL_PROMPT=0` avoids the scary
  credential popup for public-repo clones.
- **[M11] Setup wizard** — TIMEZONE env-var auto-derived from the first
  home market chosen (Italy → Europe/Rome, Portugal → Europe/Lisbon,
  etc.). Users can still override in `/settings`. Fixes Portugal /
  Nordics timezone confusion in the scheduler and Outlook timestamp
  conversion.

### Lean

- Removed unused `LANG_TO_GOOGLE_NEWS` import from `app/admin.py`.
- Removed unused `CollectedItem` pydantic model from `app/models.py`.
- Removed unreachable `_sort_key` placeholder in
  `app/database.py::list_keyword_suggestions`.
- Fixed `app/url_canon.py` docstring — still referenced the v0.2.0 F3
  IMAP poller (removed in v0.3.0).

### Tests

- **13 new tests**: `tests/test_security_middleware.py` (7 —
  TrustedHost + Origin guard); `tests/test_providers.py` (6 —
  ProfileNotFound surfacing, session-token forward, friendly-error
  redaction).
- All 397 pass. Bumped `Settings` dataclass in the four fixture
  builders to include the new `aws_session_token` field.

### Rollout notes

- **No schema change.** Path A (`update.bat` git-pull) works for
  existing installs. Colleagues on Path B (ZIP) can pick up v0.6.1 by
  extracting the new ZIP over their existing folder — DB, `.env`, and
  home_markets survive.
- **Wizard timezone auto-fill** only kicks in if `TIMEZONE` is unset or
  equals the old default `Europe/Berlin`. Users who already saved an
  explicit timezone keep their choice.

## [0.6.0] — 2026-07-08

Distribution model shift: colleagues who have Git installed can update
via `update.bat` (git-pull) instead of waiting for a new ZIP. The ZIP
path stays the default for corporate laptops without Git, so no rollout
disruption. Small UX addition: bulk-dismiss on the keyword-suggestions
page.

### Added

- **`update.bat`** at repo root and in the ZIP. Runs `git pull --ff-only`
  on the extracted clone. Bails cleanly with an actionable message when
  Git is missing, the folder is not a clone, or the pull is not a
  fast-forward. The database, `.env`, home markets, starred articles,
  and golden-set labels live in `%LOCALAPPDATA%\FTNewsMonitor\` and are
  outside the repo — unaffected by the pull.
- **`docs/CONTRIBUTING.md`**: release process (bump / commit / tag /
  push) and first-install-via-clone recipe.
- **`docs/UPGRADE.md`**: split into Path A (`update.bat`) and Path B
  (ZIP re-extract), with a "when to use which" guide.
- **`docs/ROLLOUT_RUNBOOK.md`** section 6b: how to push a release from
  Jordan's side, and repo-hygiene checks before the first push to
  origin.
- **Suggestions admin — Dismiss-all button.** When the pending queue
  balloons to hundreds of low-signal proposals after a wide AI run,
  one confirm-click clears them. Only pending rows are affected;
  approved / previously-dismissed rows are untouched. Backed by a new
  `POST /admin/suggestions/dismiss-all` endpoint.

### Notes

- Path A works only for code / seeds / docs updates that don't change
  `requirements.txt` or the pinned Python. When a release ships a new
  dependency or interpreter, use Path B — the CHANGELOG entry will
  call it out.
- No schema change. T11 additive-only invariant preserved.
- Auto-reseed on startup (v0.4.0) picks up any new seed rows from the
  `git pull` on the next launch — no manual re-import needed.
- `.gitignore` widened to cover backup DBs (`*.db.bak*`),
  `_restore_*.json`, `MIGRATED_FROM_DEV.txt`, and `backfill.lock` so
  stray dev-side artefacts don't sneak into commits.

## [0.5.1] — 2026-07-08

Bugfix release for v0.5.0's auto-backfill logic. Live-test on Jordan's
install revealed two related problems: (a) the 5y sweep wasted ~40% of
its collection budget on articles that the prefilter throws away because
they're older than `MAX_AGE_DAYS=180`, and (b) the `errors==0` gate on
`INITIAL_BACKFILL_COMPLETED` was too strict — a single known-broken feed
(`bav.admin.ch`) kept the flag permanently off, so every manual "Run"
click re-triggered the full 30-minute sweep. Solution: replace the
one-shot flag with an adaptive time-window keyed off
`last_non_dry_run_at`.

### Changed

- **Adaptive time-window** in `pipeline.run_pipeline`. First run
  (no `last_non_dry_run_at`) uses `1y` — matches the prefilter's
  180-day cutoff with headroom for stale metadata, replaces the old
  `5y` sweep which was ~40% wasted work. Subsequent runs pick the
  smallest GDELT bucket that still covers the gap since the last
  run: `≤30d → 1m`, `≤90d → 3m`, `≤180d → 6m`, else `1y` (cap).
  Explicit `time_window_override` (e.g. `scripts/backfill.py
  --window 5y`) always wins.
- **`INITIAL_BACKFILL_COMPLETED` flag deprecated**. The constant is
  kept in `app/runtime_settings.py` for backward compatibility and
  any existing DB rows are ignored — the adaptive-window logic reads
  `last_non_dry_run_at` instead (which is stamped on every non-dry
  run by `run_pipeline`, so no new bookkeeping needed).
- **`scripts/backfill.py`**: removed the post-run flag flip. The
  script still forces a wider window via `--window 5y`, but no longer
  writes any settings key — its side effect on `last_non_dry_run_at`
  is enough.
- **Pipeline summary**: replaced boolean `initial_backfill` with
  descriptive `time_window_reason` string (`"first-run"`,
  `"gap=5.2d"`, `"gap=400.0d (capped)"`, etc.).

### Fixed

- **Rerun loop**: v0.5.0's `errors==0 AND total_new>0` gate on the
  flag flip meant a single failing feed (which Jordan's install has:
  `bav.admin.ch` → 403) kept `INITIAL_BACKFILL_COMPLETED` off
  forever. Every manual pipeline run then re-triggered a 30-minute
  5y sweep. The new logic has no flag, so this failure mode cannot
  recur.
- **Wasted queries on old articles**: 40% of the 5y sweep's 698 new
  articles were >180 days old and got dropped by the prefilter. The
  new 1y first-run window aligns with the prefilter cutoff.
- **Review-fix**: `LAST_NON_DRY_RUN_AT` is now only stamped when
  `SETUP_COMPLETED` is True. Without this guard, a pre-wizard trigger
  (scheduler catch-up, POST `/run-collection`, CLI) would write the
  timestamp even though the run collected nothing, causing the first
  real post-wizard run to adaptively pick `1m` instead of `1y` and
  permanently miss articles 30-180 days old.

### Migration

- Zero action required. On the next run, the pipeline reads
  `last_non_dry_run_at` (which v0.5.0 already stamped) and picks the
  smallest adaptive bucket. Existing `initial_backfill_completed`
  rows in `app_settings` are ignored (kept for T11 additive-only
  invariant).

## [0.5.0] — 2026-07-08

Recall-uplift release. v0.4.x installs typically stalled around ~1,000
articles even with a 58-keyword DACH catalogue because the collector
architecture had three structural caps in the raw-fetch stage:
keyword-batching against Google News, no time-window operator, and no
second aggregator besides Google/Bing. This release removes all three
caps and adds a one-off backfill script for populating historical
Bestand on a fresh install. No schema changes.

### Added

- **GDELT DOC 2.0** as a third aggregator behind the `GDELT_ENABLED`
  flag (default `true`). Free, no API key, IP-neutral index. The
  new `_fetch_gdelt_entries` adapter maps the JSON response to the
  feedparser-style shape that `_store_entries` already consumes, so
  the pre-filter / translator / AI-filter pipeline is unchanged.
  Rows land with `source_type='gdelt'`.
- **`COLLECTOR_TIME_WINDOW` env setting** (default `"3m"`) that
  controls the `when:{window}` operator injected into Google News
  queries and the equivalent `qft=interval` value on Bing plus
  `timespan=` on GDELT. Valid tokens: `1h, 1d, 7d, 1m, 3m, 6m,
  1y, 5y`. Invalid values silently fall back to `3m`.
- **`scripts/backfill.py`** — one-off historical sweep with a wider
  time-window (default `--window 5y`). Uses the same `run_pipeline`
  path as scheduled runs (so the async lock protects against
  overlaps) plus its own `backfill.lock` file. Runtime 15-30 min.
  Regular daily runs continue to use `COLLECTOR_TIME_WINDOW` (3m).
- **Auto-backfill on first pipeline run.** New runtime flag
  `initial_backfill_completed` (default false). The FIRST non-dry
  `run_pipeline` call on a fresh install silently uses a 5y
  time-window and flips the flag after success. All subsequent runs
  inherit `COLLECTOR_TIME_WINDOW` (3m). Colleagues who never open a
  terminal still get the fat historical catalogue on their first
  "Run collection" click / scheduled catch-up run. Explicit overrides
  (e.g. `scripts/backfill.py --window 5y`) still win and do NOT flip
  the flag, so power-users retain control.
- **Raw counters in `CollectionStats`** — `raw_google_news`,
  `raw_bing_news`, `raw_gdelt`. Makes the attrition-per-stage visible
  in the collector log: "Google: 5000 raw / 500 new / 4500 dup"
  instead of just "500 new". Diagnostic hook only, no behaviour
  change.

### Changed

- **Un-batched keyword queries** (`app/collector.py::_process_keywords`).
  Previous revisions grouped keywords into 5-term OR chunks to cut
  the request count; the resulting complex queries made Google News
  return well below its 100-item cap. Now each keyword fires its own
  Google + Bing + GDELT query. Query count per run scales roughly
  from ~21 to ~90 with an 84-keyword catalogue. Wall-clock adds
  ~30-90 seconds; Bedrock cost is unchanged because duplicates are
  dropped by the URL UNIQUE constraint before the AI filter runs.
- **`source_health` slug format** for keyword rows changed from
  `keywords:<lang>:<market>:<chunk-index>` to
  `keywords:<lang>:<market>:<keyword-slug>` so per-keyword health is
  stable across runs.
- **`collector.run` signature** gained an optional
  `time_window_override` kwarg (also on `pipeline.run_pipeline`) so
  the backfill script can force `5y` without touching env values.
  Existing callers pass `None` and inherit the env default.

### Backward compat

- No schema changes. `articles`, `source_health`, `app_settings`
  remain untouched. T11 additive-only invariant preserved.
- Old-style `keywords:<lang>:<market>:<chunk-index>` rows in
  `source_health` stay in the DB as historical noise and eventually
  fall out of the UI's recency view. Not deleted programmatically.
- Existing `.env` files keep working unchanged. The new
  `GDELT_ENABLED` and `COLLECTOR_TIME_WINDOW` keys have safe defaults
  and are optional.

### Expected impact

- After one backfill run (`python scripts/backfill.py --window 5y`),
  a DACH+EU install typically grows from ~1,000 to ~3,000-6,000
  articles depending on Google/GDELT depth for the keyword set.
- Regular daily runs will pull ~5-15× more raw entries than before
  because of un-batching + `when:3m`, but most collapse into
  duplicates. Net new articles per day rise from ~10-30 to ~50-150.
- Zero cost impact on Bedrock: duplicates never reach the AI filter,
  and old-date articles bypass or fail the pre-screen before AI is
  invoked.

### Fixes (pre-release adversarial review)

Seven findings from the v0.5.0 review workflow were fixed before the
release build:

- **Backfill flag gate**: `INITIAL_BACKFILL_COMPLETED` now only flips
  when the collector reports `errors == 0` AND `total_new > 0`. Fixes
  the silent-loss case where an offline / broken-TLS first run would
  swallow all HTTP errors, return empty stats, and permanently mark
  the historical sweep as done.
- **Flag flip placement**: moved to immediately after `collector.run()`
  instead of after `ai_filter.run()`. A Bedrock throttle on the fat
  first-run batch no longer forces the pipeline to re-fetch Google /
  Bing / GDELT (~30 min) on retry — downstream stages retry against
  the DB rows already collected.
- **Setup guard**: auto-backfill is skipped when `setup_completed=False`,
  so a scheduler catch-up run that fires before the wizard finishes
  cannot trigger a 5y sweep against demo config.
- **`scripts/backfill.py` flips the flag** manually after a successful
  explicit run, so a subsequent scheduled run does not do a second
  automatic 5y sweep.
- **Bing `1h` interval** now maps to `"7"` (past 24h — Bing's narrowest
  bucket) instead of `"9"` (past month). The old value was mislabeled
  in the comment and would silently defeat a tight time-window.
- **Google-News URL None** no longer kills the whole query. Keywords
  with an unmapped language (Ukrainian, exotic locales) now log an
  INFO line, skip the Google leg, and still hit GDELT.
- **Non-ASCII source-slug**: keywords in Cyrillic / CJK / Arabic no
  longer collapse to an empty slug (which would have collided every
  such row in `source_health`). A 8-hex-char SHA256 fallback keeps
  each keyword's health row uniquely keyed.

## [0.4.3] — 2026-07-08

Widens the AI relevance rubric to cover the adjacent-mode funding and
charging-infrastructure signals that Public Affairs treats as
lobbying opportunities: any bus-funding programme (including
city/regional buses) and any truck/HGV charging-infrastructure funding
or regulation. Score is now driven by the funding/policy signal — not
by whether "coach" is named explicitly in the source. Operational news
about the same adjacent modes (fleet retirements, model launches)
still scores low, so the dashboard is not flooded with generic HGV
reporting.

### Changed

- **`SYSTEM_PROMPT` in `app/ai_filter.py`.** In-scope list grew from
  6 to 8 items:
  - New item 2: bus-funding programmes for city / regional buses are
    in-scope because a new funding scheme is a PA opportunity to
    lobby for coach eligibility.
  - New item 5: truck / HGV charging-infrastructure funding and
    regulation (MCS corridors, en-route depots, grid-connection
    programmes) is in-scope because coaches share the truck power
    class and any truck-charging expansion is a placement
    opportunity.
- **Priority-weighting note** now explicitly says these adjacent-mode
  funding items must NOT be down-ranked for lack of an explicit coach
  mention.
- **Guardrail rewritten** to draw the line where PA relevance ends:
  city-bus / regional-bus / truck **operational** news (e.g. a local
  operator retiring diesel, a truck-model launch) still gets a low
  score. Only funding / policy / charging signals from the adjacent
  modes are pulled in.

### Backward compat

- No schema changes, no runtime changes, no new settings. The prompt
  edit takes effect from the next AI-filter run after upgrading.
- Historical article scores are not re-rated. If you want the new
  rubric applied retroactively, delete or reset the affected rows'
  `ai_filtered` flag so the next run picks them up.

## [0.4.2] — 2026-07-08

Ships an `AGENTS.md` file at the root of the release ZIP so a
non-technical colleague can delegate the AWS-Bedrock setup step to an
AI assistant (Claude Code, Claude Cowork). The assistant reads the
runbook, locates AWS credentials already present on the laptop (SSO
profile, static keys, or active SSO cache), and writes them into
`%LOCALAPPDATA%\FTNewsMonitor\.env`. The user then starts the tool
normally and continues through the remaining wizard steps (home
markets, seeds, scheduler) in the browser. No runtime or schema
changes.

### Added

- **`AGENTS.md` at ZIP root.** Deterministic setup runbook aimed at AI
  coding assistants. Scope is strictly limited to writing the
  `AI_PROVIDER` / `AWS_*` block of the local `.env` file — home
  markets, seed imports, Outlook and the scheduler stay in the web
  wizard. The document is written as a checklist (no prose) with
  explicit "do NOT print secrets in chat", "do NOT launch start.bat",
  and "preserve unrelated .env keys" rules so different AI models
  behave consistently.
- **Shortcut note in `docs/QUICKSTART.md`.** A small callout before
  wizard Step 2 tells non-technical users the AI-assistant path
  exists. Colleagues without an AI assistant follow the regular
  wizard unchanged.
- **Build-payload guard.** `build/build_release.py` now includes
  `AGENTS.md` in `INCLUDE_FILES` and requires it in `_sanity_check`,
  so a missing file fails the build loudly rather than shipping a
  half-ready ZIP.

### Backward compat

- No code changes. No schema changes. Not even a new setting.
- v0.4.1 installs upgrade transparently — the new file just appears at
  the ZIP root; whether the colleague uses it or not is opt-in.

## [0.4.1] — 2026-07-08

Hotfix for `import pywintypes` failing on fresh packaged extracts. Reported
by a colleague on Windows while walking through the Outlook-COM setup — the
DLLs and Python modules were shipped in the ZIP but not discoverable
because `pip install --target lib/` skips the `pywin32_postinstall` step.

### Fixed

- **`import pythoncom` / `import pywintypes` on packaged install.**
  `scripts/launch.py` now calls `_ensure_pywin32_dlls()` before any `app`
  import: adds `lib/pywin32_system32` to the DLL search path (via
  `os.add_dll_directory` + `PATH`) and prepends `lib/win32/lib` and
  `lib/win32` to `sys.path` so the Python-level wrappers resolve. Mirrored
  as `_ensure_pywin32_importable()` at the top of `app/outlook_poller.py`
  as belt-and-braces for entry points that don't go through
  `scripts/launch.py`. Idempotent, silently no-ops on non-Windows or dev
  trees.
- **Symptom** users saw before the fix: the wizard's "Test Outlook
  connection" button returned `pywin32 is not available: no module named
  'pywintypes'`. Reproducible on any first-time extract with a corporate
  Windows profile.

## [0.4.0] — 2026-07-08

Rollout-hardening: shipped seed packs now update automatically on startup
without touching user edits, existing installs keep every keyword, every
golden-label and every star across a version upgrade, and Jordan's own
dev-layout DB is migrated into the packaged `%LOCALAPPDATA%\FTNewsMonitor`
location on the first packaged launch. Additive-only — T11 invariant
preserved.

### Added

- **Auto-reseed on startup.** The FastAPI lifespan now calls
  `seed_import.auto_reseed_home_markets()` after `database.init_db()`.
  For every entry in `home_markets`, if `seeds/<slug>.json` declares a
  `seed_version` higher than the one stored under
  `runtime_settings.IMPORTED_SEED_VERSIONS`, the file is merged into the
  local DB. Merge semantics are the existing merge-mode of
  `seed_import.import_seed_file` — only new natural keys are inserted,
  existing rows (including anything the user hand-edited) are preserved.
  Skipped when `setup_completed` is falsy (Fresh installs go through the
  wizard, which runs its own explicit import).
- **`runtime_settings.IMPORTED_SEED_VERSIONS`.** New `app_settings` key
  storing a `{slug: version}` dict so the auto-reseeder can decide
  whether a merge run is needed. Bumped by both the auto-reseeder and
  the wizard's manual import step.
- **`seed_import.mark_seed_imported(market, seed_version)`.** Helper for
  the wizard to record a completed import; called from the wizard's
  `POST /setup/import-seeds` handler.
- **Auto-migration of a dev-layout DB into the packaged location.**
  `scripts/launch.py::_migrate_dev_db_if_needed()` runs before
  `database.init_db()` on packaged startup. If the embedded-Python layout
  is detected AND the target `%LOCALAPPDATA%\FTNewsMonitor\monitor.db`
  doesn't exist yet AND a non-empty `data/monitor.db` sits next to the
  extract, the file is copied over and a `MIGRATED_FROM_DEV.txt` marker
  is written. Idempotent — the target-exists guard makes a second launch
  a no-op.

### Changed

- **`seeds/dach.json` and `seeds/eu.json` regenerated from Jordan's
  Prod DB.** 58 DACH keywords (was 45), 31 EU keywords (was 26), 16
  DACH feeds, 11 EU feeds, 14 DACH outlets, 19 EU outlets, 16 DACH
  negatives, 8 EU negatives. `seed_version` bumped from 1 to 2 so the
  auto-reseeder on any existing v0.3.x install merges the new rows on
  first startup. Nothing is removed — only additions.

### Backward compat

- No schema changes. No column adds. No table adds.
- Existing installs upgrade transparently: extract the new ZIP over the
  old folder, launch, and the auto-reseeder merges the additional
  DACH/EU rows into the existing DB. User-added keywords, golden labels,
  starred articles, all runtime settings survive untouched.
- Downgrade to v0.3.x is safe: the new
  `runtime_settings.IMPORTED_SEED_VERSIONS` key is unknown to older code
  but harmless (JSON blob in `app_settings`, ignored).
- Migration invariant (T11): additive-only preserved.

## [0.3.2] — 2026-07-08

Three related newsletter improvements — pre-screen sees the mail body,
newsletter articles are visually identified as such, and clicking a
newsletter can re-open the source mail in Outlook. Additive schema
change only (T11 invariant preserved).

### Added

- **Pre-screen now reads the newsletter body, not just the subject.** A
  new `_extract_snippet_from_body` helper pulls the first ~1500 chars
  of plain-text (falling back to HTML with `<script>`/`<style>`
  stripped and entities unescaped) and persists it as
  `articles.snippet`. The keyword pre-screen already consumed
  `snippet` via `_row_haystack`, so no ai_filter change was needed;
  newsletters with generic subjects like "Weekly digest" but strong
  body signal now survive the pre-screen and reach Claude.
- **Newsletter chip + "Open in Outlook" button in the dashboard.**
  Cards with `source_type='newsletter'` now show a purple `✉ Newsletter`
  chip so they're distinguishable from RSS/Google-News hits at a glance.
  The card title links to `/article/{id}` instead of `art.url` (which
  is only the first body link — clicking it went to a landing page,
  not the mail). A prominent "Open in Outlook" action button appears
  on both dashboard cards and article-detail pages for newsletters.
- **`POST /outlook/open/{article_id}` route.** Uses `pywin32` to call
  `Outlook.Application.GetNamespace("MAPI").GetItemFromID(entry_id, store_id)`
  and `.Display()` — pops the original mail open in the running
  Outlook window. Read-only: never modifies `.Read` state, never
  moves or deletes.
- **Two new nullable columns on `articles`**: `outlook_entry_id` and
  `outlook_store_id`, populated at ingest for `source_type='newsletter'`
  rows so the re-open path can round-trip the mail through COM.

### Changed

- The `insert_article()` helper gains two optional kwargs
  (`outlook_entry_id`, `outlook_store_id`). Backwards-compatible —
  every existing caller other than the Outlook poller passes them as
  the default `None`.

### Backward compat

- Newsletter rows ingested with v0.3.0 / v0.3.1 have `NULL` for the
  new columns; clicking "Open in Outlook" on those returns a hint
  string with the mail subject so the user can find it manually
  ("This newsletter was ingested before v0.3.2 and has no Outlook
  link stored. Search your Outlook folder for the subject: …").
  No forced migration.

## [0.3.1] — 2026-07-08

Settings-page usability pass. Everything below is additive — no breaking
changes, no schema changes. Existing v0.3.0 installs pick up the new
behaviour after extracting the ZIP; the DB and `.env` are untouched.

### Added

- **Nav "Settings" (was "Diagnostics").** The page title on `/settings`
  is now "Settings and Diagnostics" to reflect that the page hosts
  preferences, not just diagnostics.
- **`SUGGEST_KEYWORDS` toggle in the UI.** The F2 keyword-suggestion
  feature is now enabled/disabled from `/settings` instead of
  requiring an `.env` edit. Resolution order at runtime is
  `runtime setting > env flag > default false`, so the env flag stays
  as a documented CLI escape hatch.
- **Translation target languages configurable from `/settings`.**
  Instead of the hard-coded `{de, en}` skip-list, the translator now
  either (a) auto-derives target languages from the configured home
  markets via `config.MARKET_TO_LANGS` plus a hard-coded `"en"`, or
  (b) uses a manual override entered in Settings. Portugal-based
  colleagues now see PT articles in Portuguese by default; Nordics
  colleagues see SV/NO/DA/FI in their originals.
- **Outlook mailbox editable from `/settings`.** New form on the
  Settings page posts to `/setup/outlook` (same validated route as the
  wizard). Includes a **Test connection** button that reaches out to
  Outlook via COM, resolves the folder, counts items, and reports back
  as a green/red badge — no side effects, read-only. The wizard's
  step 6 also gains the same button.
  - New helpers: `outlook_poller.test_connection()` + private
    `_test_connection_sync()`; new route `POST /setup/test-outlook`
    (accepts form-values, not the saved `.env`, so users can validate
    a path before saving).
  - New runtime-settings constants: `SUGGEST_KEYWORDS_ENABLED`,
    `TRANSLATION_TARGET_LANGS`.

### Fixed

- **Silent `items.Sort()` failure could drop newer newsletters.** If
  Outlook rejects the `Items.Sort("[ReceivedTime]", True)` call (locale
  quirks, MAPI restrictions), the iteration falls back to insertion
  order and the old code broke on the first old item, silently
  skipping later fresh ones. Now the sort success is tracked in a
  local flag; on failure we log a warning, keep iterating, and just
  skip old items with `continue` instead of `break`.
- **`ReceivedTime` interpreted as UTC when it's actually local
  wall-clock.** COM's `ReceivedTime` is a naive local datetime, but
  the v0.3.0 poller stamped it with `tzinfo=timezone.utc`, which
  shifted the dashboard grouping by the local offset (a 23:30 Berlin
  newsletter appeared on the next day). Now the poller resolves the
  local zone via `TIMEZONE`/`FT_TIMEZONE` env or the system tz and
  converts to UTC before insertion.

## [0.3.0] — 2026-07-08

Newsletter ingest via the locally running Outlook desktop app. Replaces
the v0.2.0 IMAP poller which turned out to be blocked at FlixBus by
tenant policy (Microsoft App-Passwords disabled, Basic-Auth-for-IMAP off).

### Breaking

- **F3 IMAP poller removed.** The `IMAP_*` env vars are no longer read.
  `app/imap_poller.py`, `scripts/imap_list_folders.py`, and
  `tests/test_imap_poller.py` are gone.
  - Existing `.env` values are left untouched but ignored — no data
    loss, just harmless leftover lines.
  - Wizard step 6 is now Outlook-COM instead of IMAP. Re-run the wizard
    (or edit `.env` directly) to configure the new source.

### Added

- **Newsletter ingest via Outlook-COM (F3.5).** A new
  `app/outlook_poller.py` talks to the locally running Outlook desktop
  app via COM automation (`pywin32`). No password, no OAuth, no IT
  ticket — piggybacks on the user's existing SSO session in Outlook.
  - New env vars: `OUTLOOK_ENABLED`, `OUTLOOK_FOLDER_PATH` (required,
    e.g. `Posteingang/BT Verkehr`), `OUTLOOK_STORE_NAME` (optional,
    for multi-account setups), `OUTLOOK_MAX_AGE_DAYS` (default 30),
    `OUTLOOK_MAX_LINKS_PER_EMAIL` (default 20).
  - Reads **exactly one folder** — never the whole mailbox. Empty
    folder path is treated as disabled at both the wizard layer and
    runtime; no fallback to Inbox root.
  - Read-only: never toggles `\\Read` on the source item, never moves
    or deletes messages.
  - URL extraction + tracker-strip logic identical to v0.2.0 F3, so
    dedup against RSS/Google-News collected articles works transparently.
- **Wizard step 6 rewritten.** New Outlook-flavoured form with a
  folder-path input, optional store name, and preconditions callout
  (Outlook must be open + logged in; first-run trust popup expected).
- **`scripts/outlook_list_folders.py`** — one-off diagnostic CLI that
  walks the Outlook store tree and prints every folder path so users
  can copy their exact name into the wizard.

### Requirements

- `pywin32>=306; sys_platform == "win32"` added to `requirements.txt`.
  Bundled in the release ZIP via `build/build_release.py`. Adds ~10 MB
  to the packaged distribution — ZIP grows from 34.6 MB to ~45 MB.
- Windows-only. Non-Windows callers see `outlook_enabled() → False`
  and the poll step is skipped; the rest of the pipeline is unchanged.

### Known limitations

- **Outlook must be running** when the collection runs. If it's closed
  the step raises, gets caught by the collector, marked as failing in
  `source_health`, and the rest of the pipeline continues. Next
  collection with Outlook open picks the missed newsletters up.
- **First-run trust popup.** Modern Outlook may show a security prompt
  ("A program is trying to access email addresses — allow?") on the
  first COM connection. Click "Allow for 10 minutes"; hardened tenants
  may block this via Group Policy in which case the user needs to talk
  to IT (rare).
- **Folder-path language matches Outlook UI.** German Outlook UI shows
  `Posteingang`, English shows `Inbox`. The exact string as displayed
  is what COM expects. `scripts/outlook_list_folders.py` shows it.
- Newsletter articles land with `country=None`, so market-scoped
  negative keywords don't apply to them (only globally-scoped negkw).
  This is a documented asymmetry inherited from v0.2.0.

## [0.2.0] — 2026-07-07

Post-pilot precision/recall uplift. All three features are **default-off** —
existing v0.1.0 installs upgrade in place without behaviour change until an
admin flips the flag. The DB schema evolves additively (two new tables, no
column changes), so the T11 rollback invariant ("re-extract the previous ZIP
and everything still works") remains true.

### Added

- **Golden-Set Backtesting (F1).** Personal per-install eval loop.
  Label articles as `relevant` / `noise` via new R/N buttons on the dashboard
  cards and the article detail page. Then run
  `python scripts/run_eval.py --tag <name>` to re-score them against the
  current prompt and get precision/recall/F1 + per-market confusion in
  `%LOCALAPPDATA%\FTNewsMonitor\eval_runs\<ts>_<tag>.json`.
  - New `/admin/golden` overview with count badges.
  - `--no-feedback` flag runs against a baseline without the few-shot examples.
  - Separate `golden_labels` table so eval labels never leak into the AI
    filter's few-shot prompt or the domain-adjustment score.
  - Cost guardrail: > 200 articles requires `--yes`.

- **Claude keyword suggestions (F2).** During scoring, Claude may propose
  1-3 phrases (in the article's original language) that would have surfaced
  the article via keyword pre-screen if they had been in the catalogue.
  Suggestions land in a new `/admin/suggestions` page, grouped by
  normalised phrase, sortable by frequency.
  - Approve → creates a real `keywords` row and flips the whole group to
    `approved`; the phrase is picked up by the very next pipeline run.
  - Dismiss → group-level, so a spammy phrase silences all future duplicates.
  - Bidirectional subset dedup against existing keywords prevents the model
    from proposing "PNRR autobus" when "PNRR autobus elettrici" already exists.
  - Requires `SUGGEST_KEYWORDS=true` in `.env`. Off by default — no extra
    output-token cost for installs that don't opt in.

- **Newsletter IMAP poller (F3).** Optional 5th collection step that pulls
  URLs from a personal mailbox via IMAP + a Microsoft app-password.
  - Sender allow-list matches by exact email or `@domain.com` form.
  - Regex-only href extraction (no bs4 vendored).
  - `BODY.PEEK[]` fetch — never marks the user's mailbox as read.
  - URL canonicalisation via new `app.url_canon` module: allow-list-based
    tracker strip (`utm_*`, `fbclid`, `gclid`, `mc_cid`, `ref`, …) plus
    HEAD-unwrap for allow-listed redirector hosts (Substack, Mailerlite,
    Sendgrid patterns). Never HEADs arbitrary domains.
  - Setup wizard step 6 configures IMAP; blank-password submit preserves
    the existing `.env` value (round-trip safety).
  - Failures are isolated to `source_health` — a dead mailbox doesn't kill
    the rest of the pipeline.
  - Requires `IMAP_ENABLED=true` in `.env`. Off by default.

### Changed

- Dashboard hot-path is a hair faster: introduces
  `database.golden_label_map()` (slim `SELECT article_id, label`) in place
  of the JOIN-heavy `list_golden_labels()` when the dashboard only needs
  the id → label mapping.
- `CollectionStats.total_new` now includes IMAP-sourced articles.
- `AIScore` gained an optional `suggested_keywords` list (empty by default
  — no behaviour change for existing callers that construct the model
  positionally).

### Migration invariant

No column adds, no renames, no drops. Two `CREATE TABLE IF NOT EXISTS`
statements. `%LOCALAPPDATA%\FTNewsMonitor\monitor.db` from v0.1.0 opens
under v0.2.0 without conversion.

### Rollout for existing v0.1.0 installs

1. Extract the new ZIP over the existing folder (do not delete
   `%LOCALAPPDATA%\FTNewsMonitor`).
2. Relaunch `start.bat`. The dashboard renders as before; R/N buttons
   appear on every article; the Golden set nav link shows up (empty state).
3. To enable F2, add `SUGGEST_KEYWORDS=true` to `.env` and restart.
4. To enable F3, run the wizard once more from the settings gear icon (or
   edit `.env` directly — the wizard is the friendlier path).

### Costs

- F1: one Haiku 4.5 call per labeled article, on-demand only. ~$0.30
  per 100 articles.
- F2: negligible extra output tokens per pipeline run (~50 tokens per
  scored article on average, one JSON field). Est. +$25/year at 500
  articles/day.
- F3: zero AWS spend. IMAP is on your own mailbox.

## [0.1.0] — 2026-07-06

Initial FT News Monitor market-edition release. See `docs/DELIVERY_v0.1.0.md`.
