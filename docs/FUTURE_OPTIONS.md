# FT News Monitor — Future Options

Audience: someone (Jordan's line manager, another stakeholder) deciding
whether to invest more in this tool, migrate it to a larger platform,
or leave it as is. Each option below is scoped, ranked, and honest
about what it would cost.

---

## 1. Claude Cowork as the successor distribution model

If the FlixBus enterprise Claude Cowork tenancy comes online, the
current distribution model — a ZIP file per user, an AWS key pair per
user, a laptop-local SQLite database per user — can be replaced by a
single shared Cowork instance that all PA colleagues authenticate to
via corporate SSO.

Benefits:

- Zero-install for end users: they open a URL.
- No AWS key handling: authentication is corporate SSO, model access is
  centrally managed.
- No per-laptop state: article history and feedback signals are shared
  across the team.

Risks and open questions:

- Cowork rollout timeline is not yet confirmed.
- Article history from the seven per-laptop DBs would need to be
  merged into the shared instance (feasible: SQLite → central DB
  migration script is a few days of work).
- Some users will not want their star/ignore signals visible to peers;
  a per-user filter view would be needed.

**Recommendation:** revisit this option quarterly. If Cowork lands in
2026, migrate; otherwise the current model works for a small pilot
group (< 15 users).

---

## 2. Feedback-precision report

The `feedback` table already records every star and ignore per article,
with domain aggregates surfaced under `/settings → Learning from your
clicks`. A weekly precision/recall report would let market curators
tune keywords with data instead of gut feel:

- **Precision**: of the articles the model scored ≥ 6, how many did
  the user star vs. ignore?
- **Recall**: of the articles the user starred, how many were scored
  ≥ 6 by the model? (Requires a small held-out set for reference —
  see option 3.)

Effort: roughly one week — a new dashboard page pulling from existing
tables, plus a small "download PDF" export.

**Recommendation:** cheap and high-value once each market has ~200 star
clicks accumulated. Schedule after Italy + one more market pass the
200-click threshold.

---

## 3. Backtesting harness

Inspiration: the open-source `nostoz/news_monitor` project on GitHub
maintains a curated hold-out set of ~500 articles with gold labels and
replays every prompt / seed change against it before shipping. Applied
here it would look like:

- One-time build of a hold-out set: 500 articles across markets, each
  labelled "relevant" / "not relevant" by two independent PA
  reviewers.
- A CLI replay: `scripts/backtest.py --prompt new_prompt.txt` scores
  the set with the modified prompt and reports precision / recall
  deltas against the current prompt.
- Gate seed refreshes and prompt changes on "no worse than the last
  release" on the hold-out set.

Effort: ~2 developer days for the harness itself, plus 1–2 days of PA
reviewer time to label the hold-out set. Ongoing maintenance is
minimal (add ~20 articles per quarter to keep the set current).

**Recommendation:** worth doing before the next major seed refresh
(rough target: after 2–3 markets are live). Without it, every seed
change is "hope it did not make things worse".

---

## 4. Central digest email (rejected, documented for closure)

Previously considered: a shared central instance that emails a daily
digest to a mailing list. Rejected because:

- The dashboard-as-primary-interface is the right pattern for PA staff
  who work with articles **interactively** (open source, star, ignore,
  export to Confluence). Email digests are consumed passively — no
  feedback loop, no learning signal, no interactive filter.
- Corporate mailboxes are already saturated. A daily digest joins
  hundreds of unread emails and gets archive-scrolled.
- Confluence recaps produced from `Copy as Markdown` already cover the
  "share with people who don't run the tool" use case.

Documented here so nobody re-opens the option casually. Reopen only if
a stakeholder is willing to name a specific reader who prefers email
over the dashboard and would actually act on the digest.

---

## 5. Programmatic JSON API

Currently only `/api/stats` returns JSON. If downstream tools want
article lists (a Slack digest bot, a Confluence auto-page, a Power BI
dashboard), the article list route already exists internally —
`_articles_for_filters()` in `app/main.py` is the shared filter core
for the dashboard and the export routes. Wiring a JSON view is a small
change:

- Add a `GET /api/articles` route that reuses `_parse_dashboard_filters`
  + `_articles_for_filters` and returns the row dicts as JSON.
- Add a simple API-key check via a header value pulled from `.env`
  (`FM_API_KEY=<random-string>`). The tool is localhost-only, but an
  API key raises the bar against accidental use from other apps on
  the same machine.

Effort: half a day, including tests.

**Recommendation:** do this only when a specific downstream consumer
asks for it. Building it "in case someone needs it" adds a surface
that has to be secured without producing user value.
