# Project Instructions

`AGENTS.md` points here, so every coding agent reads this same instruction set.

## This Repository

WhatsApp Radar classifies new WhatsApp chat messages and surfaces only actionable items through a separate notification channel. **Treat it as a sensitive-data project even though the repository is public.**

Fleet standard layout (as in `E:\automation\app-launcher`): UI in `app/`, logic in `src/`, committed config in `config/`, docs in `docs/`, the read-only Node/Baileys connector in `src/sidecar/`. Runs from a checkout, never installed.

Folder by folder, and where new code goes: [`docs/layout.md`](docs/layout.md); the internal structure as a diagram: [`docs/architecture.mmd`](docs/architecture.mmd). In short: the argparse CLI is `app/cli/main.py`, the FastAPI admin PWA `app/webapp/`, the pystray tray `app/tray/`; the connector, store, classifier and notifiers live in `src/`; the Node/Baileys connector is `src/sidecar/` (own `package.json`, not Python). `src/family/travel_blocks_write.py` is the only path that deletes from calendars; `src/analysis/reminders.py` is the insert-only writer.

CLI entry points: `python launcher.py <command>`, `python -m app.cli.main <command>`, or `scripts\wr.bat <command>`.

Update `docs/architecture.mmd` in the same PR as any material structural change (connector added, pipeline stage moved, router split); it is hand-authored, so `scripts/verify-before-ship.ps1` does not check it.

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
- Bundled assets (`db/schema.sql`, `analysis/prompts/*`, `tests/fixtures/*.json`) resolve by path relative to `__file__`, never via `importlib.resources` package-data.
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
.\.venv\Scripts\python.exe -m scripts.runtime_write_ratchet_gate
.\.venv\Scripts\python.exe -m scripts.usage_track_ratchet_gate
```

The gate runs offline on sanitized fixtures. The static ratchet is the one exception: it needs Node, because the duplicate-block detector runs through `npx`.

Every ratchet is a vendored `scripts/<name>_ratchet.py` with a `.fleet.toml [<name>_ratchet]` table and a `.<name>-ratchet-baseline.json`, called by `verify-before-ship.ps1` only through its `scripts.<name>_ratchet_gate`. A gate that exits 3 means a detector could not run: NOT CONFIRMED, never a pass. Baselines stay at zero: reword or fix the flagged line, never raise a baseline to get past it. Re-vendor a script only from project-scaffolding. Detail per ratchet: [`docs/verification.md`](docs/verification.md). The ones that bite while editing:

- **runtime write** (`code_folders = ["src", "app", "scripts"]`) — runtime output goes under `data/` or the runtime root (`src/runtime_data.py`). A caller-named path goes through `src/runtime_files.py`, which raises when the path lands in a code folder. A `# runtime-write-exempt: <reason>` marker is for a path fixed from outside, with that reason stated.
- **usage track** — a new web control takes a constant `data-track` id and a `<dialog>` a `data-track-view` in the same change; a deliberate exception carries `<!-- untracked-control-exempt: <reason> -->` (`//` in JS).
- **comment, docs** — comments, docstrings and docs state the present-tense why: no `#N` issue references, no history phrases.
- **structure** — new code goes where `docs/layout.md` says (Google API code in the portable root packages, not `src/`); split rather than grow.
- **conventions** — `no-window`, `logging-not-print`, `no-drive-path`, `subprocess-timeout`, and `must-use` for `requests`, `sqlite3.connect` and `os.replace` (their helpers are named in `.fleet.toml`). Mark a deliberate exception `# <rule>-exempt: <reason>`.

## CI expectations

- Workflow `.github/workflows/e2e.yml`, job `verify-before-ship`, on every PR. **Advisory, not required** (no branch protection) — the local gate (`pytest` / `ruff` / `mypy`) is the contract.
- Typical green: **~2 min**. Investigate at **>5 min**; treat as wedged at **>8 min**.
- Flaky leg: the Playwright **WebKit/iPhone** e2e projection can wedge the browser on the hosted runner; `timeout-minutes: 30` caps it. A wedge is a flake, not the diff.
- CI's only signal beyond the local gate is the **e2e suite** (skipped locally — `pytest` shows ~13 skipped). e2e surface = `app/webapp/`, `app/tray/`, `tests/e2e/`, static assets under `app/webapp/static/`; a diff touching **none** of these (e.g. `src/db/`, `src/analysis/`, `src/notify/`, docs) gains nothing from CI.
