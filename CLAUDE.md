# Project Instructions

Canonical instructions for AI coding agents working in this repository. `AGENTS.md` points here for non-Claude tools.

## This Repository

WhatsApp Radar is a local-first personal utility for classifying new WhatsApp chat messages and surfacing only actionable items through a separate notification channel. Treat it as a sensitive-data project even though the repository is public.

It follows the fleet's standard layout (as in `E:\automation\app-launcher`): UI surfaces in `app/`, logic in `src/`, committed config in `config/`, reference docs in `docs/`, the read-only Node/Baileys connector in `sidecar/`. It is **not** an installable package — it runs from a checkout.

```
whatsapp-radar/
  app/cli/main.py        # argparse CLI (status|ingest|chats|monitor|ignore|review|notify)
  src/                   # logic, imported as `from src.…`
    config.py  models.py
    connector/ (base, fixture, linked_device)
    db/ (store.py, schema.sql)
    analysis/ (classifier, contract, keywords, review, prompts/)
    notify/ (base, factory, telegram)   report/digest.py
    fixtures/sample_chats.json
  config/                # committed defaults (default.json); local.json + .env are gitignored
  sidecar/               # read-only Node/Baileys connector
  docs/  tests/
  launcher.py  wr.bat    # entry points
  requirements.txt  requirements-dev.txt  pytest.ini
  pyproject.toml         # tool config only (ruff/mypy) — no packaging
```

Run it with `python launcher.py <command>`, `python -m app.cli.main <command>`, or the `wr.bat <command>` wrapper. (The FastAPI admin PWA + tray surfaces under `app/webapp/` and `app/tray/` arrive in later steps.)

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

- Reuse `E:\automation\local-llm-hub` for LLM calls. Do not implement direct `claude -p`, `agy`, or provider-specific subprocess wrappers in this repo.
- Use App Launcher for scheduling and launch surfaces where appropriate: Jobs for periodic digest runs, Apps for a small admin UI.
- The admin UI (later steps) is **FastAPI + vanilla JS** mirroring App Launcher — not Streamlit. Its secrets (bearer token, Telegram token/chat id, passkey state) will live in the gitignored `config/webapp_config.json`; until that step lands, runtime secrets are read from the gitignored `.env` / `config/local.json`.
- If a reusable convention emerges, route the general rule back to `E:\automation\project-scaffolding` instead of creating fleet drift here.

## Implementation Conventions

- Prefer a small, explicit architecture over framework ceremony.
- Keep connector, storage, analysis, notification, and UI boundaries separate.
- Store durable state in SQLite unless a later issue justifies something heavier.
- Public functions should have type hints.
- Use structured JSON outputs for LLM classification and validate them before advancing cursors.
- Advance a per-chat cursor only after analysis state is persisted.
- Notification delivery should be retryable independently of message analysis.

## Verification

Run the gate from the repo root with the project venv:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src app
```

The suite runs entirely offline against sanitized fixtures (no WhatsApp credentials, no network, no Telegram). Do not claim tests pass without running them.

## Planning Discipline

Future work belongs in GitHub issues, not dated planning files. Durable reference material may live under `docs/` when it will still be useful next quarter.
