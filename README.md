# WhatsApp Radar

WhatsApp Radar is a local-first spike for reducing attention load from high-volume WhatsApp chats. The intended workflow is to monitor selected chats, process only new messages since the previous review, classify whether any actionable information exists, and send one consolidated report to a separate notification channel.

This repository is intentionally public-safe. It must not contain real WhatsApp credentials, linked-device auth state, message exports, chat names, phone numbers, school names, screenshots, or notification tokens.

## Goal

The first milestone is a reusable spike, not a polished product:

- Connect to WhatsApp as a linked device in read-only application behavior.
- Discover chats and store sanitized metadata locally.
- Allow selected chats to be marked as monitored.
- Maintain per-chat cursors so each review processes only new messages.
- Classify deltas into actionable vs. noise.
- Emit one consolidated report only when action is required.
- Deliver reports outside WhatsApp, initially through Telegram.

## Non-Goals

- No sending messages through WhatsApp.
- No auto-replies, bots, or group moderation.
- No cloud-hosted multi-user service.
- No committed personal data or credentials.
- No model training on WhatsApp data.

## Architecture Direction

The expected shape is a small standalone local service, integrated with the existing home automation fleet:

- A WhatsApp linked-device connector owns pairing, chat discovery, message ingestion, and reconnect handling.
- A local SQLite store owns chat metadata, messages, review cursors, analysis results, and notification history.
- A processing worker analyzes only message deltas and calls the existing local LLM Hub instead of duplicating model/subprocess orchestration.
- A small admin UI handles connection status, discovered chats, monitor/ignore decisions, frequency, retention, and notification settings.
- App Launcher should schedule the digest run through its Jobs tab and open the admin UI through its Apps tab.

## Compliance And Risk

The practical connector path for personal/group chats is likely a WhatsApp Web linked-device integration. That is technically feasible but not an official WhatsApp Business Platform use case. Any implementation must be conservative: read-only behavior in our code, no send surface, no bulk automation, no scraping beyond chats the account can already see, clear local-only storage, and explicit operator consent.

Before building product features, complete the spike in [`docs/onboarding.md`](docs/onboarding.md).

## Running The Spike (No Personal Data)

The spike runs end-to-end with a deterministic sanitized fixture connector and a deterministic stub classifier, so it needs **no WhatsApp credentials and no network**. All runtime state lives under the ignored `data/` path.

The project runs from a checkout (no install step) — `wr.bat <cmd>` is the ergonomic wrapper for `python launcher.py <cmd>`.

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

Adding new messages causes only the delta to be reviewed; the per-chat cursor advances only after analysis is persisted, so a classifier error safely reprocesses the same delta next run.

### One-shot `scan` (the scheduled job)

`scan` is the single callable that collapses the whole flow — sync → keyword prefilter (Stage 1) → LLM (Stage 2) → digest → deliver — into one run, and it persists a **full per-run audit trace** so every decision is inspectable (what synced, what passed the keyword stage, the exact LLM prompt and raw response, the verdict, and what was delivered). This is what App Launcher's Jobs tab fires.

```powershell
.\wr.bat scan                 # live: sync all chats, analyze monitored deltas, deliver one digest
.\wr.bat scan --dry-run       # replay stored messages with no connector, no delivery, no cursor advance
.\wr.bat scan --dry-run --days 7   # dry-run windowed to the last 7 days
```

Live `scan` advances each cursor only after that chat's analysis and trace are persisted (same retry-safe guarantee as `review`). `--dry-run` replays history straight from SQLite — it never touches the connector, never delivers, and never advances a cursor — so it's the safe way to see what a run *would* do. Funnel counters land on `review_runs`; the per-chat decision record lands on `analysis_trace`.

A live `scan` / `resync` **preflights the source** before reading it: if the WhatsApp sidecar's heartbeat is stale (the process stopped) it first tries to relaunch the sidecar — when the device is still paired — and re-checks (set `WR_SIDECAR_AUTOSTART=0` to disable the self-heal). If the source still isn't live it **aborts loudly**: exits non-zero, records the run as failed, advances no cursor, and fires an alert to the notification channel. This closes the silent-failure hole where a scheduled job against a dead sidecar would report green while checking nothing.

Classification defaults to the offline stub. Set `WR_CLASSIFIER=hub` to route through [local-llm-hub](../local-llm-hub) (the `claude_sonnet` model on `127.0.0.1:8000`), or `WR_CLASSIFIER=cascade` (recommended for real use) to run a cheap multilingual keyword prefilter first that gates the LLM call — so "utter noise" deltas never reach the model. Use a model that answers with JSON directly (the default `claude_sonnet` does); a reasoning model that emits a long `<think>` trace can overrun the token budget and return nothing parseable — that case is recorded as a distinct `llm_truncated` trace state (not a generic contract error), the output budget is configurable (`WR_HUB_MAX_TOKENS`), and the per-prompt delta is capped (`WR_HUB_MAX_PROMPT_CHARS`) so a whole-history scan can't blow the model's context window. Both prompt assets are inspectable plain-text files you can tune without touching code: the system prompt at `src/analysis/prompts/classification_system.md` and the cascade's actionable roots (Spanish/English/Catalan) at `src/analysis/prompts/keyword_roots.txt`.

## Running Against Real WhatsApp + Telegram

The fixture path above needs no credentials. To run against real chats and deliver digests to Telegram, follow the full step-by-step guide in [`docs/manual.md`](docs/manual.md). In short:

1. Pair a WhatsApp **linked device** with the read-only Node sidecar (`cd sidecar && npm install && npm start`, then scan the QR). It writes a local buffer under the ignored `data/linked_device/`.
2. Set `WR_CONNECTOR=linked_device`; `wr ingest` / `wr chats` / `wr monitor` / `wr review` / `wr scan` / `wr resync` / `wr reprocess --confirm` then run unchanged against real data. `wr scan`, `wr resync`, and `wr reprocess` are also launchable as plain processes from App Launcher's Jobs tab (and surfaced live in the webapp's Execution tab).
3. For delivery, create a Telegram bot, set `WR_NOTIFIER=telegram` plus `WR_TELEGRAM_BOT_TOKEN` / `WR_TELEGRAM_CHAT_ID`, and `wr review` delivers one consolidated digest. `wr notify` re-delivers a run if a send failed.

The connection is **read-only by construction** — no send/react/read-receipt surface exists. The unofficial-library risk (Baileys), the buffer contract, the message-normalization set, and answers to the spike questions are documented in [`docs/linked-device.md`](docs/linked-device.md). Credentials/session live only under ignored `auth/`; Telegram secrets live in the gitignored `config/webapp_config.json` (or the ignored `.env` via `WR_TELEGRAM_*`).

## Admin Webapp (phone-first PWA)

A FastAPI + vanilla-JS admin PWA runs on port **8455**, mirroring App Launcher's auth/tunnel model: a bearer token (loopback bypasses it), an optional login password, WebAuthn passkeys (enrolled from the tray, ceremonies Tailscale-only), Tailscale TLS, and dormant Cloudflare named-tunnel scaffolding. Of the four tabs — Dashboard · Chats & Config · Execution · Audit — three are live. The **Dashboard** shows read-only metrics (channels monitored, messages stored, scans run, backlog since the last scan, alerts raised, notifications sent, plus a per-monitored-channel table) served by `GET /api/dashboard`. **Chats & Config** lets you pick which chats are watched (a searchable Monitored/Ignored/All list ordered by last activity, a single watch toggle per row, and a tap-to-open conversation overlay that pages older messages in as you scroll up; marking a chat monitored baselines its review cursor to only new messages). From the overlay you can also ✏️ **rename** a chat — an operator alias that shows first with the connector-derived name in parentheses (e.g. `Tom (+44123…)`), the human fallback for chats WhatsApp only exposes as a bare number. It also lets you inspect the classifier: the LLM system prompt and keyword roots are shown read-only (edited in their `src/analysis/prompts/` files by design), while the safe settings subset (connector, classifier, notifier, hub model — persisted to `config/local.json`; Telegram token/chat-id masked and persisted to `config/webapp_config.json`) is editable. Scan frequency stays in App Launcher's Jobs tab. Endpoints: `GET /api/chats`, `GET /api/chats/{id}/history`, `POST /api/chats/{id}/status`, `POST /api/chats/{id}/alias`, `GET`/`POST /api/config`. **Execution** runs the pipeline — whole or in pieces — and streams it live, mirroring App Launcher's job-run view: each action spawns the matching `launcher.py` command as a subprocess and tails its combined output beside a parsed funnel. Pick the steps to run (Sync · Process · Message) and a Live/Dry-run mode (dry-run simulates the whole pipeline on stored data — no sync, no delivery; live composes the ticked steps, where Message means "send"), then watch the run: a funnel (synced → monitored-with-delta → Stage 1 → Stage 2 LLM → actionable → notification status), the would-be/sent Telegram message, and the live output log, with a recent-runs history and a WhatsApp **connection health** dot (the sidecar's pairing/heartbeat). When the connection is down the health pill grows a one-tap **Reconnect** that relaunches the sidecar, and — when the device needs re-linking — shows the **pairing QR right in the phone UI** (served no-cache from `GET /api/sidecar/qr`) so a non-technical household member can re-pair from their phone without a terminal (`GET /api/sidecar/status`, `POST /api/sidecar/start`). A **Maintenance** card offers **Sync** (incremental, idempotent upsert from the connector buffer) and a guarded **Rebuild** (full cache rebuild for after a reader-logic change — backs up the DB, preserves monitored/ignored/alias state across the canonical re-key, resets run history). Runs are single-flight (one at a time, sharing the SQLite store). Endpoints: `POST /api/execution/run`, `GET /api/execution/runs`, `GET /api/execution/runs/{kind}/{id}`, `POST /api/execution/runs/{kind}/{id}/kill`, `GET /api/execution/health`. Audit is still an empty shell that Step 7 fills.

```powershell
.\setup.bat                 # one-shot: .venv + deps + PWA icons
.\webapp.bat                # run the webapp standalone (HTTP, or HTTPS if a cert exists)
.\tray.bat                  # adopt-or-spawn the webapp behind a tray icon (daily use)
.\tray.bat --restart        # stop the running tray + reclaim :8455, start fresh

# Optional hardening / access:
.\.venv\Scripts\python.exe scripts\gen_token.py        # turn the bearer gate ON
.\.venv\Scripts\python.exe scripts\set_password.py PW  # add a login password
.\.venv\Scripts\python.exe scripts\gen_ssl_cert.py     # Tailscale TLS + iOS trust profile
```

Restart matrix:

| Command | Effect |
| --- | --- |
| `tray.bat` | no-op if a WhatsApp Radar tray is already running |
| `tray.bat --restart` | kills only this repo's tray + reclaims `:8455` by PID (scoped to this `.venv` — never a blanket `pythonw` kill), then relaunches |
| `webapp.bat` | standalone server, no tray (headless / dev iteration) |

Secrets (bearer token, password, passkey state, **and the Telegram token/chat id**) live in the gitignored `config/webapp_config.json` (`config/webapp_config.sample.json` is the template). `WR_TELEGRAM_*` env still overrides it. Confirm the live build with `GET /api/version` → `{git_sha, built_at, asset_hash}`.

Verification gate:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src app
powershell -File scripts\verify-before-ship.ps1   # all of the above + Playwright e2e (Chromium + WebKit/iPhone)
```

The offline suite needs no browsers; the e2e smoke tests self-boot the webapp on a free port and require `playwright install chromium webkit` once.

## Repository Status

Spike complete end-to-end. On top of the fixture foundation (read-only connector, SQLite store, cursor/delta review engine, validated LLM JSON contract, consolidated digest), the repo now has: the real WhatsApp linked-device connector (read-only Node/Baileys sidecar + Python reader), baseline-to-now on first monitor, a multilingual (ES/EN/CA) cascade classifier that gates LLM calls behind a keyword prefilter, and retryable Telegram delivery. See [`docs/manual.md`](docs/manual.md) and [`docs/linked-device.md`](docs/linked-device.md). The fixture connector and offline stub classifier remain the default so the whole suite runs with no credentials.
