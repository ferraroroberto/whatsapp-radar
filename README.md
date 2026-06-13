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

To stand the whole system up from zero — new WhatsApp linked device, new Telegram bot, phone access, App Launcher wiring — follow [`docs/bootstrapping.md`](docs/bootstrapping.md).

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

Two reliability mechanisms keep a scan from ever reading a down or half-loaded source (#73). **Keep-alive:** while the **tray** is open a supervisor re-checks the sidecar every `WR_SIDECAR_SUPERVISE_SECONDS` (default 90) and relaunches it if the process died — never killing a live one, toasting you to re-pair on a phone-side logout — so the buffer stays warm continuously. **Settled-buffer gate:** before a live `scan` reads, it waits until the buffer stops growing for `WR_SYNC_SETTLE_SECONDS` (default 12, capped by `WR_SYNC_SETTLE_TIMEOUT`, then reads anyway; `0` disables) so a scan coinciding with a reconnect's async history backfill can't read early and advance cursors over messages it never saw. With keep-alive holding the buffer warm the gate is a near-instant no-op, so **one `scan` whenever you like is enough — no pre-warming syncs**. (`resync` skips the gate by design — it's idempotent and never advances a cursor.)

Classification defaults to the offline stub. Set `WR_CLASSIFIER=hub` to route through [local-llm-hub](../local-llm-hub) (the `claude_sonnet` model on `127.0.0.1:8000`), or `WR_CLASSIFIER=cascade` (recommended for real use) to run a cheap multilingual keyword prefilter first that gates the LLM call — so "utter noise" deltas never reach the model. Use a model that answers with JSON directly (the default `claude_sonnet` does); a reasoning model that emits a long `<think>` trace can overrun the token budget and return nothing parseable — that case is recorded as a distinct `llm_truncated` trace state (not a generic contract error), the output budget is configurable (`WR_HUB_MAX_TOKENS`), and the per-prompt delta is capped (`WR_HUB_MAX_PROMPT_CHARS`) so a whole-history scan can't blow the model's context window. Both prompt assets are inspectable plain-text files you can tune without touching code: the system prompt at `src/analysis/prompts/classification_system.md` and the cascade's actionable roots (Spanish/English/Catalan) at `src/analysis/prompts/keyword_roots.txt`. To stop a repeated to-do from being re-alerted every run, Stage 2 is also given a **short-term alert memory** — the actionable items already surfaced for that chat (family) over the last `WR_HUB_RECENT_ALERT_DAYS` days (default 7) — and instructed not to raise them again unless the information is genuinely new or the matter is now more urgent (e.g. a deadline has moved closer). It is built fresh from the persisted alert log each run, so an intervening noise message can't wipe it.

## Running Against Real WhatsApp + Telegram

The fixture path above needs no credentials. To run against real chats and deliver digests to Telegram, follow the from-zero runbook in [`docs/bootstrapping.md`](docs/bootstrapping.md) (and [`docs/manual.md`](docs/manual.md) for day-to-day operation). In short:

1. Pair a WhatsApp **linked device** with the read-only Node sidecar (`cd sidecar && npm install && npm start`, then scan the QR). It writes a local buffer under the ignored `data/linked_device/`.
2. Set `WR_CONNECTOR=linked_device`; `wr ingest` / `wr chats` / `wr monitor` / `wr review` / `wr scan` / `wr resync` / `wr reprocess --confirm` then run unchanged against real data. `wr scan`, `wr resync`, and `wr reprocess` are also launchable as plain processes from App Launcher's Jobs tab (and surfaced live in the webapp's Execution tab).
3. For delivery, create a Telegram bot, set `WR_NOTIFIER=telegram` plus `WR_TELEGRAM_BOT_TOKEN` / `WR_TELEGRAM_CHAT_ID`, and `wr review` delivers one consolidated digest. `wr notify` re-delivers a run if a send failed.

The connection is **read-only by construction** — no send/react/read-receipt surface exists. The unofficial-library risk (Baileys), the buffer contract, the message-normalization set, and answers to the spike questions are documented in [`docs/linked-device.md`](docs/linked-device.md). Credentials/session live only under ignored `auth/`; Telegram secrets live in the gitignored `config/webapp_config.json` (or the ignored `.env` via `WR_TELEGRAM_*`).

## Admin Webapp (phone-first PWA)

A FastAPI + vanilla-JS admin PWA runs on port **8455**, mirroring App Launcher's auth/tunnel model: a bearer token (loopback bypasses it), an optional login password, WebAuthn passkeys (enrolled from the tray, ceremonies Tailscale-only), Tailscale TLS, and dormant Cloudflare named-tunnel scaffolding. All four tabs are live.

### Dashboard

Read-only metrics: channels monitored, messages stored, scans run, backlog since the last scan, alerts raised, notifications sent, plus a per-monitored-channel table. A linked family is folded into its parent as one row whose count and last-activity span the whole family.

- `GET /api/dashboard`

### Chats & Config

Pick which chats are watched via a searchable Monitored/Ignored/All list ordered by last activity (single watch toggle per row, tap-to-open conversation overlay that pages older messages in). Marking a chat monitored baselines its review cursor to only new messages. From the overlay you can ✏️ **rename** a chat (an operator alias that shows first with the connector-derived name in parentheses, e.g. `Tom (+44123…)`) and use the 🔗 **link** button to merge the same person reached under two different numbers into one family. Linked children drop out of the chat list (parent shows `🔗N`), the parent overlay shows a time-ordered merged history across the family, and monitoring/review/digest treat the family as one subject. Linking is pure local metadata — reversible, moves no message data, manual only. The tab also surfaces the classifier: the LLM system prompt and keyword roots are shown read-only (edited in `src/analysis/prompts/` by design), while the safe settings subset (connector, classifier, notifier, hub model — persisted to `config/local.json`; Telegram token/chat-id masked and stored in `config/webapp_config.json`) is editable. Scan frequency stays in App Launcher's Jobs tab.

- `GET /api/chats`, `GET /api/chats/{id}/history`, `POST /api/chats/{id}/status`
- `POST /api/chats/{id}/alias`, `POST /api/chats/{id}/link`, `POST /api/chats/{id}/unlink`
- `GET`/`POST /api/config`

### Execution

Runs the pipeline — whole or in pieces — and streams it live, mirroring App Launcher's job-run view. Pick the steps (Sync · Process · Message) and a Live/Dry-run mode, then watch: a funnel (synced → monitored-with-delta → Stage 1 → Stage 2 LLM → actionable → notification status), the would-be/sent Telegram message, and the live output log, with a recent-runs history and a WhatsApp **connection health** dot (sidecar pairing/heartbeat). When the connection is down the health pill grows a one-tap **Reconnect**, and — when the device needs re-linking — shows the **pairing QR right in the phone UI** so a household member can re-pair from their phone without a terminal. A **Recent syncs** card lists each ingest (*timestamp · chats added · messages added*) written by every sync path (webapp Sync button, a scheduled `wr resync` Job, or a live scan). A **Maintenance** card offers **Sync** (incremental upsert) and a guarded **Rebuild** (full cache rebuild — backs up the DB, preserves monitored/ignored/alias state and family links, resets run history). Runs are single-flight.

- `POST /api/execution/run`, `GET /api/execution/runs`
- `GET /api/execution/runs/{kind}/{id}`, `POST /api/execution/runs/{kind}/{id}/kill`
- `GET /api/execution/health`, `GET /api/execution/syncs`
- `GET /api/sidecar/status`, `POST /api/sidecar/start`, `GET /api/sidecar/qr`

### Audit

Read-only trust surface over the persisted per-run trace: a list of every review/scan run (live vs dry-run, parameters, funnel counters), most recent first, with resync/reprocess maintenance events interleaved. Drilling into a run shows, per chat, the complete decision record — a per-message breakdown of every message analyzed (Stage-1 keyword roots matched, whether the LLM flagged it as actionable), the exact LLM prompts sent, the raw model response, the parsed verdict, the final action, and the Telegram text it contributed. When a run synced messages but none landed in a monitored chat, the drill-down says so explicitly.

- `GET /api/audit/runs`, `GET /api/audit/runs/{id}`

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

The same gate runs in CI on every branch push and PR to `main` ([`.github/workflows/e2e.yml`](.github/workflows/e2e.yml)) — the local gate stays the contract; the workflow just creates the `.venv` it expects and calls it unmodified. The WebKit/iPhone e2e leg is the flaky one on the hosted runner, so CI sets `WR_E2E_TIMEOUT_SCALE=3` to give every browser wait budget 3× headroom (local runs leave it unset and keep Playwright's native budgets) and gives only the WebKit projection a bounded rerun.

## Home-stack wiring (App Launcher)

WhatsApp Radar runs as part of the home stack through [App Launcher](../app-launcher): a scheduled `wr scan` digest from the **Jobs** tab, and the admin PWA opened from the **Apps** tab. That wiring lives in App Launcher's gitignored runtime registries (`config/jobs.json`, `config/apps.json`) — machine-local state, not committed here — so it is recreated per box from App Launcher's UI. The full procedure (job name + cadence, the two Apps rows, and the calendar-anchored cert/token rotation schedule) is **Step 7 + Recurring maintenance** in [`docs/bootstrapping.md`](docs/bootstrapping.md).

## Repository Status

Spike complete end-to-end. On top of the fixture foundation (read-only connector, SQLite store, cursor/delta review engine, validated LLM JSON contract, consolidated digest), the repo now has: the real WhatsApp linked-device connector (read-only Node/Baileys sidecar + Python reader), baseline-to-now on first monitor, a multilingual (ES/EN/CA) cascade classifier that gates LLM calls behind a keyword prefilter, and retryable Telegram delivery. To recreate it from zero see [`docs/bootstrapping.md`](docs/bootstrapping.md); for day-to-day operation see [`docs/manual.md`](docs/manual.md) and for the connector design [`docs/linked-device.md`](docs/linked-device.md). The fixture connector and offline stub classifier remain the default so the whole suite runs with no credentials.
