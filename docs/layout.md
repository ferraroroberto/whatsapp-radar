# Layout

Where code goes. The shape is `webapp-tray`, declared in `.fleet.toml` together with the root folders it adds (`extra_roots`); the structure ratchet (`scripts/structure_ratchet.py`, run by the gate through `scripts/structure_ratchet_gate.py`) checks that the root folders match, that no new file passes 1,000 lines, no new function 100 lines and no directory 40 modules, and that nothing under `src/` imports `app/`. Offenders that predate the ratchet sit in `.structure-ratchet-baseline.json` at their size and may not grow; split them rather than raise the baseline.

## Top-level folders

| Folder | Holds |
|---|---|
| `app/` | UI surfaces: the argparse CLI, the FastAPI admin PWA, the tray. Imports `src/`, never the reverse. |
| `src/` | The logic: connector, store, classifier, notifiers, family checks. Imported as `from src.…`. |
| `tests/` | The offline pytest suite (fixture connector only) and the Playwright e2e suite in `tests/e2e/`. `tests/` is a baselined flat directory: a new test module goes in a subfolder (`tests/gates/` holds the ratchet gate tests), not in `tests/` itself. |
| `docs/` | Durable reference, this file included. |
| `scripts/` | Helper CLIs, the verification gate, and the vendored ratchets with their gate wrappers. |
| `config/` | Committed defaults (`default.json`) and `*.sample` templates. Machine-local overrides next to them are gitignored. |
| `assets/` | Tray icon, Stream Deck icon and the Telegram bot avatars. |

Declared exceptions (`extra_roots`), each outside `src/` on purpose:

| Folder | Why it stays a root |
|---|---|
| `calendar_readonly/`, `calendar_write/`, `gmail_readonly/` | Portable Google API clients shared with sibling repos, liftable unchanged. They import nothing from `src/`, `app/` or `scripts/`. |
| `google_oauth_common/` | The installed-app OAuth bootstrap, token store and transport the three clients wrap. Lifted alongside any of them. |
| `sidecar/` | The read-only Node/Baileys connector. A separate process with its own `package.json`, not Python. |

**Google API code goes in those packages, not in `src/`.** `src/` consumes them through their public functions (`from calendar_readonly import …`); a new Google call extends the matching client, and a new Google surface gets its own sibling package that follows the same no-`src`/`app` contract (`docs/gmail-reuse.md`).

## `app/`

| Path | Holds |
|---|---|
| `app/cli/` | `main.py`, the argparse dispatcher behind `launcher.py` and `wr.bat`. |
| `app/tray/` | The pystray surface that owns the webapp lifecycle. |
| `app/webapp/` | FastAPI server (`server.py`), adopt-or-spawn uvicorn lifecycle (`manager.py`), auth middleware, run tracking. |
| `app/webapp/routers/` | One API router per area (chats, config, family, audit, auth, …). |
| `app/webapp/static/` | The vanilla-JS PWA shell, its CSS, and vendored components under `_vendored/`. |

## `src/`

| Path | Holds |
|---|---|
| `src/config/` | `load_config` aggregates one module per subsystem. |
| `src/connector/` | The connector boundary: base contract, factory, fixture, linked-device, sidecar and Gmail sources, preflight. |
| `src/db/` | SQLite: `store.py` facade over per-table modules, sync, reprocess, retention, `schema.sql`. |
| `src/analysis/` | Classifier, pipeline, tripwire, transcription, summaries, prompts. |
| `src/notify/` | Notification channels and delivery, retryable independently of analysis. |
| `src/report/` | The digest. |
| `src/family/` | Calendar scan, rules, traffic check and travel blocks. `travel_blocks_write.py` is the only path that writes to or deletes from calendars. |
| `src/presence/`, `src/traffic/`, `src/task_os/` | Clients for the presence, traffic and Task OS integrations. |
| `src/fixtures/` | Sanitized sample chats. |
| `src/*.py` | Shared leaf modules: models, paths, logging, runtime data, vendored `runtime_data.py` and `usage_log.py`. |

Prefer a domain subpackage over a new module at `src/` itself; the module count of a directory is what the ratchet watches.
