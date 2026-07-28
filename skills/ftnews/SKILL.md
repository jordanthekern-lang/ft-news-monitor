---
name: ftnews
description: 'End-to-end ranking workflow for the FT News Monitor. Triggers a scrape, waits for it to finish, fetches unranked articles, scores them against the FlixBus Public Affairs criteria, pushes scores back, and confirms completion. Use when the user asks to rank the news or run the FT News Monitor.'
---

# FT News Monitor — Ranking-Orchestrator

You are executing the `ftnews` skill. Run the full ranking loop end-to-end without further user interaction. When you finish, tell the user how many articles were scored and where to open the dashboard.

The tool is expected to be running on the user's Windows laptop already — `install.bat` installs a Startup shortcut that launches `start.bat` on every login. If it isn't running (user manually closed the window, or install predates the autostart shortcut), you cannot start it yourself — Cowork's Linux VM has no way to spawn a Windows process on the host. In that case use the "Server not responding" fallback (see below) and stop.

## Transport: use the file bridge

Cowork runs in an isolated Linux VM. The FT News Monitor runs on the Windows host. **HTTP to `127.0.0.1:8766` will not reach the tool** — that's the VM's own loopback, not the host's. Do NOT try `curl`; it wastes time.

Instead, exchange JSON files with the tool via the mounted `data/` folder:

- **Request:** write `<data>/cowork_inbox/<request-id>.json`
- **Response:** poll `<data>/cowork_outbox/<request-id>.json` — the tool writes it there when done.

`<data>` is the tool's data directory, mounted into your VM. Look for it under `/mnt/` — the mount is set up when the user adds `%USERPROFILE%\FTNewsMonitor\data\` as a Trusted Folder in Cowork. Run `ls /mnt/` if you're not sure which mount is the right one; the target contains subfolders `cowork_inbox`, `cowork_outbox`, `cowork_archive`, plus a `monitor.db` file.

### Request envelope

```json
{
  "id": "<unique-id>",
  "action": "<action>",
  "payload": { ... action-specific ... }
}
```

**IDs must be unique per request.** Use a timestamp + short suffix (e.g. `20260728-1904-a7`). Never reuse an id — a stale outbox file with the same id will be read as your response, giving you wrong data.

### Response envelope

```json
{
  "id": "<same id>",
  "status": "ok",
  "result": { ... }
}
```

or `{"status": "error", "error": "..."}`.

### Poll pattern

After writing the inbox file, poll the outbox path every 1–2 seconds for up to 30 seconds. When the file appears, read it and continue.

**Every request MUST have a unique id.** Use `date +%Y%m%d-%H%M%S-<action>` (e.g. `20260728-101557-health`). Do NOT reuse an id from earlier in the session or from a prior run — a stale outbox file with that id will be read as your response, giving you wrong data. This is the single most common failure mode of this skill.

**If no response arrives within 30 seconds → the server is down.** Do NOT interpret older files in the outbox as your answer. Do NOT search the outbox for anything except the exact filename you wrote. Instead go straight to the "Server not responding" fallback below.

**Do not try to delete files.** The plan-9 share often refuses `unlink` from the VM side with EPERM — that's fine, the tool sweeps stale outbox files itself. If deletion fails, ignore the error and move on.

**The token file (`cowork_token.txt`) is NOT needed in bridge mode.** It only gates the HTTP endpoints, which you're not using. Skip reading it.

### Server not responding — fallback

If your health-check request produces no matching outbox file within 30 seconds, the FT News Monitor server is not running. This CAN happen (user closed the terminal, laptop rebooted before autostart kicked in, etc.).

Tell the user, verbatim:

> Der FT News Monitor läuft gerade nicht. Bitte doppelklicke einmal
> `%USERPROFILE%\FTNewsMonitor\start.bat` (ein schwarzes Fenster
> geht auf, das darfst du minimieren aber nicht schließen), dann
> sag mir nochmal „News-Ranking laufen lassen".

Then stop. Do NOT try to score anything, do NOT read older outbox files. There is nothing to work with.

## Available actions

| Action            | Payload                                        | Result                                                              |
|-------------------|------------------------------------------------|---------------------------------------------------------------------|
| `health`          | `{}`                                           | `{"status": "ok", "pending_count": N}`                              |
| `run_collection`  | `{}` (or `{"dry_run": bool, "markets": [...]}`)| `{"status": "started"}` or `{"status": "already_running"}`          |
| `pipeline_status` | `{}`                                           | `{"phase": "collecting"\|"prefiltering"\|"translating"\|"ai_scoring"\|"idle"\|"done", ...}` |
| `list_pending`    | `{"limit": 200}`                               | `{"articles": [...], "total": N, "categories": [...], "themes": [...]}` |
| `submit_scores`   | `{"scores": [...]}`                            | `{"updated": N, "skipped": M, "errors": []}`                        |

## Steps

### 1. Health check

Send `{"action": "health"}` with a fresh id. Wait for your specific outbox file (30 s timeout). If it doesn't arrive → use the "Server not responding" fallback above and stop.

If it does arrive, read `pending_count`. Decide:

- `pending_count == 0` → always run step 2 (fresh scrape).
- `pending_count > 0` and user asked for a fresh scrape → run step 2 anyway.
- `pending_count > 0` and user just said "rank the news" → skip step 2, go to step 3.

### 2. Trigger a scrape

Send `{"action": "run_collection"}` with a fresh id. Wait up to 30 s for the response.

- `status == "started"` → the pipeline is running in the background. Continue to polling.
- `status == "already_running"` → another run is in flight. Continue to polling anyway.

Poll `{"action": "pipeline_status"}` every 5 seconds, each with a fresh id. The response's `phase` field walks through `collecting → prefiltering → translating → ai_scoring` and settles on `done` or `idle`. Stop polling when `phase in {"idle", "done"}`.

Timeout: **15 minutes**. If the pipeline is still running at 15 min, tell the user *"Collection is taking longer than expected. Please run me again in a few minutes."* and stop.

After the pipeline finishes, run **another** `health` check (fresh id!) to see how many articles the fresh scrape produced. Then continue to step 3.

### 3. Fetch pending articles

Send `{"action": "list_pending", "payload": {"limit": 200}}`. The result carries `articles` (list), `categories` (allowed values for scoring), and `themes` (allowed values). If `total == 0`, skip to step 5 with zero scored articles.

Each article has the fields `id`, `title`, `url`, `snippet`, `source_name`, `country` (the market), `language`, and `published_at`.

### 4. Score each article

For every article, apply the **RANKING_PROMPT** below and produce:

```json
{
  "id": <article.id>,
  "relevance_score": 0-10,
  "category": one value from result.categories,
  "themes": subset of result.themes (may be empty),
  "reasoning": "1-2 sentences in English"
}
```

Batch them into one submission:

```json
{
  "action": "submit_scores",
  "payload": {"scores": [<score-obj>, <score-obj>, ...]}
}
```

Send it. Expect `{"updated": N, "skipped": M, "errors": []}`. If `errors` is non-empty, note the count and paste the first three entries in your reply to the user.

### 5. Report

One short paragraph:

> Fertig. `<updated>` Artikel bewertet
> (`<skipped>` übersprungen, weil sie inzwischen weg sind).
> Öffne <http://127.0.0.1:8766/> um die Ergebnisse zu sehen.

Nothing else. Do not list individual articles, scores, or categories — the dashboard is the interface for that.

## Do NOT

- Do NOT try HTTP to `127.0.0.1:8766` — it will fail, wasting ~30-60s per attempt.
- Do NOT read `cowork_token.txt` — the bridge doesn't require it.
- Do NOT reuse request IDs — always generate a fresh one.
- **Do NOT read older files in the outbox.** Only ever read the specific filename that matches an id you just wrote. Outbox files from earlier sessions describe old work — treating them as answers to the current run is the #1 way this skill goes wrong. If your file doesn't appear within 30 s, the server is down (use the fallback), not "your answer is in some other file".
- **Do NOT infer server state from filesystem contents.** `pending_count: 0` in a two-hour-old outbox file tells you nothing about right now.
- Do NOT retry on outbox-delete failures — ignore EPERM, the tool cleans up.
- Do NOT paste article content or scores into the chat. The dashboard displays them.
- Do NOT run a scrape when there are already pending articles unless the user explicitly asks for a fresh scrape.

---

## RANKING_PROMPT

Apply the following criteria to every article. This is the exact prompt used by the tool's internal Bedrock scorer — do not paraphrase.

> You are a filter assistant for a fleet-transformation news monitor
> run by a Public Affairs team.
>
> Task: Score how relevant a news article is to monitoring the fleet
> transformation of long-distance buses/coaches (diesel → zero- or
> low-emission drivetrains).
>
> In scope — score each of the following topics:
> 1. Funding programmes / subsidies for zero-emission buses and
>    coaches: purchase premiums, calls for projects, tax incentives
>    (including HVO), toll exemptions.
> 2. Any bus funding programme — including city buses and regional
>    buses — because a new bus-funding scheme is a Public-Affairs
>    opportunity: we may lobby to have it opened up to coaches. Score
>    these on the merits of the funding signal, not on whether the
>    programme mentions coaches today.
> 3. Policy & regulation (national or EU): CO2 fleet standards, ZEV
>    mandates, AFIR, clean-vehicle rules, decarbonisation strategies
>    relevant to coaches.
> 4. Charging & hydrogen infrastructure relevant to coaches: depots,
>    corridors, en-route charging, megawatt charging.
> 5. Truck / HGV charging infrastructure funding and regulation —
>    including MCS corridors, en-route depots, and grid-connection
>    programmes primarily framed around trucks. Coaches share the
>    same charging power class as trucks, so any truck-charging
>    expansion is a placement opportunity for us.
> 6. Tenders & public procurement of zero-emission coach services.
> 7. Market moves: operators or OEMs piloting or ordering zero-emission
>    coaches, new e-coach models, battery or range developments
>    relevant to long distance.
> 8. Energy costs / supply where directly relevant to bus operations.
>
> Priority weighting (soft guidance, no hard cutoff):
>   Policy/regulatory and funding items score +1–2 higher than
>   otherwise-equal market/technology items of the same informational
>   value. Bus- and truck-adjacent funding items (points 2 and 5) are
>   in-scope on the same footing as coach-specific funding — do NOT
>   down-rank them just because coaches aren't named explicitly.
>
> Geographic scope — ALL markets are to be scored EQUALLY — no bonus
> for DACH or German-language content; a French or Norwegian item is
> exactly as important as a German one. Markets covered: Benelux,
> DACH, EU, France, Italy, Nordics, Portugal.
>
> Coach vocabulary (articles may use any of these terms):
>   coach, long-distance bus, Reisebus, Fernbus (DE), autocar (FR),
>   pullman / autobus a lunga percorrenza (IT), autocarro (PT),
>   touringcar (NL), turistbuss/expressbuss (SV), turbuss/ekspressbuss
>   (NO), turistbus/fjernbus (DA), linja-auto/kaukoliikenne (FI).
>
> Guardrails:
> - City-bus / regional-bus / truck operational news that is NOT about
>   funding, policy, or charging infrastructure (e.g. a local operator
>   retiring a diesel fleet, a truck-model launch) → low score. The
>   point is coach-adjacency of funding/policy signals, not general
>   adjacent-mode reporting.
> - Pure vehicle PR without fleet-transformation relevance → low score.
> - Advertisements, job postings, off-topic content → 0–1.
> - Articles may be in any language — read in the original language and
>   score fairly regardless of language.
>
> Reasoning must be in English.
