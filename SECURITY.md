# Security Policy

## Scope

FT News Monitor is a **single-user, localhost-only desktop tool** distributed
as a self-contained ZIP or via `git clone`. It runs a FastAPI server bound
to `127.0.0.1` and stores data under `%LOCALAPPDATA%\FTNewsMonitor\`. It is
**not** a multi-tenant service, not internet-exposed, and not intended to be
deployed to shared infrastructure.

See [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) for the full posture,
in-scope threats, and explicit non-goals.

## Reporting a vulnerability

If you find a security issue:

1. **Do not** open a public GitHub issue.
2. Email the maintainer directly — the address is on the repo owner's
   GitHub profile. Include a proof-of-concept if possible.
3. Please allow up to 14 days for triage before public disclosure.

## What is in scope

- Bugs that allow a **remote website in the user's browser** to
  read/write local tool state (CSRF, DNS rebinding, XSS).
- Bugs that leak **credentials** (AWS keys, Outlook data) into logs,
  HTTP responses, or git history.
- Bugs that allow **local privilege escalation** on the user's laptop
  via crafted DB values, feed URLs, seed files, etc.
- Bugs in the `AGENTS.md` runbook that would cause an AI assistant to
  leak credentials or write them to the wrong location.

## What is out of scope

- **A local attacker with filesystem read/write already owns the box.**
  Once someone can read `%LOCALAPPDATA%\FTNewsMonitor\.env` or edit
  `monitor.db`, they can do anything the tool can do (call Bedrock,
  open Outlook items). This is by design — the tool is a personal
  productivity utility for the logged-in user.
- **Corporate proxy/MITM interception.** The tool trusts the Windows
  cert store via `truststore`. A malicious MITM CA installed on the
  laptop is out of scope.
- **AWS-Bedrock cost abuse via legitimate use.** The tool costs money
  to run. Rate limits and cost caps are the user's / IT's job at the
  AWS side (IAM policy scoping, budget alarms).
- **The AI assistant obeying malicious user instructions.** If the user
  tells their AI to `rm -rf` the repo, that's their choice. AGENTS.md
  covers the *unattended-AI* case (assistant following the runbook
  faithfully), not the *attended-user-directs-AI-against-themselves*
  case.

## Hardening summary (v0.6.1)

- `TrustedHostMiddleware`: only `127.0.0.1` / `localhost` / `testserver`
  Host headers accepted. Blocks DNS rebinding.
- Origin/Referer guard: state-changing HTTP methods (POST/PUT/PATCH/
  DELETE) with a non-loopback Origin or Referer are rejected 403.
  Blocks browser-driven CSRF.
- `.env` never included in ZIP or git; secrets never printed in logs
  or HTTP responses.
- AGENTS.md forbids writing credentials pasted into chat.
- Uvicorn binds to `127.0.0.1` (see `scripts/launch.py`). No
  `0.0.0.0` mode is exposed.
