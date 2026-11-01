# Project Instructions

`AGENTS.md` points here, so every coding agent reads this same instruction set.

## This Repository

WhatsApp Radar classifies new WhatsApp chat messages and surfaces only actionable items through a separate notification channel. **Treat it as a sensitive-data project even though the repository is public.**

Fleet standard layout (as in `E:\automation\app-launcher`): UI in `app/`, logic in `src/`, committed config in `config/`, docs in `docs/`, the read-only Node/Baileys connector in `sidecar/`. Runs from a checkout, never installed.

```
whatsapp-radar/
  app/cli/main.py        # argparse CLI (status|ingest|chats|monitor|ignore|review|scan|gmail-survey|
                         #   notify|resync|reprocess|calendar-scan|traffic-check|tray)
  app/webapp/            # FastAPI admin PWA: server.py, middleware.py, manager.py, runs.py,
                         #   routers/, static/ (vanilla-JS shell)
  app/tray/tray.py       # pystray surface that owns the webapp lifecycle
  src/                   # logic, imported as `from src.…`
    calendar_readonly/ calendar_write/ gmail_readonly/   # portable Google clients (see Layout & Imports)
    google_oauth_common/ # installed-app OAuth bootstrap the three clients wrap
    config/ (load_config aggregates one module per subsystem)   models.py  paths.py  ...
    connector/ (base, factory, fixture, gmail, linked_device, preflight, sidecar)
    db/ (store.py facade over per-table modules, sync.py/reprocess.py, schema.sql)
    analysis/ (classifier, pipeline, tripwire, transcription, prompts/, ...)
    notify/ (base, factory, telegram, alert, delivery)   report/digest.py
    family/ (calendar_scan, rules, traffic_check, travel_blocks, travel_blocks_write — the only
             path that deletes from calendars; analysis/reminders.py is the insert-only writer)
    presence/  traffic/  task_os/    fixtures/sample_chats.json
  config/                # committed defaults (default.json) + *.sample templates;
                         #   webapp_config.json / webauthn_devices.json + .env gitignored
  scripts/               # helper CLIs + run-e2e.ps1, verify-before-ship.ps1
  sidecar/               # read-only Node/Baileys connector
  webapp/                # runtime log output + certificates/ + cloudflared.yml (gitignored contents)
  docs/  tests/ (+ tests/e2e Playwright)
  launcher.py  wr.bat    # CLI entry points
  tray.bat  webapp.bat  webapp_tunnel_named.bat  setup.bat   # webapp entry points
  requirements.txt  requirements-dev.txt  pytest.ini
  pyproject.toml         # tool config only (ruff/mypy) — no packaging
```

CLI entry points: `python launcher.py <command>`, `python -m app.cli.main <command>`, or `wr.bat <command>`.

### Internal architecture

[`docs/architecture.mmd`](docs/architecture.mmd) — hand-authored Mermaid of internal structure. Not auto-generated, not covered by `scripts/verify-before-ship.ps1`: update it in the same PR as any material structural change (connector added, pipeline stage moved, router split).

### Admin webapp & tray

- FastAPI + vanilla JS on port **8455** (mirrors App Launcher — deliberately not Streamlit, #8); no second service port. `tray.bat` adopt-or-spawns it; `webapp.bat` runs it standalone. Four tabs (named under **UX surface** below; follow-ups are Telegram-only), plus the Settings dialog from every pane's gear. Family and Settings rows open sheets built on `static/edit-sheet.js` that save on Done; endpoint lists in `docs/webapp.md`.
- Auth: bearer token (loopback bypasses), optional login password, WebAuthn passkey enrollment (Tailscale-only; provisioning state, no passkey sign-in, #353), Tailscale TLS, dormant Cloudflare scaffolding.
- Secrets + passkey state (bearer token, login password, Telegram token/chat id, passkeys) live in gitignored `config/webapp_config.json`; the Telegram pair resolves `WR_TELEGRAM_*` env, then `webapp_config.json`, then `config/local.json`/`default.json`. Non-secret `enabled`/`host`/`port` also live in `webapp_config.json` (defaults in `src/webapp_config.py`).
- **Safe restart (never blanket-kill python):** tray and `tray.bat --restart` reclaim **only** the `:8455` PID scoped to this repo's `.venv` — never a blanket `pythonw`/`python` kill (would take down sister apps). By hand: find the owner with `Get-NetTCPConnection -LocalPort 8455`, stop that PID, relaunch via `tray.bat`.
- **Build confirmation:** `GET /api/version` returns `{git_sha, built_at, asset_hash}` — after a restart `git_sha` should match `HEAD` and `asset_hash` should change when static assets did.

## Layout & Imports

- `src/` is the logic package; `app/` holds UI surfaces. Import with absolute paths — `from src.config import load_config`, `from src.db import store`. Do **not** reintroduce an installable package or a `whatsapp_radar.` namespace.
- `src/calendar_readonly/`, `src/calendar_write/`, `src/gmail_readonly/` and their shared `src/google_oauth_common/` bootstrap (installed-app OAuth, plus the token load/refresh and atomic-write steps all three wrap) are portable packages, liftable into another repo unchanged: inside them every import is relative (`from .core`, `from ..google_oauth_common.x`), never `from src.…`, and they import nothing from `src`/`app`/`scripts`. Callers import them as `from src.<package>…`; lifting a client means copying `google_oauth_common/` alongside it under the same parent (`docs/gmail-reuse.md`).
- Subpackage `__init__.py` files may re-export their own submodules with relative `from .x` imports; everything else (cross-subpackage and `app/` → `src/`) uses `from src.…`.
- Bundled assets (`db/schema.sql`, `analysis/prompts/*`, `fixtures/*.json`) resolve by path relative to `__file__`, never via `importlib.resources` package-data.
- Out-of-tree script importing `src.*`/`app.*` → the global PYTHONPATH gotcha; prefer `-m <module>` from the repo root.

## Hard Privacy Rules

- Never commit real WhatsApp auth state, session credentials, QR codes, message databases, message exports, chat names, phone numbers, school names, screenshots, or notification tokens.
- Use sanitized fixtures only; example chat names stay generic, such as `School Parents Group` or `Class 4A Group`.
- Keep all runtime data under ignored paths such as `auth/`, `sessions/`, `data/`, or local config files.
- Do not add telemetry or external logging for message content.
- Do not use WhatsApp data to train, fine-tune, or improve shared AI models.

## WhatsApp Integration Guardrails

- Application behavior must be read-only: ingest, classify, and notify outside WhatsApp.
- Do not implement WhatsApp sending, auto-replies, reactions, read-receipt manipulation, contact scraping, broadcast, or group administration unless a future issue explicitly changes scope.
- Keep the connector boundary isolated — the rest of the system stays testable with sanitized fixtures and connector implementations stay swappable.
- Document any unofficial library risk clearly in README or durable docs before implementation.

## Fleet Integration

- LLM calls go through `E:\automation\local-llm-hub`.
- App Launcher owns scheduling and launch surfaces: Jobs for periodic digest runs, Apps for the small admin UI.

## Implementation Conventions

- Prefer a small, explicit architecture over framework ceremony; keep connector, storage, analysis, notification, and UI boundaries separate.
- Store durable state in SQLite unless a later issue justifies something heavier.
- Use structured JSON outputs for LLM classification and validate them before advancing cursors.
- Advance a per-chat cursor only after analysis state is persisted.
- Notification delivery should be retryable independently of message analysis.

## UX surface
*Live, parseable design-conformance-gate block read by `/issue-{start,finish,yolo}` (convention: `project-scaffolding#83`). The product is the FastAPI + static PWA under `app/webapp/`.*

- design spec applies: yes        # `no` would make the gate a permanent no-op; this repo serves a real PWA
- paths:
  - app/webapp/static/**/*.css
  - app/webapp/static/**/*.{js,html}
- key views:                      # single tabbed SPA served at `/`
  - /          (Home · Activity · Inbox · Family tabs)

## Verification

Gate, from the repo root, with the project venv:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src app
.\.venv\Scripts\python.exe -m scripts.static_ratchet_gate
.\.venv\Scripts\python.exe -m scripts.docs_ratchet_gate
.\.venv\Scripts\python.exe -m scripts.comment_ratchet_gate
.\.venv\Scripts\python.exe -m scripts.structure_ratchet_gate
.\.venv\Scripts\python.exe -m scripts.instruction_ratchet_gate
.\.venv\Scripts\python.exe -m scripts.doc_refs_ratchet_gate
.\.venv\Scripts\python.exe -m scripts.dead_code_ratchet_gate
.\.venv\Scripts\python.exe -m scripts.conventions_ratchet_gate
```

Entirely offline against sanitized fixtures: no WhatsApp credentials, no network, no Telegram. The one exception is the **static ratchet** (`scripts/static_ratchet.py`, vendored; `.fleet.toml [static_ratchet]`, baseline `.static-ratchet-baseline.json`): it fails on a duplicate block (jscpd via `npx`, so **Node is a prerequisite**), an unused import/variable (ruff `F401,F841`) or a likely bug or security pattern (ruff's curated `B` and `S` families, `S113` included) not in the baseline. It reads the whole tree, never path-routed.

Every ratchet is a vendored `scripts/<name>_ratchet.py` with a `.fleet.toml [<name>_ratchet]` table and a `.<name>-ratchet-baseline.json`; `verify-before-ship.ps1` calls it only through `scripts.<name>_ratchet_gate`, which fails exit 3 (a detector could not run) as NOT CONFIRMED, never a pass or skip. Shrink a baseline with `--update-baseline` in the PR that removes the debt; adding needs `--allow-additions` and shows in review; reword or fix the flagged line rather than raising a baseline. Re-vendor a script only from project-scaffolding. Per ratchet:

- **docs** — `README.md` size and history markers (issue references, dated or changelog headings) in `README.md` and `docs/**/*.md`; docs you touch state the present-tense why with no `#N` citations.
- **comment** — counts `#NNN` issue references inside code comments per file and fails when one rises; a comment you touch states the present-tense why with no issue number or history narrative.
- **structure** — `.fleet.toml` `shape = "webapp-tray"` + `extra_roots` + `[structure_ratchet]`; fails on a file over 1,000 lines, a function over 100, a directory over 40 modules, a `src`→`app` import, or a root folder the shape and `extra_roots` do not list, with baselined offenders held at today's size. New code goes where `docs/layout.md` says — Google API code in the portable root packages, not `src/` — so split rather than grow.
- **instruction** — fails when `CLAUDE.md` or `AGENTS.md` gains a prompt-audit lint hit or passes its size cap.
- **doc-refs** — fails when `README.md`, `CLAUDE.md` or `docs/**/*.md` names a repo path, `python -m` module or relative link that does not exist; point it at what exists, or list a deliberate pointer to another repo's file in `allow_refs`.
- **dead-code** — fails when a tracked Python function, class or method that nothing in the repo refers to is not in the baseline; delete the definition or call it; a framework hook it misses goes in `allow` or `entry_decorators`, and every vendored copy must be declared in `[vendored]` first.
- **conventions** — `rules = ["no-window", "logging-not-print", "no-drive-path"]`; fails when a file has more hits of an enabled rule than the baseline records: a subprocess spawn without `creationflags`, a `print` in non-CLI code under `app/` or `src/`, or a string literal starting with a drive letter. Fix the line, or mark a deliberate exception `# <rule>-exempt: <reason>`; `secret-compare` and `git-wrapper` stay off until the repo has the helper they enforce.

## CI expectations

- Workflow `.github/workflows/e2e.yml`, job `verify-before-ship`, on every PR. **Advisory, not required** (no branch protection) — the local gate (`pytest` / `ruff` / `mypy`) is the contract.
- Typical green: **~2 min**. Investigate at **>5 min**; treat as wedged at **>8 min**.
- Flaky leg: the Playwright **WebKit/iPhone** e2e projection can wedge the browser on the hosted runner; `timeout-minutes: 30` caps it. A wedge is a flake, not the diff.
- CI's only signal beyond the local gate is the **e2e suite** (skipped locally — `pytest` shows ~13 skipped). e2e surface = `app/webapp/`, `app/tray/`, `tests/e2e/`, static assets under `app/webapp/static/`; a diff touching **none** of these (e.g. `src/db/`, `src/analysis/`, `src/notify/`, docs) gains nothing from CI.
