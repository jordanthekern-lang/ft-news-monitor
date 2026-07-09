# FT News Monitor v0.2.0 — Delivery Note

**Release:** v0.2.0 · **Build date:** 2026-07-07 · **Target:** post-pilot
precision/recall uplift. Existing v0.1.0 installs upgrade in place.

## What is new since v0.1.0

- **Golden-Set Backtesting (F1).** Label articles as `relevant` / `noise`
  from the dashboard or detail page (new R/N buttons), then run
  `python scripts/run_eval.py --tag <name>` to re-score them and get
  precision/recall/F1 + per-market confusion. Artefacts land under
  `%LOCALAPPDATA%\FTNewsMonitor\eval_runs\<ts>_<tag>.json`. Labels live in
  a separate `golden_labels` table so eval never contaminates the AI
  filter's few-shot prompt.
- **Claude keyword suggestions (F2).** With `SUGGEST_KEYWORDS=true` in
  `.env`, Claude proposes 1-3 phrases per scored article that would have
  surfaced it via keyword pre-screen if they had been in the catalogue.
  Review at `/admin/suggestions` — approve to promote to a real keyword
  (picked up on the very next run), or dismiss to silence duplicates.
- **Newsletter IMAP poller (F3).** Optional 5th collection step reading
  a personal mailbox via IMAP + Microsoft app-password. Sender allow-list,
  tracker-param URL canonicalisation, `BODY.PEEK` (never marks messages as
  read). New setup wizard step 6 configures it; can also be edited via
  `.env` directly.

## Migration invariant

Two new tables (`golden_labels`, `keyword_suggestions`), zero column
adds, zero renames, zero drops. Rolling back to v0.1.0 is safe — extract
the old ZIP over the folder, `%LOCALAPPDATA%\FTNewsMonitor\monitor.db`
still opens.

## Rollout for Jordan

1. Copy `dist\FTNewsMonitor-0.2.0-win64.zip` over the previous
   installation (or into a scratch folder for a fresh smoke test).
2. Right-click → Properties → Unblock → OK, then Extract All.
3. Double-click `start.bat`. The dashboard renders as before, with new
   R/N buttons on each article card and a Golden set link in the nav.
4. To enable keyword suggestions: `SUGGEST_KEYWORDS=true` in `.env`.
5. To enable IMAP: run the setup wizard's step 6 (or edit `.env`).

## Rollout for existing v0.1.0 pilot (Guglielmo)

Zero-impact upgrade. All three features are default-off. Guglielmo can
keep using the tool exactly as before; when Jordan says the word, one
`.env` edit switches on F2, and the wizard's new step 6 configures F3.

## What is NOT shipping (deferred)

- CSRF tokens on admin POSTs — same posture as v0.1.0 (localhost-only,
  revisit if LAN-exposed). Flagged in the F2 security review.
- Microsoft-Graph OAuth for IMAP — currently the plan assumes an
  app-password. If FlixBus IT blocks app-passwords per policy, F3 is
  blocked and OAuth becomes a separate future task.
- SSRF hardening on redirector-unwrap follows: HEAD-unwrap follows
  arbitrary `Location:` headers (bounded to 3 hops). Reduced impact (HEAD,
  no data leak), but noted for a future round.

## Costs

- F1: ~$0.30 per 100 labeled articles on Haiku 4.5, on-demand.
- F2: ~$25/year at 500 articles/day. Off by default.
- F3: zero AWS spend; only IMAP + your existing mailbox.

## Known quirks

- **CSV `relevance_score` column** still contains the composite score
  (raw + trust + feedback). Rename to `final_score` remains parked for
  v0.1.1.
- **URL canonicalisation is best-effort.** On network failure the
  original URL is stored — dedup falls back to the un-stripped form.

## Support

- Log file: `%LOCALAPPDATA%\FTNewsMonitor\server.log`
- Eval artefacts: `%LOCALAPPDATA%\FTNewsMonitor\eval_runs\`
- Contact **Jordan Kern** via Teams or email.
- Troubleshooting matrix: `ROLLOUT_RUNBOOK.md` §4.
