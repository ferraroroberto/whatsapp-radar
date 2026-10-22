# Gmail source

Gmail is an optional second source. Provisioning (OAuth registration, token creation, whitelist, survey, renewal, troubleshooting) is in [`gmail-bootstrap.md`](gmail-bootstrap.md); this page is the behaviour once it is on.

## Whitelist and sender monitoring

Gmail uses the official Gmail API with the read-only `gmail.readonly` scope. Named senders and labels from the ignored `config/local.json` are the explicit whitelist (full-history ingest): a sender or label becomes a channel, an email becomes a message, attachments are never downloaded, and sender matches take precedence over labels so one email cannot create duplicate digest lines.

On top of that whitelist, **sender-level monitoring** discovers every sender active in the last `discovery_days` (default 30, capped at `discovery_max_messages` metadata reads so a huge mailbox never floods the store) and lists them on the Messages tab, where one tap promotes a sender to monitored — it then enters the classification pipeline like a monitored WhatsApp chat, baselined to new mail.

A retention pass on each successful Gmail sync prunes messages from **unmonitored** senders older than `retention_days` (default 30) and drops any sender left with no recent mail. **Monitored senders are exempt** — their history is kept like a monitored WhatsApp chat — and WhatsApp data is never touched.

## Classification

Gmail uses its own editable Stage-1 taxonomy and roots, while Stage 2 explicitly receives `Source: Gmail` and the neutral channel name. Run `wr gmail-survey` to count a bounded 60-day whitelist window, show its aggregate date scope, and use one local-llm-hub pass to replace the generic Gmail assets after privacy validation.

OAuth credentials and the refresh token live under the ignored `auth/gmail/`. The OAuth, whitelist, paginated search, metadata/count, bounded retrieval, and MIME-normalization implementation is a framework-neutral root package consumed through a thin adapter; [`gmail-reuse.md`](gmail-reuse.md) documents the files, dependencies, standalone command, examples, tests, and byte-for-byte adoption path for other applications.

## Household child registry

An optional `children` list in `config/local.json` (`[{"name": ..., "aliases": [...], "class_name": ...}]`, empty by default in `config/default.json`) lets Gmail-source Stage-2 classification resolve which registered child a school email concerns, a short free-text task category, and whether the requested prep is `routine` or `non_routine`. The registry is injected into the per-request classification prompt only — never the committed system prompt — and only for `source: gmail`; WhatsApp, and any Gmail household with no registry configured, are unaffected. The resolved fields are visible per-run in the Activity run sheet's parsed verdict.

## Routine-prep calendar reminders

When a live scan resolves a Gmail-school item as `action_required`, `prep_complexity: "routine"`, with a registered `child` and a `deadline_date`, it also creates one morning-of-deadline calendar event through the `calendar_write` adapter ([`calendar-bootstrap.md`](calendar-bootstrap.md)) — alongside, never instead of, the Telegram alert.

It is opt-in: set `family.reminder_calendar_id` (the target calendar id) in `config/local.json`. An empty value (the default) keeps the pipeline exactly as before, and a not-yet-minted write token (`auth/calendar/write_token.json`) degrades the same way — no event, no error, Telegram unaffected. `family.reminder_time` (default `07:30`) is the local `HH:MM` slot each event is created at. Creation is live-mode only (a dry run never mints one) and idempotent per item: the same underlying evidence messages, even reclassified in a later run, reuse the first run's event id rather than duplicating it. The created event id is visible on the item's row (`analysis_items.calendar_event_id`).

## Non-routine follow-ups (Telegram only)

A non-routine item (`prep_complexity: "non_routine"`) gets no calendar reminder; its line in the Telegram alert says it *needs your own follow-up, not a routine reminder*, so it does not blend into routine reminders and get forgotten. There is no in-app queue or Acknowledge (the `ack_items` table is kept, unused, so no destructive migration runs).
