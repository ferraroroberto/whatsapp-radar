# Project Instructions

Canonical instructions for AI coding agents working in this repository. `AGENTS.md` points here for non-Claude tools.

## This Repository

WhatsApp Radar is a local-first personal utility for classifying new WhatsApp chat messages and surfacing only actionable items through a separate notification channel. Treat it as a sensitive-data project even though the repository is public.

It follows the fleet's standard layout (as in `E:\automation\app-launcher`): UI surfaces in `app/`, logic in `src/`, committed config in `config/`, reference docs in `docs/`, the read-only Node/Baileys connector in `sidecar/`. It is **not** an installable package — it runs from a checkout.

```
whatsapp-radar/
  app/cli/main.py        # argparse CLI (status|ingest|chats|monitor|ignore|review|scan|notify|resync|reprocess|tray)
  app/webapp/            # FastAPI admin PWA: server.py, middleware.py, manager.py,
                         #   routers/ (misc, auth, webauthn), static/ (vanilla-JS shell)
  app/tray/tray.py       # pystray surface that owns the webapp lifecycle
  src/                   # logic, imported as `from src.…`
    config.py  models.py  webapp_config.py  webauthn_gate.py  static_versioning.py
    connector/ (base, fixture, linked_device)
    db/ (store.py facade over connection/chats/messages/runs/dashboard/
         sync_log/reprocess_support.py, schema.sql)
    analysis/ (classifier, contract, keywords, review, prompts/)
    notify/ (base, factory, telegram)   report/digest.py
    fixtures/sample_chats.json
  config/                # committed defaults (default.json) + *.sample templates;
                         #   webapp_config.json / webauthn_devices.json / cloudflared.yml + .env are gitignored
  scripts/               # gen_token, set_password, gen_icons, run_named_tunnel, verify-before-ship.ps1
  sidecar/               # read-only Node/Baileys connector
  webapp/                # runtime log output (gitignored contents)
  docs/  tests/ (+ tests/e2e Playwright)
  launcher.py  wr.bat    # CLI entry points
  tray.bat  webapp.bat  webapp_tunnel_named.bat  setup.bat   # webapp entry points
  requirements.txt  requirements-dev.txt  pytest.ini
  pyproject.toml         # tool config only (ruff/mypy) — no packaging
```

Run the CLI with `python launcher.py <command>`, `python -m app.cli.main <command>`, or the `wr.bat <command>` wrapper.

### Internal architecture

[`docs/architecture.mmd`](docs/architecture.mmd) is a hand-authored Mermaid diagram of this repo's own internal structure (the CLI/tray/webapp entry points, the connector boundary, the scan pipeline, storage, notify, and the external dependencies) — the per-repo counterpart to the fleet-wide diagram `fleet-config`'s `/system-map` generates. Update it in the same PR as any material structural change (a connector added, a pipeline stage moved, a router split) — same anti-staleness contract as this repo's `.fleet.toml` `description` field. It is not auto-generated and not covered by `scripts/verify-before-ship.ps1`.

### Admin webapp & tray

The phone-first admin PWA is **FastAPI + vanilla JS** on port **8455** (mirrors App Launcher; no second service port). `tray.bat` adopt-or-spawns it; `webapp.bat` runs it standalone. Auth is the App Launcher model: a bearer token (loopback bypasses), an optional login password, WebAuthn passkeys (Tailscale-only ceremonies), Tailscale TLS, and dormant Cloudflare scaffolding. Secrets + passkey state live in the gitignored `config/webapp_config.json`; non-secret `enabled`/`host`/`port` live in `config/default.json` under `webapp`. All four tabs (Dashboard · Chats & Config · Execution · Audit) are live; see `README.md` §"Admin Webapp" for per-tab endpoint lists.

**Safe restart (never blanket-kill python):** the tray and `tray.bat --restart` reclaim **only** the `:8455` PID scoped to this repo's `.venv` — never a blanket `pythonw`/`python` kill, which would take down sister apps (App Launcher, local-llm-hub, …). To restart by hand, find the owner with `Get-NetTCPConnection -LocalPort 8455` and stop that PID, then relaunch via `tray.bat`. **Build confirmation:** `GET /api/version` returns `{git_sha, built_at, asset_hash}` — after a restart the `git_sha` should match `HEAD` and `asset_hash` should change when static assets did.

## Layout & Imports

- `src/` is the logic package; `app/` holds UI surfaces. Import logic with absolute paths — `from src.config import load_config`, `from src.db import store`. Do **not** reintroduce an installable package or a `whatsapp_radar.` namespace.
- Subpackage `__init__.py` files may re-export their own submodules with relative `from .x` imports; everything else (cross-subpackage and `app/` → `src/`) uses `from src.…`.
- Bundled assets (`db/schema.sql`, `analysis/prompts/*`, `fixtures/*.json`) are resolved by path relative to `__file__`, never via `importlib.resources` package-data.
- A script that lives **outside** the repo but imports `src.*`/`app.*` needs `$env:PYTHONPATH = (Get-Location).Path;` before `& .\.venv\Scripts\python.exe <path>`. Prefer `& .\.venv\Scripts\python.exe -m <module>` from the repo root when the script can live in-tree (a gitignored scratch dir is fine) — `-m` puts CWD on `sys.path` and needs no env var.

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
- The admin UI is **FastAPI + vanilla JS** mirroring App Launcher — not Streamlit (landed in #8; see "Admin webapp & tray" above). Its secrets (bearer token, login password, Telegram token/chat id, passkey state) live in the gitignored `config/webapp_config.json`; `WR_TELEGRAM_*` env / `config/local.json` still override it.

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
  - /          (Dashboard · Chats · Run · Audit tabs)

## Verification

Run the gate from the repo root with the project venv:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src app
```

The suite runs entirely offline against sanitized fixtures (no WhatsApp credentials, no network, no Telegram). Do not claim tests pass without running them.

## CI expectations

- Workflow `.github/workflows/e2e.yml`, job `verify-before-ship`, on every PR. **Advisory, not required** (no branch protection) — the local gate (`pytest` / `ruff` / `mypy`) is the contract.
- Typical green: **~2 min**. Investigate at **>5 min**; treat as wedged at **>8 min**.
- Flaky leg: the Playwright **WebKit/iPhone** e2e projection can wedge the browser on the hosted runner. `timeout-minutes: 30` caps a wedge. A wedge is a flake, not the diff.
- CI's only signal beyond the local gate is the **e2e suite** (skipped locally — `pytest` shows ~13 skipped). Its e2e surface = `app/webapp/`, `app/tray/`, `tests/e2e/`, static assets under `app/webapp/static/`. A diff touching **none** of these (e.g. `src/db/`, `src/analysis/`, `src/notify/`, docs) gains nothing from CI.
