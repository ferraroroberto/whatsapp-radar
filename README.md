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

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# Ingest sanitized fixture chats/messages into local SQLite, then pick chats to monitor.
.\.venv\Scripts\wr.exe ingest
.\.venv\Scripts\wr.exe chats
.\.venv\Scripts\wr.exe monitor 1
.\.venv\Scripts\wr.exe monitor 3

# First review classifies the delta and prints one consolidated digest.
.\.venv\Scripts\wr.exe review --dry-run
# Second review with no new messages does nothing and produces no notification.
.\.venv\Scripts\wr.exe review --dry-run
```

Adding new messages causes only the delta to be reviewed; the per-chat cursor advances only after analysis is persisted, so a classifier error safely reprocesses the same delta next run.

Classification defaults to the offline stub. Set `WR_CLASSIFIER=hub` to route through [local-llm-hub](../local-llm-hub) (the `agentic_light` model on `127.0.0.1:8000`), or `WR_CLASSIFIER=cascade` (recommended for real use) to run a cheap multilingual keyword prefilter first that gates the LLM call — so "utter noise" deltas never reach the model. Both prompt assets are inspectable plain-text files you can tune without touching code: the system prompt at `src/whatsapp_radar/analysis/prompts/classification_system.md` and the cascade's actionable roots (Spanish/English/Catalan) at `src/whatsapp_radar/analysis/prompts/keyword_roots.txt`.

## Running Against Real WhatsApp + Telegram

The fixture path above needs no credentials. To run against real chats and deliver digests to Telegram, follow the full step-by-step guide in [`docs/manual.md`](docs/manual.md). In short:

1. Pair a WhatsApp **linked device** with the read-only Node sidecar (`cd sidecar && npm install && npm start`, then scan the QR). It writes a local buffer under the ignored `data/linked_device/`.
2. Set `WR_CONNECTOR=linked_device`; `wr ingest` / `wr chats` / `wr monitor` / `wr review` then run unchanged against real data.
3. For delivery, create a Telegram bot, set `WR_NOTIFIER=telegram` plus `WR_TELEGRAM_BOT_TOKEN` / `WR_TELEGRAM_CHAT_ID`, and `wr review` delivers one consolidated digest. `wr notify` re-delivers a run if a send failed.

The connection is **read-only by construction** — no send/react/read-receipt surface exists. The unofficial-library risk (Baileys), the buffer contract, the message-normalization set, and answers to the spike questions are documented in [`docs/linked-device.md`](docs/linked-device.md). Credentials/session live only under ignored `auth/`; Telegram secrets live only in the ignored `.env`.

Verification gate:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
```

## Repository Status

Spike complete end-to-end. On top of the fixture foundation (read-only connector, SQLite store, cursor/delta review engine, validated LLM JSON contract, consolidated digest), the repo now has: the real WhatsApp linked-device connector (read-only Node/Baileys sidecar + Python reader), baseline-to-now on first monitor, a multilingual (ES/EN/CA) cascade classifier that gates LLM calls behind a keyword prefilter, and retryable Telegram delivery. See [`docs/manual.md`](docs/manual.md) and [`docs/linked-device.md`](docs/linked-device.md). The fixture connector and offline stub classifier remain the default so the whole suite runs with no credentials.
