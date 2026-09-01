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
- The admin PWA (six tabs — Dashboard, Messages & Config, Execution, Audit, Family, Follow-ups) handles connection status, discovered chats/senders, monitor/ignore decisions, classifier configuration, and the family-check rules.
- App Launcher schedules the three jobs (`family-radar-scan`, `family-radar-calendar-sync`, `family-radar-traffic-check`) through its Jobs tab and opens the admin UI through its Apps tab.

The internal module map is [`docs/architecture.mmd`](docs/architecture.mmd).

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
| [`docs/family-checks.md`](docs/family-checks.md) | the family checks and travel blocks in full — behaviour, every knob, and the rollout runbook |
| [`docs/architecture.mmd`](docs/architecture.mmd) | internal module map |

## Running offline (no personal data)

The app runs end-to-end with a deterministic sanitized fixture connector and a deterministic stub classifier, so it needs **no WhatsApp credentials and no network**. All runtime state lives under the ignored `data/` path.

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

`scan` collapses the monitored-message flow — sync → keyword prefilter (Stage 1) → LLM (Stage 2) → digest → deliver — into one run, and persists a **full per-run audit trace** so every decision is inspectable: what synced, what passed the keyword stage, the exact LLM prompt and raw response, the verdict, and what was delivered.

```powershell
.\wr.bat scan                 # live: sync all chats, analyze monitored deltas, deliver one digest
.\wr.bat scan --dry-run       # replay stored messages with no connector, no delivery, no cursor advance
.\wr.bat scan --dry-run --days 7   # dry-run windowed to the last 7 days
```

Live `scan` advances each cursor only after that chat's analysis and trace are persisted — the same retry-safe guarantee as `review`. `--dry-run` replays history straight from SQLite: it never touches the connector, never delivers, and never advances a cursor, so it is the safe way to see what a run *would* do. Funnel counters land on `review_runs`; the per-chat decision record lands on `analysis_traces`.

**Multi-source sync.** The committed default is WhatsApp-only. Set `sources` in the ignored `config/local.json`, use the source switches in Messages & Config, or set `WR_SOURCES=whatsapp,gmail`. The `connector` / `WR_CONNECTOR` setting still chooses WhatsApp's fixture or linked-device reader. Each enabled source is preflighted, ingested, tagged, and logged independently.

**Preflight — a run that cannot check anything fails loudly.** A live `scan` / `resync` preflights each source before reading it. If the WhatsApp sidecar's heartbeat is stale (the process stopped) it first tries to relaunch the sidecar — when the device is still paired — and re-checks; set `WR_SIDECAR_AUTOSTART=0` to disable that self-heal. If the source still is not live it **aborts loudly**: exits non-zero, records the run as failed, advances no cursor, and fires an alert to the notification channel, rather than reporting green while checking nothing. The **classifier** end of the pipeline aborts the same way: if the LLM hub is unreachable — down, timing out, or erroring — the run stops at the first chat rather than timing out once per monitored chat, is recorded as failed with `notification_status=classifier_offline` (distinct from a source `offline`), fires the alert, exits non-zero, and holds every cursor so the unanalysed deltas are retried next run.

**Keeping the buffer warm.** Two mechanisms stop a scan ever reading a down or half-loaded source. *Keep-alive:* while the tray is open a supervisor re-checks the sidecar every `WR_SIDECAR_SUPERVISE_SECONDS` (default 90) and relaunches it if the process died — never killing a live one, and toasting you to re-pair on a phone-side logout. *Settled-buffer gate:* before a live `scan` reads, it waits until the buffer stops growing for `WR_SYNC_SETTLE_SECONDS` (default 12, capped by `WR_SYNC_SETTLE_TIMEOUT`, then reads anyway; `0` disables), so a scan coinciding with a reconnect's async history backfill cannot read early and advance cursors over messages it never saw. With keep-alive holding the buffer warm the gate is a near-instant no-op, so **one `scan` whenever you like is enough — no pre-warming syncs**. `resync` skips the gate by design: it is idempotent and never advances a cursor.

**Classifier.** Defaults to the offline stub. Set `WR_CLASSIFIER=hub` to route through [local-llm-hub](../local-llm-hub) (the `claude_sonnet` model on `127.0.0.1:8000`), or `WR_CLASSIFIER=cascade` (recommended for real use) to run a cheap multilingual keyword prefilter first that gates the LLM call, so "utter noise" deltas never reach the model. The trade-offs are in [`docs/manual.md`](docs/manual.md#choosing-the-classifier).

Use a model that answers with JSON directly — the default `claude_sonnet` does. A reasoning model that emits a long `<think>` trace can overrun the token budget and return nothing parseable; that case is recorded as a distinct `llm_truncated` trace state rather than a generic contract error. The output budget is configurable (`WR_HUB_MAX_TOKENS`) and the per-prompt delta is capped (`WR_HUB_MAX_PROMPT_CHARS`) so a whole-history scan cannot blow the model's context window.

Both prompt assets are inspectable plain-text files, tunable without touching code: the system prompt at `src/analysis/prompts/classification_system.md` and the cascade's actionable roots (Spanish/English/Catalan) at `src/analysis/prompts/keyword_roots.txt`.

To stop a repeated to-do being re-alerted every run, Stage 2 also receives a **short-term alert memory** — the actionable items already surfaced for that chat (or family) over the last `WR_HUB_RECENT_ALERT_DAYS` days (default 7) — and is instructed not to raise them again unless the information is genuinely new or the matter is now more urgent (e.g. a deadline moved closer). It is built fresh from the persisted alert log each run, so an intervening noise message cannot wipe it.

## Running Against Real WhatsApp + Telegram

The fixture path above needs no credentials. To run against real chats and deliver digests to Telegram, follow the from-zero runbook in [`docs/bootstrapping.md`](docs/bootstrapping.md), then [`docs/manual.md`](docs/manual.md) for day-to-day operation. In short:

1. Pair a WhatsApp **linked device** with the read-only Node sidecar (`cd sidecar && npm install && npm start`, then scan the QR). It writes a local buffer under the ignored `data/linked_device/`.
2. Set `WR_CONNECTOR=linked_device` and leave `WR_SOURCES=whatsapp`. `wr ingest` / `chats` / `monitor` / `review` / `scan` / `resync` / `reprocess --confirm` then run unchanged against real data. `scan`, `resync`, and `reprocess` are also launchable as plain processes from App Launcher's Jobs tab, and appear live in the webapp's Execution tab.
3. For delivery, create a Telegram bot, set `WR_NOTIFIER=telegram` plus `WR_TELEGRAM_BOT_TOKEN` / `WR_TELEGRAM_CHAT_ID`, and `wr review` delivers one consolidated digest. `wr notify` re-delivers a run if a send failed.

The connection is **read-only by construction** — no send, react, or read-receipt surface exists. The buffer contract, the message-normalization set, and the connector design answers are in [`docs/linked-device.md`](docs/linked-device.md). Credentials and session state live only under the ignored `auth/`; Telegram secrets live in the gitignored `config/webapp_config.json`, or the ignored `.env` via `WR_TELEGRAM_*`.

### Gmail source

Gmail is an optional second source using the official Gmail API with the read-only `gmail.readonly` scope. Named senders and labels from the ignored `config/local.json` are the explicit whitelist (full-history ingest): a sender or label becomes a channel, an email becomes a message, attachments are never downloaded, and sender matches take precedence over labels so one email cannot create duplicate digest lines.

On top of that whitelist, **sender-level monitoring** discovers every sender active in the last `discovery_days` (default 30, capped at `discovery_max_messages` metadata reads so a huge mailbox never floods the store) and lists them on the Messages tab, where one tap promotes a sender to monitored — it then enters the classification pipeline like a monitored WhatsApp chat, baselined to new mail.

A retention pass on each successful Gmail sync prunes messages from **unmonitored** senders older than `retention_days` (default 30) and drops any sender left with no recent mail. **Monitored senders are exempt** — their history is kept like a monitored WhatsApp chat — and WhatsApp data is never touched.

Gmail uses its own editable Stage-1 taxonomy and roots, while Stage 2 explicitly receives `Source: Gmail` and the neutral channel name. Run `wr gmail-survey` to count a bounded 60-day whitelist window, show its aggregate date scope, and use one local-llm-hub pass to replace the generic Gmail assets after privacy validation.

OAuth credentials and the refresh token live under the ignored `auth/gmail/`. [`docs/gmail-bootstrap.md`](docs/gmail-bootstrap.md) covers registration, token creation, whitelist configuration, survey, verification, renewal, and troubleshooting. The OAuth, whitelist, paginated search, metadata/count, bounded retrieval, and MIME-normalization implementation is a framework-neutral root package consumed through a thin adapter; [`docs/gmail-reuse.md`](docs/gmail-reuse.md) documents the files, dependencies, standalone command, examples, tests, and byte-for-byte adoption path for other applications.

**Household child registry.** An optional `children` list in `config/local.json` (`[{"name": ..., "aliases": [...], "class_name": ...}]`, empty by default in `config/default.json`) lets Gmail-source Stage-2 classification resolve which registered child a school email concerns, a short free-text task category, and whether the requested prep is `routine` or `non_routine`. The registry is injected into the per-request classification prompt only — never the committed system prompt — and only for `source: gmail`; WhatsApp, and any Gmail household with no registry configured, are unaffected. The resolved fields are visible per-run in the Audit tab's parsed verdict.

**Routine-prep calendar reminders.** When a live scan resolves a Gmail-school item as `action_required`, `prep_complexity: "routine"`, with a registered `child` and a `deadline_date`, it also creates one morning-of-deadline calendar event through the `calendar_write` adapter ([`docs/calendar-bootstrap.md`](docs/calendar-bootstrap.md)) — alongside, never instead of, the Telegram alert.

It is opt-in: set `family.reminder_calendar_id` (the target calendar id) in `config/local.json`. An empty value (the default) keeps the pipeline exactly as before, and a not-yet-minted write token (`auth/calendar/write_token.json`) degrades the same way — no event, no error, Telegram unaffected. `family.reminder_time` (default `07:30`) is the local `HH:MM` slot each event is created at. Creation is live-mode only (a dry run never mints one) and idempotent per item: the same underlying evidence messages, even reclassified in a later run, reuse the first run's event id rather than duplicating it. The created event id is visible on the item's row (`analysis_items.calendar_event_id`).

**Non-routine acknowledgment surface.** A non-routine item (`prep_complexity: "non_routine"`) instead gets a distinct, acknowledgeable follow-up — always on, no config — so it does not blend into routine reminders and get forgotten. It queues a row in `ack_items` (child, task category, summary) alongside the Telegram alert, whose text says a webapp confirmation is needed, and the **Follow-ups** tab lists every pending item with a one-tap **Acknowledge** action. Live-mode only, same as the calendar reminder.

### Voice-note transcription

With transcription enabled, the sidecar downloads each voice note's audio to the ignored `data/linked_device/media/`, and a transcription phase in every live `scan` (between sync and analysis) sends it to the local LLM Hub's Whisper endpoint. Without it, voice notes flow through as the literal text `[voice note]` and anything spoken rather than typed is silently missed.

Transcribed audio is **retained for playback** for `audio_retention_days`: in the Chats overlay the 🎤 marker becomes a tap-to-play/stop control that streams the note from an authenticated, read-only endpoint (`GET /api/messages/{id}/audio`, gated by the same auth as the rest of the API; the `<audio>` element passes the token via `?token=`, and loopback bypasses). WhatsApp voice notes are OGG/Opus, which iOS Safari cannot play in an `<audio>` element, so the endpoint **transcodes to MP3 on the fly** with ffmpeg for universal playback, falling back to the original bytes if ffmpeg is unavailable. The control appears on any voice note whose audio is still on disk, so a note can be played back even before — or if — its transcription completes. A sweep at the start of each transcription phase deletes audio past the window and clears its `media_path`, after which the control disappears and the endpoint 404s; the transcript is kept either way. Audio is more sensitive than text, so the window is short by default and the files never leave the gitignored buffer dir. Set `audio_retention_days: 0` to delete the audio immediately on a successful transcription.

It is **off by default** and opt-in, routing through the hub directly — no extra dependency, no detour through voice-transcriber. Configure it under `transcription` in `config/default.json` (override per-host in `config/local.json`, or via `WR_TRANSCRIPTION_*` env):

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Master switch; `false` makes the phase a no-op (voice notes stay `[voice note]`). |
| `window_days` | `7` | Only *never-attempted* voice notes from the last N days are transcribed; older ones are marked `skipped_old` and never fetched, so a fresh pairing never transcribes years of backlog. Notes that already *failed* get the longer `failed_retry_days` leash instead. |
| `failed_retry_days` | `30` | How long a note that already *failed* keeps being retried (and its audio kept) before giving up. A failure means a transient outage, not backlog, so it retries on **every** full sync regardless of `window_days` — bounded here so a multi-day outage recovers without keeping sensitive audio forever. |
| `audio_base_url` | `http://127.0.0.1:8000` | The hub's audio base URL (its `:8000` proxy keeps the call in the hub's observability ring); `/v1/audio/transcriptions` is appended. |
| `model` | `whisper-vanilla` | OpenAI-shape model id sent in the multipart form. `whisper-vanilla` is the hub's glossary-free turbo path that auto-detects the source language. Do **not** use the plain turbo (`whisper-1`) — it carries an English glossary and defaults to `en`, Englishizing non-English notes. |
| `language` | `auto` | `auto` infers each chat's language from its own text (chats are single-language) and passes it as the Whisper hint, so a note transcribes in its real language regardless of any backend auto-detect bias. Falls back to the backend's auto-detect when a chat has too little text. Pin to an ISO code (e.g. `es`) to force one language for every note. |
| `timeout_seconds` | `120` | Per-file transcription request timeout. |
| `audio_retention_days` | `7` | Days a transcribed note's audio is kept on disk for playback before the sweep deletes it. `0` deletes the audio immediately on success (no playback). |

Transcribe-only, never translation. Failures are isolated and **retried on every full sync** up to `failed_retry_days`: a voice note whose transcription errors is held back from analysis and the cursor never advances past it, so its real transcript is never skipped, while analysis of the other chats proceeds. Because a failed note keeps its audio and retries regardless of `window_days`, a backend outage lasting longer than the transcribe window still recovers the whole backlog once the backend is back.

When the **whole** whisper backend is unreachable — connection refused, or a `502`/`503`/`504` gateway error, as opposed to one bad file — the batch short-circuits after the first note: one `whisper backend unreachable` line instead of one warning per pending note, and every remaining note is left untouched (still `pending` / `failed`, not flipped) for the next scan to retry.

> **Requires `ffmpeg` on PATH.** WhatsApp voice notes are OGG/Opus, but the hub's whisper backend only decodes WAV, so the transcription client transcodes each note to 16 kHz mono WAV with ffmpeg before sending. Without ffmpeg, transcription fails with a clear error (and the note is retried); analysis is unaffected.

**Why the language hint exists.** The hub's shared turbo whisper-server carries an English tech-dictation glossary as its initial prompt, which biases pure audio auto-detect toward English — a Spanish note comes back Englishized. Rather than personalize anything app-side, the app passes the correct standard `language` hint per note. A plain-vanilla, glossary-free transcription option in the hub, reusable by any caller, is tracked in [`ferraroroberto/local-llm-hub#128`](https://github.com/ferraroroberto/local-llm-hub/issues/128); once it lands, `language: auto` can rely on unbiased audio detection directly.

### On-demand summaries and read-aloud

A long message in the Chats overlay — a long voice-note transcript or a long typed message — shows a **Summarize** control. It is strictly on-demand (ingest, sync, and scan never generate a summary) and the result is **persisted** to `messages.summary`, an additive nullable column: the first tap dials the hub's `claude_haiku`; every later tap, page reload, or overlay reopening returns the stored text with no further hub call. Retranscribing a voice note clears its stored summary automatically (`store.mark_transcription`), so a retranscription can never leave a stale summary visible or spoken.

**Play summary aloud** streams the summary through one of four logical voice profiles — `en_female` / `en_male` / `es_female` / `es_male` — resolved entirely server-side from the message's own context, never from the client:

- **Language** is detected deterministically from the *original* message text (never the summary), using the same `langdetect` pattern the transcription phase uses for its Whisper hint. Below ~20 characters of text, a detector error, or any language other than Spanish all fall back to English — the feature only distinguishes English and Spanish.
- **Gender** comes from an explicit per-sender mapping (`sender_voice_genders` in `config/webapp_config.json`, keyed by the lowercased/trimmed sender label) with a configured fallback (`default_voice_gender`, default `"female"`) for any unmapped or unlabeled sender. It is never inferred from a name — only an explicit mapping counts.

The hub model and voice behind each profile are committed, non-secret config under `tts.profiles` in `config/default.json` (override per-host in `config/local.json`):

| Profile | Default model | Default voice |
| --- | --- | --- |
| `en_female` | `orpheus-tts` | `tara` |
| `en_male` | `orpheus-tts` | `leo` |
| `es_female` | `kokoro-tts` | `ef_dora` |
| `es_male` | `kokoro-tts` | `em_alex` |

A resolved voice's own backend being unavailable (e.g. `kokoro-tts` not loaded on the hub) surfaces as `503`, distinct from a general hub outage (`502`), so the two failure modes are never confused in logs or the UI. Summary **text** is persisted; synthesized **audio** stays streamed and ephemeral — never written to disk, never retained.

## Admin Webapp (phone-first PWA)

A FastAPI + vanilla-JS admin PWA runs on port **8455**, mirroring App Launcher's auth/tunnel model: a bearer token (loopback bypasses it), an optional login password, WebAuthn passkeys (enrolled from the tray, ceremonies Tailscale-only), a real Tailscale-issued HTTPS cert (see [HTTPS certificate (Tailscale)](#https-certificate-tailscale)), and dormant Cloudflare named-tunnel scaffolding. All six tabs are live.

The UI follows the fleet design system (`design.md` v2): **light + dark themes** with a toggle in the Dashboard's *Family Radar* identity card (stored per device, defaulting to the OS preference), the floating bottom-tab navigation pill on the phone, Lucide icons (no emojis), home-automation's control recipes (ghost `range-tab` segmented selectors, accent-tinted ghost buttons, a red-tinted danger variant), and the shared component shells vendored verbatim from `project-scaffolding` under `app/webapp/static/_vendored/` (nav, card, disclosure, switch, editor dialog, icons, empty-state). Do not edit vendored files per-app — re-vendor from the scaffold. There is no Settings panel: the build-identity line lives in a footer visible under every tab, and the passkey-enrollment card appears on the Dashboard only while the tray's enrollment window is open. The webapp serves HTTPS directly once a Tailscale cert is provisioned — no per-device CA install, no trust profile — and falls back to plain HTTP on a fresh clone with no cert yet.

The bottom pill gives every tab an equal slice of the phone's width and ellipsizes anything longer, so its labels are kept to ≤6 characters and read shorter than the section names used below: **Home** = Dashboard, **Inbox** = Messages & Config, **To-do** = Follow-ups. `Run`, `Audit` and `Family` are the same in both places. Keep new labels short — the nav component is vendored and must not be edited to make a longer one fit.

### Dashboard

Leads with a **last-activity grid**: one card per kind of work — **WhatsApp · Gmail · Traffic · Calendar** — each showing a source icon, the relative last-run time, an outcome badge (`OK` / `N alerts` / `KO` / `never ran`), and a distilled "what we found" line (e.g. *12 new · 1 actionable*, *no significant delay*, *2 conflicts · 1 missing location*). The data comes from the unified run store, so CLI- and App-Launcher-launched runs appear here too; tapping a card jumps to that run's detail on the Execution tab.

Below the grid, a collapsible **Sources** card (WhatsApp, Gmail, and a read-only Calendar row, each with its icon) and a folded-by-default **Monitored channels** table give the provenance detail. A linked family folds into its parent as one row whose count and last-activity span the whole family.

- `GET /api/dashboard`

### Messages & Config

**Choosing what is watched.** A searchable Monitored/Ignored/All list ordered by last activity, with a single watch toggle per row and a tap-to-open conversation overlay that pages older messages in. Marking a chat monitored baselines its review cursor to only new messages.

A **Chats worth monitoring** card appears above the list when a recent message in a discovered chat matches that source's Stage-1 rules; it shows only the distilled roots/buckets and offers one-tap promotion. This tripwire reads only the last 7 days by default, caps both total messages and messages per chat, excludes monitored / explicitly ignored / linked-child chats, and never invokes Stage 2. Its bounds live under `tripwire` in `config/default.json` — `window_days`, `max_messages`, `max_messages_per_chat`, `nudge_cadence_days` — each overridable by `WR_TRIPWIRE_WINDOW_DAYS`, `WR_TRIPWIRE_MAX_MESSAGES`, `WR_TRIPWIRE_MAX_MESSAGES_PER_CHAT`, and `WR_TRIPWIRE_NUDGE_CADENCE_DAYS`. The in-app card is always available; Telegram stays quiet by default — set `tripwire.telegram_nudge_enabled: true` (or `WR_TRIPWIRE_TELEGRAM_NUDGE_ENABLED=true`) to send at most one nudge per `nudge_cadence_days` while hits remain.

**Per-chat operator metadata.** From the overlay you can **rename** a chat with the pencil button (an alias that shows first with the connector-derived name in parentheses, e.g. `Tom (+44123…)`) and use the **link** button to merge the same person reached under two different numbers into one family. Linked children drop out of the chat list (the parent shows a link-count badge), the parent overlay shows a time-ordered merged history across the family, and monitoring/review/digest treat the family as one subject. Linking is pure local metadata — reversible, moves no message data, manual only.

**Per-source behaviour.** The tab has independent monitoring-state and source selectors, source badges on every channel (all in the same neutral style — source identity comes from the icon and name, not pill color), and source-appropriate history. Gmail shows timestamp, sender, subject, body, and thread id, and the history overlay carries a **sender chip**; WhatsApp retains linking, aliases, voice playback, and merged history. The Monitored/Ignored vocabulary and watch-toggle wording are shared across sources: with the Gmail filter active the list is of **senders (email addresses)** from the last `discovery_days` rather than WhatsApp "channels" — the one wording difference — but the not-monitored bucket still reads **Ignored** and the toggle still reads "tap to ignore" / "tap to monitor". Internally a Gmail demote still lands on the `discovered` status, preserving the discovery window and retention exemptions.

**Classifier configuration** is deliberately visible in the same tab. The shared Stage-2 system prompt, WhatsApp Stage-1 roots, Gmail Stage-1 buckets/rules, the Gmail survey taxonomy (a rule-generation reference, not an LLM prompt), the effective whitelist, and the actual Gmail history scope are all shown read-only and labelled with their source files — they are edited in `src/analysis/prompts/` by design. The safe settings subset is editable: connector, classifier, notifier, and hub model persist to `config/local.json`; the Telegram token and chat id are masked and stored in `config/webapp_config.json`. Scan frequency stays in App Launcher's Jobs tab. Audit shows the exact rendered prompts actually sent, so configured intent and runtime behavior can be compared directly.

The guarded **Rebuild** (full cache rebuild — backs up the DB, preserves monitored/ignored/alias state and family links, resets run history) lives in this tab's **Maintenance** card, since it operates on the local message cache.

**Send to Task-OS** (#307) is a per-message control in the history overlay: tapping it POSTs the message to [task-os](../task-os)'s Inbox (`POST /api/tasks`) as a task titled from the message text, with sender/chat/timestamp folded into the description. Off by default — set `task_os.enabled: true` (or `WR_TASK_OS_ENABLED=1`) and `WR_TASK_OS_TOKEN` in `.env` to turn it on; `WR_TASK_OS_BASE_URL` defaults to `http://127.0.0.1:8448`. The export is idempotent: the first successful send persists to `messages.task_exported_at` and every later tap on the same message reads that back instead of posting again (task-os's own `POST /api/tasks` doesn't yet dedupe on `external_id` — [task-os#98](../task-os) — so this repo is what actually protects a retry). Not-configured and upstream failures surface as a toast, never a silent no-op.

- `GET /api/chats`, `GET /api/chats/tripwire`, `GET /api/chats/{id}/history`, `POST /api/chats/{id}/status`
- `POST /api/chats/{id}/alias`, `POST /api/chats/{id}/link`, `POST /api/chats/{id}/unlink`
- `GET /api/messages/{id}/audio` — streams a voice note's retained audio for in-overlay playback
- `POST /api/messages/{id}/summarize` — on-demand, **read-through** hub summary of a long message; the first call persists it to `messages.summary`, every later call returns the stored text with no further hub call
- `POST /api/messages/{id}/task-export` — send a message to task-os's Inbox as a task (#307); read-through on `messages.task_exported_at` once sent
- `GET /api/tts/health`, `POST /api/tts/speak` — reachability probe plus an ephemeral headerless PCM16 stream of a message's stored summary, `{message_id}`-addressed with the voice profile resolved server-side
- `GET`/`POST /api/config`

### Execution

The single place where everything runs, mirroring App Launcher's job-run view. Runs are single-flight.

- **Messages & calendar sync** — picks the steps (Sync messages · Process messages & email · Send alerts, plus an independent **Calendar sync** step) and a Live/Dry-run mode, then streams a funnel (synced → monitored-with-delta → Stage 1 → Stage 2 LLM → actionable → notification status), the would-be/sent Telegram message, and the live output log.
- **Traffic jam insurance** (folded by default) — the enable toggle, the check cadence in minutes (`traffic.cadence_min`, self-skipped against by every `wr traffic-check` fire so an edit here takes effect with no App Launcher re-arm), a one-off Run now (live/dry), and a last-check / last-alert status line from the unified run store.
- **Recent runs**, **Selected run detail**, **Recent syncs** (each ingest: *timestamp · source · chats/messages added*).
- **Sources health** — WhatsApp, Gmail, and a read-only **Calendar** row (token, calendar count, last successful fetch). Each card distinguishes configured, enabled, authorized/connected, whitelisted, stored, monitored, and last-checked state; Gmail displays only a masked connected account and never returns token, secret, client, OAuth-payload, or credential-path values. When WhatsApp is down its card offers one-tap **Reconnect** and, when re-linking is required, shows the **pairing QR right in the phone UI**.

**Every run carries its output, whoever launched it.** A run fired outside the webapp — from `wr.bat`, or by an App Launcher Job on a schedule — writes the same run record the webapp writes for the runs it spawns, teeing its own stdout/stderr into `webapp/runs/<kind>/<run-id>/output.log` (see `app/cli/runlog.py`). Because the record carries the DB run id from the `__WR_RESULT__` sentinel, it merges with its DB row into a single entry, so **Selected run detail** shows a scheduled check's actual log. Only the launchable verbs are captured — `scan`, `review`, `resync`, `reprocess`, `notify`, `calendar-scan`, `traffic-check`; `status` / `chats` / `monitor` / `ignore` record nothing. A webapp-spawned run is captured once, not twice: the webapp sets `WR_RUN_CAPTURED=1` on its child and the CLI stands down.

A check that self-skips records too, reading as *skipped* with its reason, so a fire that deliberately did nothing is distinguishable from one that failed. Self-skips are hidden from **Recent runs** behind a "N self-skipped runs hidden" line; `GET /api/execution/runs?include_skipped=true` still returns them. See [`docs/family-checks.md`](docs/family-checks.md#run-records-and-self-skips) for the two self-skip rules.

`webapp/runs/` is gitignored and each kind is pruned to its newest 200 records after every run finishes and once at webapp startup (`app/webapp/runs.py::prune_runs`). The active in-flight run is never touched, and a "running" record older than 24h — a crashed run whose finalize never ran — is treated as dead rather than kept forever.

- `POST /api/execution/run`, `GET /api/execution/runs`
- `GET /api/execution/runs/{kind}/{id}`, `POST /api/execution/runs/{kind}/{id}/kill`
- `GET /api/execution/health`, `GET /api/execution/syncs`
- `GET /api/sidecar/status`, `POST /api/sidecar/start`, `GET /api/sidecar/qr`

### Audit

A read-only trust surface over the persisted per-run trace: every recorded run of every kind — message scans, process runs, and the family checks — live vs dry-run, filterable by kind, most recent first, with resync/reprocess maintenance events interleaved.

Drilling into a message run shows, per channel, the source, complete decision record, per-message Stage-1 buckets/roots, whether the LLM flagged it, the exact LLM prompts sent, the raw model response, the parsed verdict, the final action, and the Telegram text it contributed. When a run synced messages but none landed in a monitored channel, the drill-down says so explicitly. Family-check runs drill into their structured payload — every route checked, every conflict — instead of a per-chat trace.

A folded **Filtered out** review card spans runs and shows every recent non-actionable decision over a selectable 7/30/90-day window, with a distilled Stage-1/Stage-2 reason and one-tap drill-through to its full run; the API is bounded and paged. Consecutive live scans that abort with every source offline are collapsed into one attention-state **coverage gap** marker showing the start/end, elapsed days, failed-scan count, and recovery time; an isolated transient failure remains an individual run.

- `GET /api/audit/runs`, `GET /api/audit/filtered?days=N&limit=N&offset=N`, `GET /api/audit/runs/{id}`

### Family

The rules command center for the two family checks. A collapsible **Rules in force** card makes every rule editable from the phone — the daily-scan enable switch, the on-duty weekday pattern, kids-home time, childcare windows, quiet hours, the significant-delay threshold, and the train-commute leave-now exemption — and a **Travel blocks** card holds that feature's toggles, its dwell/title knobs, the per-calendar write-capability readout, and the last sweep's counts. Home address and calendar accounts stay read-only and file-provisioned. Edits save to `config/local.json` and the next run picks them up with no restart; a bad save is rejected server-side with a message naming the field.

Full behaviour and every knob: [`docs/family-checks.md`](docs/family-checks.md).

- `GET /api/family` — rules, recent runs, the travel-block knobs, per-calendar write capability, last-sweep summary, and `live_sweep_blockers` (the reasons a live sweep would write nothing, reported for the run control and enforced independently by the sweep itself)
- `POST /api/family` — the full editable schedule, toggles, and threshold, plus `travel_blocks_enabled` / `travel_blocks_dry_run` / `min_home_dwell_min` / `title_template`; all validated, and a rejection names the field

### Follow-ups

The non-routine acknowledgment surface: pending items with a one-tap Acknowledge action.

- `GET /api/ack/items`, `POST /api/ack/{id}/acknowledge`

### Running the webapp

```powershell
.\setup.bat                 # one-shot: .venv + deps + PWA icons
.\.venv\Scripts\python.exe scripts\gen_tailscale_cert.py  # provision HTTPS (see below)
.\webapp.bat                # run the webapp standalone (HTTPS when a cert is present)
.\tray.bat                  # adopt-or-spawn the webapp behind a tray icon (daily use)
.\tray.bat --restart        # stop the running tray + reclaim :8455, start fresh

# Optional hardening / access:
.\.venv\Scripts\python.exe scripts\gen_token.py        # turn the bearer gate ON
.\.venv\Scripts\python.exe scripts\set_password.py PW  # add a login password
```

Restart matrix:

| Command | Effect |
| --- | --- |
| `tray.bat` | no-op if a WhatsApp Radar tray is already running |
| `tray.bat --restart` | kills only this repo's tray + reclaims `:8455` by PID (scoped to this `.venv` — never a blanket `pythonw` kill), then relaunches |
| `webapp.bat` | standalone server, no tray (headless / dev iteration) |

Secrets — bearer token, login password, passkey state, **and the Telegram token/chat id** — live in the gitignored `config/webapp_config.json` (`config/webapp_config.sample.json` is the template). `WR_TELEGRAM_*` env still overrides it. The same file holds the summary-speech sender-gender preferences (`sender_voice_genders`, `default_voice_gender`); edit the JSON directly, as with `tailnet_allowlist` — there is no UI form for either. Confirm the live build with `GET /api/version` → `{git_sha, built_at, asset_hash}`.

## HTTPS certificate (Tailscale)

Fleet standard: `ferraroroberto/project-scaffolding#89`. Provision a **real Let's Encrypt cert** via `tailscale cert` — no self-signed CA, no per-device trust dance:

```powershell
.\.venv\Scripts\python.exe scripts\gen_tailscale_cert.py
# then: tray.bat --restart
```

One-time prereq: enable **DNS → HTTPS Certificates** in the [Tailscale admin console](https://login.tailscale.com/admin/dns). The script auto-detects the MagicDNS name and writes `webapp/certificates/cert.pem` + `key.pem`. Every device on the tailnet then trusts `https://<host>.<tailnet>.ts.net:8455` natively — no CA install, no profile, no Certificate Trust toggle.

**Renewal is automatic.** The Let's Encrypt leaf lives ~90 days, so every uvicorn-boot path (`tray.bat` via the webapp manager, `webapp.bat`) runs `gen_tailscale_cert.py --check` first, which renews only a `.ts.net` cert expiring within 30 days and no-ops on any other cert. No calendar entry needed.

> **Loopback and LAN URLs:** the Tailscale cert is issued *only* for the ts.net name, so `https://127.0.0.1:8455` and LAN-IP URLs show a hostname-mismatch warning by design — open the webapp via the ts.net URL on the PC too. With no cert at all the server runs plain HTTP on loopback, which is fine for a fresh clone; but iOS Safari needs HTTPS for the PWA and the WebAuthn passkey ceremonies, so provision the Tailscale cert before phone use.

## Family checks (calendar conflicts, traffic, travel blocks)

Two deterministic scheduled checks plus one opt-in calendar-write feature, living alongside the WhatsApp/Gmail pipeline and reusing this app's run store, notifier, config, and admin UI — but not its message-analysis core. **No LLM runs in any of these loops**; detection is plain Python (`src/family/`, `src/traffic/`), unit-tested offline.

| Feature | Verb | Default | What it does |
| --- | --- | --- | --- |
| **Traffic-jam insurance** | `wr traffic-check` | off | Finds each household member's next commute, resolves the origin (live phone position when available, otherwise calendar inference), prices the drive with the Google Routes API, and alerts on Telegram on a significant delay, a departure moment reached, or a back-to-back hop that cannot be made. Deduped, quiet-hours aware, and skips train commutes for the two driving-ETA judgments. |
| **Calendar sync** | `wr calendar-scan` | off | Scans the next few days of both household calendars, flags coverage gaps against the weekly responsibility pattern and same-person overlaps, and sends exactly one Telegram summary per live run — findings, or an explicit all-clear. |
| **Commute travel blocks** | rides inside `wr calendar-scan` | off **and** dry | Computes the commute blocks each person's calendar should carry, reconciles against what is already there, and writes only the difference. Ships off and dry-run; turning it on is a deliberate operator sequence. |

Both checks are independently toggleable — from the **Family** tab, the Execution tab's *Traffic jam insurance* card, or `WR_FAMILY_ENABLED` / `WR_TRAFFIC_ENABLED`. Everything household-identifying — home address, calendar ids, the responsibility pattern, childcare windows, the Routes API key — lives only in the gitignored `config/local.json`; `config/default.json` is the committed schema. Provisioning (Calendar OAuth + the Routes key) is in [`docs/calendar-bootstrap.md`](docs/calendar-bootstrap.md).

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

**Credential lifecycles.** `docs/bootstrapping.md`'s **Recurring maintenance** table covers the app-level secrets (bearer token / login password, Telegram bot token, sidecar re-pairing) on an on-leak / on-compromise cadence, not a calendar one. None of the three Google OAuth grants (Gmail read, Calendar read, Calendar write) rotate on a calendar schedule either — each keeps working until revoked, left unused for six months, or the account password changes while its scope is present (see [`docs/gmail-bootstrap.md`](docs/gmail-bootstrap.md#token-lifecycle-and-revocation) and [`docs/calendar-bootstrap.md`](docs/calendar-bootstrap.md)). The Tailscale HTTPS leaf is the one credential here that *is* calendar-anchored, and it renews itself automatically — see [HTTPS certificate (Tailscale)](#https-certificate-tailscale).

## Verification

Run the gate from the repo root with the project venv:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src app
powershell -File scripts\verify-before-ship.ps1   # all of the above + Playwright e2e (Chromium + WebKit/iPhone)
```

The offline suite needs no browsers; the e2e smoke tests self-boot the webapp on a free port and require `playwright install chromium webkit` once.

The same gate runs in CI on every branch push and PR to `main` ([`.github/workflows/e2e.yml`](.github/workflows/e2e.yml)) — the local gate stays the contract; the workflow just creates the `.venv` it expects and calls it unmodified. The WebKit/iPhone e2e leg is the flaky one on the hosted runner, so CI sets `WR_E2E_TIMEOUT_SCALE=3` to give every browser wait budget 3× headroom (local runs leave it unset and keep Playwright's native budgets) and gives only the WebKit projection a bounded rerun.

## Repository Status

The app runs day to day against real WhatsApp + Telegram, with Gmail, the two family checks, and travel blocks available as opt-in sources and features — all built on the same foundation used for offline development: a read-only connector, a SQLite store, a cursor/delta review engine, a validated LLM JSON contract, and a consolidated digest. The linked-device connector baselines each chat to now on first monitor, a multilingual (ES/EN/CA) cascade classifier gates LLM calls behind a keyword prefilter, and Telegram delivery is retryable independently of analysis. The fixture connector and offline stub classifier remain the default, so the whole suite still runs with no credentials.
