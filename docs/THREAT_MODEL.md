# Threat model — FT News Monitor

## Posture

**Single-user, localhost-only desktop utility on a corporate Windows
laptop.** Bind: `127.0.0.1:8766` (or next free port). Data root:
`%LOCALAPPDATA%\FTNewsMonitor\`. Runs under the logged-in user's
identity — no service account, no elevation, no shared install.

## Trust boundaries

| Boundary | Trust level | Notes |
|---|---|---|
| Logged-in Windows user | fully trusted | If they can read `.env`, they own the tool. |
| Localhost browser (loopback origin) | trusted for state changes | UI runs here; POSTs are legitimate. |
| Cross-origin browser tab | untrusted | Blocked by TrustedHost + Origin guard. |
| Filesystem outside `%LOCALAPPDATA%` | untrusted for reads | Tool doesn't read arbitrary paths. |
| Remote HTTP sources (Google, Bing, GDELT, RSS) | untrusted content | Sanitised via feedparser + prefilter. |
| AWS Bedrock | trusted for AI responses | Model output is treated as untrusted text (never eval'd). |
| Outlook COM (via `pywin32`) | trusted for read | Only reads a single named folder configured by user. |
| AI assistant reading AGENTS.md | must be constrained | Instructions are deterministic; secrets never written from chat. |

## In-scope threats and mitigations

### T1 — Cross-origin browser drive-by (CSRF)

A user visits `evil.example` while the tool is running. The page
issues `fetch('http://127.0.0.1:8766/setup/aws', {method:'POST', ...})`
to overwrite AWS credentials, or `/run-collection` to burn Bedrock
budget.

**Mitigation:** [`app/main.py`](../app/main.py) `_origin_guard`
middleware. State-changing methods with an Origin/Referer that isn't
`127.0.0.1`, `localhost`, or `testserver` → 403.

### T2 — DNS rebinding to read local state

An attacker publishes a domain whose A record briefly points at
`127.0.0.1` (with a low TTL). After TTL flip they can read
`/admin/export-config` or `/settings` via the victim's browser.

**Mitigation:** [`app/main.py`](../app/main.py) `TrustedHostMiddleware`
restricts the `Host` header to loopback + testserver.

### T3 — Credential leak in transcripts

A colleague using an AI assistant to configure AWS pastes the key
into chat. The assistant vendor logs the transcript.

**Mitigation:** [`AGENTS.md`](../AGENTS.md) "Credentials handling —
HARD RULES" section instructs the assistant to refuse in-chat pastes
and redirect the user to Notepad-editing the `.env` file directly.

### T4 — SSRF via user-configurable URLs

Feeds (`feeds.rss_feed`) are user-editable. A malicious feed URL could
target internal services.

**Mitigation:** All fetches go through `httpx.AsyncClient` bound to
public HTTP(S). No proxying of arbitrary URLs. FastAPI does not expose
a `?url=` fetch endpoint. Corporate IT can block outbound via proxy
rules if needed.

### T5 — XSS via feed/AI content

Article title / snippet / reasoning come from external sources
(Google/Bing headlines, AI model output). Rendered in Jinja templates.

**Mitigation:** Jinja autoescape is on (FastAPI default). No template
uses `|safe` on user- or model-derived strings. Verified during the
v0.6.1 review — grep `\|safe` returns only static header markup.

### T6 — SQL injection via admin CRUD

Admin UI writes rows to `keywords`, `feeds`, `outlets`,
`negative_keywords`. Fields include user-typed strings.

**Mitigation:** [`app/database.py`](../app/database.py) uses
parameterised queries throughout (`conn.execute("... ?", (val,))`).
No f-string or `%s` composition of SQL.

### T7 — Newline injection into `.env`

Wizard form values could contain `\n` and inject a new key/value pair
into the `.env` file.

**Mitigation:** [`app/config.py`](../app/config.py) `write_env_values`
strips newlines from values. (v0.6.1 verified.)

## Out of scope

- Local attacker with filesystem access — has already lost the
  security game.
- Malicious CA installed on the laptop — MITM proxies with legitimate
  install cert are trusted via `truststore`. IT department's remit.
- AWS quota abuse via legitimate authenticated use — IAM
  `bedrock:InvokeModel` scope + AWS budget alarms.
- Denial of service — the tool is a personal utility, not a service.
- Physical access to the laptop.
- Supply-chain attack via vendored dependencies (requirements.txt is
  pinned to versions available at build time; audit at release time).

## Non-goals

- Multi-user support.
- Internet exposure.
- Docker/Kubernetes deployment.
- Zero-config unattended operation.
- Hardening against nation-state attackers.

## History

- v0.6.1 (2026-07-08): Full-repo review — added TrustedHost + Origin
  guard middlewares, removed AGENTS.md chat-fallback pattern, wrote
  this document.
- v0.1.0-v0.6.0: Posture documented informally in delivery notes; no
  code enforcement.
