# `ftnews` — Cowork skill for FT News Monitor

This directory contains a Claude Cowork skill that runs the FT News
Monitor's ranking loop end-to-end. Install once per colleague, then
either invoke on demand or run it on a Cowork schedule.

## What it does

When triggered (either by a user prompt like *"News-Ranking laufen
lassen"* or by a Cowork scheduled task), the skill:

1. Health-checks the tool via the file bridge.
2. Triggers a scrape via `run_collection` if there are no pending
   articles, and polls `pipeline_status` until the pipeline is idle.
3. Fetches unranked articles via `list_pending`.
4. Scores each one using the FlixBus Public Affairs criteria (the
   prompt is inlined in `SKILL.md`).
5. Pushes the scores back via `submit_scores`.
6. Reports *"N Artikel bewertet — Öffne <http://127.0.0.1:8766/>"*.

Cowork runs in a Linux VM that cannot reach the Windows host's
loopback, so the skill talks to the tool exclusively via JSON files
in `%USERPROFILE%\FTNewsMonitor\data\cowork_inbox\` and `.../cowork_outbox\`.
The tool watches these directories automatically when
`AI_PROVIDER=cowork`.

## Install in Cowork

1. In the FT News Monitor folder on your laptop, locate
   `%USERPROFILE%\FTNewsMonitor\skills\ftnews\`.
2. Open Cowork → **Settings → Skills → Import** (or the equivalent in
   your Cowork build).
3. Point Cowork at the `ftnews` folder. The skill's frontmatter
   description makes it auto-discover when a user asks *"News-Ranking
   laufen lassen"*.
4. Add `%USERPROFILE%\FTNewsMonitor\data\` as a **Trusted Folder** in
   Cowork so the skill can read the shared token file and (in
   fallback mode) exchange JSON envelopes.

## Run it

### On demand
Type in Cowork: *"News-Ranking laufen lassen"* (or any variant — the
skill's `description` field covers a few phrasings).

### On a schedule
If your Cowork build supports scheduled skills, add a weekly job that
invokes `ftnews`. If not, create a Windows Task Scheduler job that
opens Cowork with the prompt *"Run ftnews now"* at your chosen
cadence. Full walk-through: [`docs/SKILL_INSTALL.md`](../../docs/SKILL_INSTALL.md).

## Update the skill

The skill file is version-pinned to `%USERPROFILE%\FTNewsMonitor\`.
When you run `update.bat` in the tool folder, the latest `SKILL.md`
is pulled with the rest of the repo. Re-import it in Cowork if you
want the new version live immediately, or wait for Cowork to reload
on its next start.
