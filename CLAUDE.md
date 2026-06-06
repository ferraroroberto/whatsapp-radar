# Project Instructions

Canonical instructions for AI coding agents working in this repository. `AGENTS.md` points here for non-Claude tools.

## This Repository

WhatsApp Radar is a local-first personal utility for classifying new WhatsApp chat messages and surfacing only actionable items through a separate notification channel. Treat it as a sensitive-data project even though the repository is public.

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

Until the first implementation issue defines project tooling, at minimum run syntax/type checks for touched code and any tests that exist. Do not claim tests pass when no tests exist.

## Planning Discipline

Future work belongs in GitHub issues, not dated planning files. Durable reference material may live under `docs/` when it will still be useful next quarter.
