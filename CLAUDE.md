# Project Instructions

Canonical instructions for AI coding agents working in this repository. `AGENTS.md` points here for non-Claude tools.

## This Repository

WhatsApp Radar classifies new WhatsApp chat messages and surfaces only actionable items through a separate notification channel. Treat it as a sensitive-data project even though the repository is public.

Fleet standard layout (as in `E:\automation\app-launcher`): UI in `app/`, logic in `src/`, committed config in `config/`, docs in `docs/`, the read-only Node/Baileys connector in `sidecar/`. **Not** an installable package — runs from a checkout.

```
whatsapp-radar/
  app/cli/main.py        # argparse CLI (status|ingest|chats|monitor|ignore|review|scan|gmail-survey|
                         #   notify|resync|reprocess|calendar-scan|traffic-check|tray)
  app/webapp/            # FastAPI admin PWA: server.py, middleware.py, manager.py, runs.py,
                         #   routers/ (ack, audit, auth, chats, config, dashboard, execution,
                         #   family, misc, sidecar, webauthn, _helpers), static/ (vanilla-JS shell)
  app/tray/tray.py       # pystray surface that owns the webapp lifecycle
  calendar_readonly/     # portable Google Calendar read client (mirrors gmail_readonly/)
  calendar_write/        # portable Google Calendar write client (family calendar automation)
  gmail_readonly/        # portable Gmail read client
  google_oauth_common/   # shared installed-app OAuth bootstrap the three clients above wrap
  src/                   # logic, imported as `from src.…`
    config/ (package: __init__.py's load_config aggregates one module per subsystem —
             hub/transcription/tts/telegram/tripwire/gmail/calendar/traffic/presence/family)
    models.py  webapp_config.py  webauthn_gate.py  static_versioning.py
    paths.py  tts_client.py  speech_profile.py  subprocess_flags.py  runresult.py  _loopback_http.py
    connector/ (base, factory, fixture, gmail, linked_device, preflight, sidecar)
    db/ (store.py facade over connection/ack/chats/messages/runs/dashboard/sync_log/
         reprocess_support/retention/tripwire, plus sync.py/reprocess.py, schema.sql)
    analysis/ (classifier, contract, keywords, pipeline, reminders, review, source_funnel,
               summarize, transcription, tripwire, gmail_survey, _common, prompts/)
    notify/ (base, factory, telegram, alert, delivery)   report/digest.py
    family/ (calendar_scan, calendar_source, dedup, rules, traffic_check)
    presence/client.py     traffic/routes_client.py
    fixtures/sample_chats.json
  config/                # committed defaults (default.json) + *.sample templates;
                         #   webapp_config.json / webauthn_devices.json / cloudflared.yml + .env are gitignored
  scripts/               # gen_token, set_password, gen_icons, gen_tailscale_cert, run_named_tunnel,
                         #   auth_calendar, auth_calendar_write, auth_gmail, gmail_school_backtest,
                         #   traffic_smoke, run-e2e.ps1, verify-before-ship.ps1
  sidecar/               # read-only Node/Baileys connector
  webapp/                # runtime log output + certificates/ (gitignored contents)
  docs/  tests/ (+ tests/e2e Playwright)
  launcher.py  wr.bat    # CLI entry points
  tray.bat  webapp.bat  webapp_tunnel_named.bat  setup.bat   # webapp entry points
  requirements.txt  requirements-dev.txt  pytest.ini
  pyproject.toml         # tool config only (ruff/mypy) — no packaging
```

Run the CLI with `python launcher.py <command>`, `python -m app.cli.main <command>`, or the `wr.bat <command>` wrapper.

### Internal architecture

[`docs/architecture.mmd`](docs/architecture.mmd) is a hand-authored Mermaid diagram of this repo's internal structure. Update it in the same PR as any material structural change (connector added, pipeline stage moved, router split) — anti-staleness contract, same as `.fleet.toml`'s `description`. Not auto-generated, not covered by `scripts/verify-before-ship.ps1`.

### Admin webapp & tray

FastAPI + vanilla JS on port **8455** (mirrors App Launcher; no second service port). `tray.bat` adopt-or-spawns it; `webapp.bat` runs it standalone. Auth: bearer token (loopback bypasses), optional login password, WebAuthn passkeys (Tailscale-only ceremonies), Tailscale TLS, dormant Cloudflare scaffolding. Secrets + passkey state live in gitignored `config/webapp_config.json`; non-secret `enabled`/`host`/`port` live in `config/default.json` under `webapp`. Six tabs (Dashboard · Messages & Config · Execution · Audit · Family · Follow-ups) are live; endpoint lists in `README.md` §"Admin Webapp".

**Safe restart (never blanket-kill python):** tray and `tray.bat --restart` reclaim **only** the `:8455` PID scoped to this repo's `.venv` — never a blanket `pythonw`/`python` kill (would take down sister apps). By hand: find the owner with `Get-NetTCPConnection -LocalPort 8455`, stop that PID, relaunch via `tray.bat`. **Build confirmation:** `GET /api/version` returns `{git_sha, built_at, asset_hash}` — after a restart `git_sha` should match `HEAD` and `asset_hash` should change when static assets did.

## Layout & Imports

- `src/` is the logic package; `app/` holds UI surfaces. Import with absolute paths — `from src.config import load_config`, `from src.db import store`. Do **not** reintroduce an installable package or a `whatsapp_radar.` namespace.
- `calendar_readonly/`, `calendar_write/`, `gmail_readonly/` are portable Google API packages deliberately outside `src/` (liftable into another repo unchanged), imported as `from calendar_readonly…` / `from calendar_write…` / `from gmail_readonly…` — an intentional exception to the absolute-`from src.…` rule. `google_oauth_common/` is a fourth portable sibling: the installed-app OAuth bootstrap, token load/refresh, and atomic-write steps shared by all three, imported as `from google_oauth_common…`. Same "no imports from `src`/`app`/`scripts`" contract — lifting one of the three clients means copying `google_oauth_common/` alongside it (`docs/gmail-reuse.md`).
- Subpackage `__init__.py` files may re-export their own submodules with relative `from .x` imports; everything else (cross-subpackage and `app/` → `src/`) uses `from src.…`.
- Bundled assets (`db/schema.sql`, `analysis/prompts/*`, `fixtures/*.json`) resolve by path relative to `__file__`, never via `importlib.resources` package-data.
- Out-of-tree script importing `src.*`/`app.*` → global PYTHONPATH gotcha applies (`$env:PYTHONPATH = (Get-Location).Path;` before `& .\.venv\Scripts\python.exe <path>`, or prefer `-m <module>` from repo root if it can live in-tree).

## Hard Privacy Rules

- Never commit real WhatsApp auth state, session credentials, QR codes, message databases, message exports, chat names, phone numbers, school names, screenshots, or notification tokens.
- Use sanitized fixtures only. Example chat names should be generic, such as `School Parents Group` or `Class 4A Group`.
- Keep all runtime data under ignored paths such as `auth/`, `sessions/`, `data/`, or local config files.
- Do not add telemetry or external logging for message content.
- Do not use WhatsApp data to train, fine-tune, or improve shared AI models.

## WhatsApp Integration Guardrails

- The application behavior must be read-only: ingest, classify, and notify outside WhatsApp.
- Do not implement WhatsApp sending, auto-replies, reactions, read-receipt manipulation, contact scraping, broadcast, or group administration unless a future issue explicitly changes scope.
- Keep the connector boundary isolated so the rest of the system can be tested with sanitized fixtures and can swap connector implementations later.
- Document any unofficial library risk clearly in README or durable docs before implementation.

## Fleet Integration

- Reuse `E:\automation\local-llm-hub` for LLM calls.
- Use App Launcher for scheduling and launch surfaces where appropriate: Jobs for periodic digest runs, Apps for a small admin UI.
- The admin UI is **FastAPI + vanilla JS** mirroring App Launcher — not Streamlit (landed in #8). Its secrets (bearer token, login password, Telegram token/chat id, passkey state) live in gitignored `config/webapp_config.json`; `WR_TELEGRAM_*` env / `config/local.json` still override it.

## Implementation Conventions

- Prefer a small, explicit architecture over framework ceremony.
- Keep connector, storage, analysis, notification, and UI boundaries separate.
- Store durable state in SQLite unless a later issue justifies something heavier.
- Use structured JSON outputs for LLM classification and validate them before advancing cursors.
- Advance a per-chat cursor only after analysis state is persisted.
- Notification delivery should be retryable independently of message analysis.

## UX surface
*The design-conformance gate the `/issue-{start,finish,yolo}` skills read (convention: `project-scaffolding#83`). This is a live, parseable block — the product is the FastAPI + static PWA under `app/webapp/`.*

- design spec applies: yes        # `no` would make the gate a permanent no-op; this repo serves a real PWA
- paths:
  - app/webapp/static/**/*.css
  - app/webapp/static/**/*.{js,html}
- key views:                      # single tabbed SPA served at `/`
  - /          (Dashboard · Messages & Config · Execution · Audit · Family · Follow-ups tabs)

## Verification

Run the gate from the repo root with the project venv:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src app
```

Runs entirely offline against sanitized fixtures (no WhatsApp credentials, no network, no Telegram). Do not claim tests pass without running them.

## CI expectations

- Workflow `.github/workflows/e2e.yml`, job `verify-before-ship`, on every PR. **Advisory, not required** (no branch protection) — the local gate (`pytest` / `ruff` / `mypy`) is the contract.
- Typical green: **~2 min**. Investigate at **>5 min**; treat as wedged at **>8 min**.
- Flaky leg: the Playwright **WebKit/iPhone** e2e projection can wedge the browser on the hosted runner. `timeout-minutes: 30` caps a wedge. A wedge is a flake, not the diff.
- CI's only signal beyond the local gate is the **e2e suite** (skipped locally — `pytest` shows ~13 skipped). Its e2e surface = `app/webapp/`, `app/tray/`, `tests/e2e/`, static assets under `app/webapp/static/`. A diff touching **none** of these (e.g. `src/db/`, `src/analysis/`, `src/notify/`, docs) gains nothing from CI.
