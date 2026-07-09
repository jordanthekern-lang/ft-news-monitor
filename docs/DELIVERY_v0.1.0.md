# FT News Monitor v0.1.0 — Delivery Note

**Release:** v0.1.0 · **Build date:** 2026-07-07 · **Target:** Italy pilot (Guglielmo)

## What is shipping

- Portable Windows ZIP (~35 MB) — extract-and-run, no admin, no Python install.
- Seven market packs curated from research: DACH, EU, Italy, France,
  Benelux, Portugal, Nordics. Italy + EU are pilot-ready.
- First-run setup wizard: home markets, AWS credentials, AI connectivity
  test, seed import, daily schedule — five steps in ~5 minutes.
- In-app scheduler (APScheduler) with catch-up on missed days.
- Confluence-ready Markdown export + Excel-ready CSV export (UTF-8 BOM
  so accents survive double-click).
- Star / Ignore feedback loop that boosts scores for domains you value.
- All UI + AI prompts in English; German legacy strings auto-normalised
  on read.
- Docs shipped inside the ZIP: `QUICKSTART.md` (Guglielmo-facing),
  `ADMIN_GUIDE.md` (curator), `ROLLOUT_RUNBOOK.md` (Jordan),
  `FUTURE_OPTIONS.md` (roadmap), `PILOT_CHECKLIST.md` (rehearsal).

## What is NOT shipping (deferred)

- CSRF protection on wizard POSTs — safe for `127.0.0.1` only; revisit
  if the tool ever gets LAN-exposed.
- Automated smoke test that the embedded interpreter can `import` every
  vendored dep — manual smoke has been confirmed for v0.1.0.
- Non-additive DB migrations — v0.1.0 is additive-only; see
  `ROLLOUT_RUNBOOK.md` §6 for the invariant.
- The 261 non-DACH/EU legacy rows in `seeds/_pending_research.json` —
  German-enum, kept out of the wizard on purpose.

## How to install

Follow `QUICKSTART.md` (also included in the ZIP under `docs/`). Total
time from ZIP receipt to first dashboard view: ~10 minutes.

## Known quirks (v0.1.0)

- **CSV `relevance_score` column** contains the final composite score
  (raw + trust bonus + feedback bonus). The `trust_bonus` column is
  informational — do not add it to `relevance_score` in Excel, that
  double-counts. Column will be renamed to `final_score` in v0.1.1.
- **Test-suite warnings**: the pytest run reports one deprecation
  warning from `fastapi.testclient` (starlette-internal, third-party).
  None from our own code path.

## Support

For any issue during the pilot:

- Log file: `%LOCALAPPDATA%\FTNewsMonitor\server.log` — please attach
  the last ~50 lines when reporting.
- Contact **Jordan Kern** via Teams or email.
- Or open the troubleshooting matrix in `ROLLOUT_RUNBOOK.md` §4 first
  for the common failure modes.
