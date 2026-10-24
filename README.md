# WhatsApp Radar

WhatsApp Radar cuts attention load from high-volume WhatsApp chats. It monitors selected chats, processes only the messages that arrived since the last review, classifies whether anything actionable is in them, and sends one consolidated report to a separate notification channel. Gmail and the household Google Calendars are optional second and third sources.

This repository is intentionally public-safe. It must not contain real WhatsApp credentials, linked-device auth state, message exports, chat names, phone numbers, school names, screenshots, or notification tokens.

## What it does

- Connects to WhatsApp as a linked device, read-only. Gmail is an optional second source over the official read-only OAuth API.
- Discovers chats and senders and stores sanitized metadata locally; either can be marked monitored.
- Maintains a per-chat / per-sender cursor so each review processes only new messages.
- Classifies each delta into actionable vs. noise with a keyword prefilter gating an LLM stage.
- Emits one consolidated digest, only when action is required, delivered outside WhatsApp through Telegram.
- Runs two deterministic, LLM-free family checks — calendar-conflict detection and traffic-jam alerting — plus an opt-in travel-blocks calendar-write feature, through the same run store, notifier, and admin UI.

## Non-Goals

- No sending messages through WhatsApp.
- No auto-replies, bots, or group moderation.
- No cloud-hosted multi-user service.
- No committed personal data or credentials.
- No model training on WhatsApp data.

## Architecture

WhatsApp Radar is a standalone local service, integrated with the home-automation fleet:

- A WhatsApp linked-device connector (read-only Node/Baileys sidecar + Python reader) owns pairing, chat discovery, message ingestion, and reconnect handling. An optional Gmail OAuth client and the `calendar_readonly` / `calendar_write` clients own the other two Google sources.
- A local SQLite store owns chat/sender metadata, messages, review cursors, analysis results, run traces, and notification history.
- A processing pipeline analyzes only message/mail deltas and calls the local LLM Hub rather than duplicating model/subprocess orchestration.
- The admin PWA (four tabs — Home, Activity, Messages, Family) with a header gear opening Settings (display, sources, how messages are judged, alerts, maintenance, this install), handles connection status, discovered chats/senders, monitor/ignore decisions, classifier configuration, and the family-check rules.
- App Launcher schedules the three jobs (`family-radar-scan`, `family-radar-calendar-sync`, `family-radar-traffic-check`) through its Jobs tab and opens the admin UI through its Apps tab.

The internal module map is [`docs/architecture.mmd`](docs/architecture.mmd); where new code goes is [`docs/layout.md`](docs/layout.md).

## Compliance And Risk

The connector path for personal and group chats is a WhatsApp Web linked-device integration. That is technically feasible but not an official WhatsApp Business Platform use case, so the implementation stays conservative: read-only behavior in our code, no send surface, no bulk automation, no scraping beyond chats the account can already see, local-only storage, and explicit operator consent. The unofficial-library risk (Baileys) is documented in [`docs/linked-device.md`](docs/linked-device.md).

## Documentation

| Document | Covers |
| --- | --- |
| [`docs/bootstrapping.md`](docs/bootstrapping.md) | standing the whole system up from zero — linked device, Telegram bot, phone access, App Launcher wiring |
| [`docs/manual.md`](docs/manual.md) | day-to-day operation, CLI surface, troubleshooting |
| [`docs/linked-device.md`](docs/linked-device.md) | the WhatsApp connector's design, buffer contract, and unofficial-library risk |
| [`docs/gmail-bootstrap.md`](docs/gmail-bootstrap.md) | provisioning the read-only Gmail source |
| [`docs/gmail-reuse.md`](docs/gmail-reuse.md) | lifting the portable Gmail client into another repo |
| [`docs/calendar-bootstrap.md`](docs/calendar-bootstrap.md) | Calendar OAuth (read + write) and the Google Routes API key |
| [`docs/scan-and-classification.md`](docs/scan-and-classification.md) | `scan`, multi-source sync, preflight, the settled-buffer gate, the classifier and alert memory |
| [`docs/gmail-source.md`](docs/gmail-source.md) | Gmail source behaviour — whitelist, sender monitoring, child registry, calendar reminders |
| [`docs/voice-and-summaries.md`](docs/voice-and-summaries.md) | voice-note transcription, playback, on-demand summaries and read-aloud |
| [`docs/webapp.md`](docs/webapp.md) | the admin PWA tab by tab, its endpoints, running it, logs, usage log, the Tailscale HTTPS cert |
| [`docs/family-checks.md`](docs/family-checks.md) | the family checks and travel blocks in full — behaviour, every knob, and the rollout runbook |
| [`docs/verification.md`](docs/verification.md) | e2e routing, the static ratchet, design capture, CI |
| [`docs/architecture.mmd`](docs/architecture.mmd) | internal module map |

## Running offline (no personal data)

The app runs end-to-end with a deterministic sanitized fixture connector and a deterministic stub classifier, so it needs **no WhatsApp credentials and no network**. Session/auth state lives under the ignored `data/` and `auth/` paths; the message database itself resolves to the fleet runtime-data root (`C:\sqlite\whatsapp-radar\` by default on this machine, overridable via `WR_DB_PATH`, `WHATSAPP_RADAR_DATA_DIR`, or `FLEET_DATA_ROOT` — see `src/runtime_data.py`), which also holds the `reprocess` backups and sits outside this git checkout, so it needs its own backup source rather than git-derived backup.

It runs from a checkout with no install step — `wr.bat <cmd>` is the ergonomic wrapper for `python launcher.py <cmd>`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt

# Ingest sanitized fixture chats/messages into local SQLite, then pick chats to monitor.
.\wr.bat ingest
.\wr.bat chats
.\wr.bat monitor 1
.\wr.bat monitor 3

# First review classifies the delta and prints one consolidated digest.
.\wr.bat review --dry-run
# Second review with no new messages does nothing and produces no notification.
.\wr.bat review --dry-run
```

Adding new messages causes only the delta to be reviewed. The per-chat cursor advances only after analysis is persisted, so a classifier error safely reprocesses the same delta next run.


### One-shot `scan` (the scheduled job)

`scan` collapses the monitored-message flow — sync → keyword prefilter (Stage 1) → LLM (Stage 2) → digest → deliver — into one run with a full per-run audit trace, and advances each cursor only after that chat's analysis is persisted. `scan --dry-run` replays stored messages with no connector, no delivery and no cursor advance.

```powershell
.\wr.bat scan                 # live: sync all chats, analyze monitored deltas, deliver one digest
.\wr.bat scan --dry-run       # replay stored messages, touch nothing
```

Sources are chosen with `sources` in the ignored `config/local.json` or `WR_SOURCES=whatsapp,gmail`; the classifier with `WR_CLASSIFIER` (`stub` offline default, `hub`, or `cascade` — keyword prefilter in front of the LLM, recommended for real use). A run that cannot check its source or reach the classifier fails loudly and holds every cursor. Preflight, the settled-buffer gate, prompt assets and alert memory: [`docs/scan-and-classification.md`](docs/scan-and-classification.md).

## Running Against Real WhatsApp + Telegram

The fixture path above needs no credentials. To run against real chats and deliver digests to Telegram, follow the from-zero runbook in [`docs/bootstrapping.md`](docs/bootstrapping.md), then [`docs/manual.md`](docs/manual.md) for day-to-day operation. In short:

1. Pair a WhatsApp **linked device** with the read-only Node sidecar (`cd sidecar && npm install && npm start`, then scan the QR). It writes a local buffer under the ignored `data/linked_device/`.
2. Set `WR_CONNECTOR=linked_device` and leave `WR_SOURCES=whatsapp`. `wr ingest` / `chats` / `monitor` / `review` / `scan` / `resync` / `reprocess --confirm` then run unchanged against real data. `scan`, `resync`, and `reprocess` are also launchable as plain processes from App Launcher's Jobs tab, and appear live in the webapp's Activity tab.
3. For delivery, create a Telegram bot, set `WR_NOTIFIER=telegram` plus `WR_TELEGRAM_BOT_TOKEN` / `WR_TELEGRAM_CHAT_ID`, and `wr review` delivers one consolidated digest. `wr notify` re-delivers a run if a send failed.

The connection is **read-only by construction** — no send, react, or read-receipt surface exists. The buffer contract, the message-normalization set, and the connector design answers are in [`docs/linked-device.md`](docs/linked-device.md). Credentials and session state live only under the ignored `auth/`; Telegram secrets live in the gitignored `config/webapp_config.json`, or the ignored `.env` via `WR_TELEGRAM_*`. The Telegram bot and chat avatars (PNG for upload, editable SVG source) live in `assets/telegram/`.

### Optional sources and features

- **Gmail** — read-only OAuth over named senders and labels, with sender-level discovery and a household child registry. [`docs/gmail-source.md`](docs/gmail-source.md); provisioning in [`docs/gmail-bootstrap.md`](docs/gmail-bootstrap.md).
- **Voice-note transcription** — off by default; routes through the hub's Whisper endpoint and needs `ffmpeg` on PATH. [`docs/voice-and-summaries.md`](docs/voice-and-summaries.md).
- **On-demand summaries and read-aloud** — a persisted hub summary per long message, spoken through four server-resolved voice profiles. [`docs/voice-and-summaries.md`](docs/voice-and-summaries.md#on-demand-summaries-and-read-aloud).

## Admin Webapp (phone-first PWA)

A FastAPI + vanilla-JS PWA on port **8455** with four tabs — Home, Activity, Inbox (messages), Family — and a Settings dialog behind the header gear. Auth is a bearer token (loopback bypasses it), an optional login password and WebAuthn passkeys, over a Tailscale-issued HTTPS cert. Tab-by-tab behaviour, every endpoint, logs and the usage log: [`docs/webapp.md`](docs/webapp.md).

```powershell
.\setup.bat                 # one-shot: .venv + deps + PWA icons
.\webapp.bat                # run the webapp standalone (HTTPS when a cert is present)
.\tray.bat                  # adopt-or-spawn the webapp behind a tray icon (daily use)
.\tray.bat --restart        # stop the running tray + reclaim :8455, start fresh
```

| Command | Effect |
| --- | --- |
| `tray.bat` | no-op if a WhatsApp Radar tray is already running |
| `tray.bat --restart` | kills only this repo's tray + reclaims `:8455` by PID (scoped to this `.venv` — never a blanket `pythonw` kill), then relaunches |
| `webapp.bat` | standalone server, no tray (headless / dev iteration) |

Secrets — bearer token, login password, passkey state, and the Telegram token/chat id — live in the gitignored `config/webapp_config.json` (`config/webapp_config.sample.json` is the template); `WR_TELEGRAM_*` env still overrides it. Confirm the live build with `GET /api/version` → `{git_sha, built_at, asset_hash}`. HTTPS: `scripts\gen_tailscale_cert.py` provisions the cert ([`docs/webapp.md`](docs/webapp.md#https-certificate-tailscale)).

## Family checks (calendar conflicts, traffic, travel blocks)

Two deterministic scheduled checks plus one opt-in calendar-write feature, living alongside the WhatsApp/Gmail pipeline and reusing this app's run store, notifier, config, and admin UI — but not its message-analysis core. **No LLM runs in any of these loops**; detection is plain Python (`src/family/`, `src/traffic/`), unit-tested offline.

| Feature | Verb | Default | What it does |
| --- | --- | --- | --- |
| **Traffic-jam insurance** | `wr traffic-check` | off | Finds each household member's next commute, resolves the origin (live phone position when available, otherwise calendar inference), prices the drive with the Google Routes API, and alerts on Telegram on a significant delay, a departure moment reached, or a back-to-back hop that cannot be made. Deduped, quiet-hours aware, and skips train commutes for the two driving-ETA judgments. |
| **Calendar sync** | `wr calendar-scan` | off | Scans the next few days of both household calendars, flags coverage gaps against the weekly responsibility pattern and same-person overlaps, and sends exactly one Telegram summary per live run — findings, or an explicit all-clear. |
| **Commute travel blocks** | rides inside `wr calendar-scan` | off **and** dry | Computes the commute blocks each person's calendar should carry, reconciles against what is already there, and writes only the difference. Ships off and dry-run; turning it on is a deliberate operator sequence. |

Both checks are independently toggleable — from the **Family** tab (its *Calendar sync* and *Traffic checks* switches), or `WR_FAMILY_ENABLED` / `WR_TRAFFIC_ENABLED`. Everything household-identifying — home address, calendar ids, the responsibility pattern, childcare windows, the Routes API key — lives only in the gitignored `config/local.json`; `config/default.json` is the committed schema. Provisioning (Calendar OAuth + the Routes key) is in [`docs/calendar-bootstrap.md`](docs/calendar-bootstrap.md).

Optionally, the traffic check reads the responsible parent's **live phone position** from [home-automation](../home-automation)'s read-only presence API instead of inferring an origin from the calendar. It is disabled by default and the checks work with no home-automation running. Raw coordinates never reach the DB, run traces, or logs.

**[`docs/family-checks.md`](docs/family-checks.md) is the full reference** — every alert and how it is anchored in time, the priced/unpriced coverage split, all three config tables, travel-block planning and deletion safety, the presence integration, and the step-by-step rollout runbook for turning travel blocks on (and undoing it).

## Home-stack wiring (App Launcher)

WhatsApp Radar runs as part of the home stack through [App Launcher](../app-launcher)'s **Jobs** tab, as three independent jobs plus the admin PWA opened from the **Apps** tab:

| Job | Verb | Schedule |
| --- | --- | --- |
| `family-radar-scan` | `wr scan` | daily at 18:00 — the WhatsApp/Gmail digest |
| `family-radar-calendar-sync` | `wr calendar-scan` | daily at 18:05 — the family calendar summary plus the travel-block sweep that rides inside it |
| `family-radar-traffic-check` | `wr traffic-check` | armed every 5 minutes; the CLI self-skips in-process against `traffic.cadence_min` (default 30), so the *effective* frequency follows the config, not the Task Scheduler entry — no re-arm after a cadence edit |

The calendar sync is deliberately its own job rather than chained after the scan: a dead WhatsApp sidecar must never suppress the family calendar summary. The reasoning behind the 18:05 slot, and what an evening sweep costs in forecast accuracy, is in [`docs/family-checks.md`](docs/family-checks.md#scheduling).

That wiring lives in App Launcher's gitignored runtime registries (`config/jobs.json`, `config/apps.json`) — machine-local state, not committed here — so it is recreated per box from App Launcher's UI, or its `POST`/`DELETE /api/jobs` API, which is what re-syncs the underlying Task Scheduler entries. Hand-editing `jobs.json` directly does not. The full procedure (the three Jobs rows and the two Apps rows) is **Step 7** in [`docs/bootstrapping.md`](docs/bootstrapping.md).

**Credential lifecycles.** `docs/bootstrapping.md`'s **Recurring maintenance** table covers the app-level secrets (bearer token / login password, Telegram bot token, sidecar re-pairing) on an on-leak / on-compromise cadence, not a calendar one. None of the three Google OAuth grants (Gmail read, Calendar read, Calendar write) rotate on a calendar schedule either — each keeps working until revoked, left unused for six months, or the account password changes while its scope is present (see [`docs/gmail-bootstrap.md`](docs/gmail-bootstrap.md#token-lifecycle-and-revocation) and [`docs/calendar-bootstrap.md`](docs/calendar-bootstrap.md)). The Tailscale HTTPS leaf is the one credential here that *is* calendar-anchored, and it renews itself automatically — see [HTTPS certificate (Tailscale)](docs/webapp.md#https-certificate-tailscale).

## Verification

Run the gate from the repo root with the project venv:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src app
.\.venv\Scripts\python.exe -m scripts.static_ratchet_gate   # no new duplicate block or unused import/variable
.\.venv\Scripts\python.exe -m scripts.docs_ratchet_gate     # README under 40 KiB, no new history marker in README or docs
.\.venv\Scripts\python.exe -m scripts.comment_ratchet_gate  # no new issue reference in a code comment
.\.venv\Scripts\python.exe -m scripts.structure_ratchet_gate # no new oversize file/function/directory, src never imports app, root folders match the shape
.\.venv\Scripts\python.exe -m scripts.instruction_ratchet_gate # no new prompt-audit lint hit or size-cap breach in CLAUDE.md / AGENTS.md
powershell -File scripts\verify-before-ship.ps1   # all of the above + the e2e slice the diff needs (Chromium + WebKit/iPhone)
powershell -File scripts\verify-before-ship.ps1 -RouteOnly   # print the e2e routing decision, run nothing
```


Detail on e2e routing (a docs-only or backend-only diff skips the browser suite), the static, docs, comment, structure and instruction ratchets, design capture and CI: [`docs/verification.md`](docs/verification.md).

## Repository Status

The app runs day to day against real WhatsApp + Telegram, with Gmail, the two family checks, and travel blocks available as opt-in sources and features — all built on the same foundation used for offline development: a read-only connector, a SQLite store, a cursor/delta review engine, a validated LLM JSON contract, and a consolidated digest. The linked-device connector baselines each chat to now on first monitor, a multilingual (ES/EN/CA) cascade classifier gates LLM calls behind a keyword prefilter, and Telegram delivery is retryable independently of analysis. The fixture connector and offline stub classifier remain the default, so the whole suite still runs with no credentials.
