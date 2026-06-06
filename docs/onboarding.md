# Spike Onboarding

This document is the cold-start guide for the first WhatsApp Radar spike. It intentionally contains no personal data, credentials, chat names, or message samples.

## Objective

Prove whether a local linked-device WhatsApp connector can support a safe, reusable read-only workflow for discovering chats, ingesting messages, tracking per-chat cursors, and producing an actionable-items digest outside WhatsApp.

## First Spike Questions

- Can a linked-device connector reliably pair and reconnect on the target Windows host?
- Can it receive enough chat history and new-message events to support incremental review?
- Are message IDs stable enough to use as cursors?
- Can the connector run without sending messages, reactions, read receipts, or other write-side effects from our code?
- Does the primary phone continue receiving normal notifications while the connector is online?
- How does offline catch-up behave after the service is stopped for several hours?
- What message shapes must be normalized for the first version: text, replies, captions, links, documents, images, voice notes, edited/deleted messages?

## Local-Only Runtime Data

Use ignored paths for all real runtime data:

- `auth/` for linked-device credentials.
- `data/whatsapp-radar.sqlite3` for local state.
- `data/logs/` for local logs.
- `config/local.json` or `.env` for tokens and host-specific settings.

Never commit files from those paths.

## Suggested Spike Shape

Steps 1–7 are implemented (see the README "Running The Spike" section). Step 8 is deferred to a follow-up issue.

1. [done] Create a connector prototype behind a narrow interface: connect, report status, list chats, ingest events, stop.
2. [done] Persist raw-but-local message records into SQLite with stable internal IDs and source message IDs.
3. [done] Add a sanitized fixture connector so storage, cursoring, analysis, and reporting can be developed without WhatsApp access.
4. [done] Build a CLI command that prints discovered chats with IDs and sanitized labels.
5. [done] Build a CLI command that marks chats as monitored in local SQLite.
6. [done] Build a CLI command that reviews monitored chats since the last cursor and emits a dry-run JSON digest.
7. [done] Call local-llm-hub only after the fixture path and cursoring are proven (opt-in `WR_CLASSIFIER=hub`; the offline stub classifier is the default).
8. [deferred] Add Telegram delivery only after the digest JSON is stable.

## Acceptance For The Spike

- A developer can run the fixture connector with no WhatsApp credentials and see a deterministic digest.
- A developer with local credentials can pair a linked device and ingest chats/messages into ignored local storage.
- The system can mark selected chats as monitored.
- Running review twice with no new messages produces no actionable notification.
- Adding new fixture messages processes only the delta.
- The report is consolidated across monitored chats.
- No WhatsApp send/write operation exists in the implemented public surface.

## Handoff Notes

Before starting implementation, read the open enhancement issue and this document. Keep public commits free of personal data. If real WhatsApp pairing is used during development, inspect `git status --ignored` before committing so no credential or message artifact leaks.
