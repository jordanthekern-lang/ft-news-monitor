# FT News Monitor

FT News Monitor scans European news sources every day for updates on
long-distance-coach fleet transformation — funding programmes, EU and
national policy, tenders, competitor moves, technology. It runs on the
user's laptop, stores everything locally, and surfaces the day's
relevant articles on a filterable dashboard for the morning PA call.

Markets monitored: **DACH, Benelux, France, Italy, Nordics, Portugal, EU**.

## Which doc do I need?

| I am a …                             | Read …                                       |
|--------------------------------------|----------------------------------------------|
| PA colleague (end user)              | [`docs/QUICKSTART.md`](docs/QUICKSTART.md)   |
| Market curator (edits keywords, feeds, outlets) | [`docs/ADMIN_GUIDE.md`](docs/ADMIN_GUIDE.md) |
| Jordan (build + deploy)              | [`docs/ROLLOUT_RUNBOOK.md`](docs/ROLLOUT_RUNBOOK.md) |
| Stakeholder deciding what next       | [`docs/FUTURE_OPTIONS.md`](docs/FUTURE_OPTIONS.md) |
| Developer contributing code          | (this file — the dev quickstart below)       |

## Pipeline

```
collect → prefilter → translate → AI score → dashboard
```

1. **collect** — RSS feeds, Google News, Bing News, targeted site scrapes
2. **prefilter** — drops articles older than the cutoff (default 180 d)
   and those matching excluded terms
3. **translate** — non-EN articles are auto-translated to English via
   the unofficial Google Translate endpoint (no API key)
4. **AI score** — Claude on Amazon Bedrock rates each article 0–10 for
   fleet-transformation relevance, assigns a category and themes
5. **dashboard** — FastAPI + Tailwind; articles sorted by final score
   (relevance + outlet trust bonus + learning bonus)

## Dev quickstart

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
```

`requirements.txt` holds runtime-only deps (vendored into the release
ZIP by `build/build_release.py`). `requirements-dev.txt` holds
test-only deps (`pytest`, `pytest-asyncio`, `respx`) and is not
shipped.

Copy and edit the `.env` file:

```powershell
copy .env.example .env
notepad .env
```

Key `.env` variables (also settable via the first-run wizard):

| Variable                | Purpose                                                |
|-------------------------|--------------------------------------------------------|
| `AI_PROVIDER`           | `bedrock` (default) or `anthropic`                     |
| `AWS_ACCESS_KEY_ID`     | Bedrock auth (or use `AWS_PROFILE`)                    |
| `AWS_SECRET_ACCESS_KEY` | Bedrock auth                                           |
| `AWS_REGION`            | Default `eu-west-1`                                    |
| `ANTHROPIC_MODEL`       | Default `eu.anthropic.claude-opus-4-7`                 |
| `ANTHROPIC_API_KEY`     | Only when `AI_PROVIDER=anthropic`                      |
| `DASHBOARD_PORT`        | Default `8766`                                         |
| `FM_DATA_DIR`           | Override the DB/log/env location (default: `data/`)    |

Start the dashboard in dev:

```powershell
venv\Scripts\python.exe -m uvicorn app.main:app --port 8766
```

Then open http://localhost:8766. The first launch redirects to
`/setup` for the wizard (home markets, credentials, seed import,
scheduler).

Run a collection cycle manually (bypasses the web UI, honours home
markets):

```powershell
venv\Scripts\python.exe scripts\run_collection.py
```

Optional flags:

| Flag                 | Effect                                                                  |
|----------------------|-------------------------------------------------------------------------|
| `--dry-run`          | Collect only — skip prefilter and AI scoring                            |
| `--markets Italy,EU` | Restrict the run to these markets (default: wizard-configured home markets) |

Run tests:

```powershell
venv\Scripts\python.exe -m pytest tests/ -q
```

## Build a release

```powershell
venv\Scripts\python.exe build\build_release.py
```

Produces `dist/FTNewsMonitor-<VERSION>-win64.zip` — a portable
extract-and-run bundle with an embedded CPython 3.13. See
`docs/ROLLOUT_RUNBOOK.md` for the full release flow, AWS provisioning
guidance, and the Italy install checklist.

## Project layout

```
foerder-monitor-market-edition/
|- app/                    FastAPI dashboard + pipeline + admin
|  |- main.py              routes, filter parsing, export endpoints
|  |- setup_wizard.py      /setup/* first-run flow
|  |- admin.py             /admin/* CRUD for the four config tables
|  |- pipeline.py          collect + prefilter + translate + AI score
|  |- scheduler.py         APScheduler daily job + catch-up
|  |- export.py            build_markdown() / build_csv()
|  |- config.py            .env loading, DATA_DIR resolution, VALID_*
|  |- runtime_settings.py  app_settings table (home_markets, ...)
|  |- templates/           Jinja2 + Tailwind
|- scripts/
|  |- launch.py            packaged entrypoint (start.bat calls this)
|  |- run_collection.py    CLI for one-shot / cron
|  |- import_seed.py       import a seed JSON into the DB
|  |- validate_seeds.py    schema + optional network probe
|- seeds/                  shipped seed packs (dach, italy, ...)
|- build/build_release.py  ZIP builder
|- tests/                  pytest suite
|- docs/                   see the table at top
|- data/                   dev-only DB + logs (gitignored)
|- .env.example
|- requirements.txt        (runtime — vendored into the release)
|- requirements-dev.txt    (test-only)
|- start.bat               packaged launcher
|- VERSION
```

## License / attribution

Internal FlixBus tool. No external license — do not redistribute
outside FlixBus PA and eng.
