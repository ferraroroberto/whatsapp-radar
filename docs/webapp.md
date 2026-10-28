# Admin webapp (phone-first PWA)

A FastAPI + vanilla-JS admin PWA runs on port **8455**, mirroring App Launcher's auth/tunnel model: a bearer token (loopback bypasses it), an optional login password, WebAuthn passkeys (enrolled from the tray, ceremonies Tailscale-only), a real Tailscale-issued HTTPS cert (see [HTTPS certificate](#https-certificate-tailscale)), and dormant Cloudflare named-tunnel scaffolding.

The UI follows the fleet design system (`design.md` v2): **light + dark themes** with a toggle in the `home-head` header row rendered at the top of every pane (stored per device, defaulting to the OS preference), the floating bottom-tab navigation pill on the phone, Lucide icons (no emojis), home-automation's control recipes (accent-tinted ghost buttons, a red-tinted danger variant), and the shared component shells vendored verbatim from `project-scaffolding` under `app/webapp/static/_vendored/` (`home-head`, nav, card, disclosure, switch, modal with its `select-native` and `button` companions, icon-button, icons, empty-state, text-size, toast, segmented, action-row, row-menu). Do not edit vendored files per-app — re-vendor from the scaffold.

**Settings is never a tab.** The same `home-head` row carries a Settings gear beside the theme toggle on every pane, opening the `#settingsDialog` modal (contents in [Settings](#settings) below); the build-identity line lives in a footer visible under every tab, and the passkey-enrollment card appears on Home only while the tray's enrollment window is open. The webapp serves HTTPS directly once a Tailscale cert is provisioned — no per-device CA install, no trust profile — and falls back to plain HTTP on a fresh clone with no cert yet.

The bottom pill gives every tab an equal slice of the phone's width and ellipsizes anything longer, so its labels are kept to ≤6 characters and read shorter than the section names used below: **Inbox** = Messages. `Home`, `Activity` and `Family` are the same in both places. Keep new labels short — the nav component is vendored and must not be edited to make a longer one fit.

## Home

Answers one question: is the radar working, and does anything need me? Every tab's header carries a context line that names only the most severe exception, else one muted fact (Home: an offline source or a failed run, else *Scanned 18:00*; Activity: a failed scan or the actionable count today; Inbox: chats worth a look, else how many are monitored; Family: a missing calendar token, a missing Routes API key, or missing locations, else *Rules in force*; Settings: an offline source, else an open passkey enrollment window, else the build; Home also names an open enrollment window after its exceptions).

- **Glance card** — one row per source (**WhatsApp · Gmail · Calendar · Traffic**): its glyph with a liveness badge (connected, or should be and is not), the last run as a relative time with a distilled line (e.g. *12 new · 1 actionable*, *no significant delay*), and chips **only for exceptions** (Offline, Failed, *N actionable*, a traffic alert, calendar items to check) — no OK badge. Rows are drawn at once with a dash while loading, so nothing shifts when the poll answers.
- **Links into Activity** — every glance row, and Home's header line when it names an exception, opens that run's sheet in Activity at the right scope.
- **Latest runs** — the last three runs of any kind, as Activity's own row, plus *All activity*.

The data comes from the unified run store, so CLI- and App-Launcher-launched runs appear here too. The per-source counts (stored, monitored, latest) live on each source's sheet in Settings → *Sources*; the monitored channels list is Inbox's Monitored scope.

- `GET /api/dashboard`, `GET /api/execution/health` (liveness), `GET /api/audit/runs?limit=3`

## Activity

One list over the one run store: every recorded run of every kind — message scans, process runs, the family checks, and the webapp-launched syncs and rebuilds — newest first, grouped under Today, Yesterday and Earlier. It is built for the question you have when an alert looks odd: what ran, and why it decided what it decided.

- **Header line** — names the most severe exception (*Running…*, *Last scan failed*, *N actionable today*), else one muted fact (*Last scan 2h ago*).
- **Scope** — a segmented control: **Messages** (the default; every non-family kind), **Calendar**, **Traffic**. The scope narrows the query server-side before its limit, so the daily scans are never crowded out by the traffic checks (about 94% of runs); **Load more** pages older runs.
- **Rows** — a kind glyph, the title, one muted line of time plus the run's last numbers, and chips **only for exceptions**: *Failed* (danger), *Running* and *Dry run* (neutral), *N actionable* (accent), a traffic *alert* or a calendar *conflict* / *missing location* (attention). No OK and no Live stamp. In Traffic, a day's repetitive checks fold into one row (*Today · 31 traffic checks · no alerts*) that expands to the individual checks; a day with an alert says so on the fold. Consecutive live scans that abort with every source offline collapse into one **coverage gap** row (start/end, elapsed days, failed-scan count, recovery time); an isolated transient failure remains an individual run.
- **Run sheet** — tapping a row opens it as a sheet on the phone, or in a right-hand pane on a wide screen: the meta line (status, mode, start, duration, parameters), the funnel (synced → monitored → reviewed → Stage 1 → LLM → actionable → notification status), the per-source split, and for a message run one disclosure per channel with the complete decision record — per-message Stage-1 buckets/roots, whether the LLM flagged it, the exact LLM prompts sent, the raw model response, the parsed verdict, the final action, and the Telegram text it contributed. When a run synced messages but none landed in a monitored channel, it says so. Family-check runs show their structured payload — every route checked, every conflict, the travel-block sweep — above a redacted dump. Then the run's Telegram message and its raw output (closed by default, tap to copy). **Stop** sits in the sheet's footer, only while that run is live; a live run streams.
- **Run now** — the header's play action opens a sheet with the four step switches (Sync messages · Process messages & email · Send alerts, plus an independent **Calendar sync**), a **Dry run** switch with its replay window (New only / Last N days), and one Run button. Runs are single-flight and chained steps queue; the sheet then follows the run live.
- **Filtered out** — a fold row at the end of Messages opens a sheet of every recent non-actionable decision over a 7/30/90-day window, with a distilled Stage-1/Stage-2 reason and one-tap drill-through to its run; the API is bounded and paged.

**Every run carries its output, whoever launched it.** A run fired outside the webapp — from `wr.bat`, or by an App Launcher Job on a schedule — writes the same run record the webapp writes for the runs it spawns, teeing its own stdout/stderr into `webapp/runs/<kind>/<run-id>/output.log` (see `app/cli/runlog.py`). Because the record carries the DB run id from the `__WR_RESULT__` sentinel, it merges with its DB row into a single entry, and the run sheet shows a scheduled check's actual log. Only the launchable verbs are captured — `scan`, `review`, `resync`, `reprocess`, `notify`, `calendar-scan`, `traffic-check`; `status` / `chats` / `monitor` / `ignore` record nothing. A webapp-spawned run is captured once, not twice: the webapp sets `WR_RUN_CAPTURED=1` on its child and the CLI stands down.

A check that self-skips records too, reading as *skipped* with its reason, so a fire that deliberately did nothing is distinguishable from one that failed. Self-skips are hidden from the list behind an "N self-skipped runs hidden" line in the Calendar and Traffic scopes; `GET /api/execution/runs?include_skipped=true` still returns them. See [`family-checks.md`](family-checks.md#run-records-and-self-skips) for the two self-skip rules.

`webapp/runs/` is gitignored and each kind is pruned to its newest 200 records after every run finishes and once at webapp startup (`app/webapp/runs.py::prune_runs`). The active in-flight run is never touched, and a "running" record older than 24h — a crashed run whose finalize never ran — is treated as dead rather than kept forever.

The run store has the same escape: a traffic-check or calendar-scan row still `running` more than 30 minutes after it started has no process behind it, so it is moved to its own terminal state, **`abandoned`** (shown as an *Abandoned* chip, never folded into *failed* or *completed*; `completed_at` stays empty because nobody saw it end). `store.reconcile_stale_runs` does this on webapp startup and before every family check opens its own row, logging one info line per run. A check that hits an unexpected error records `failed`; one that is interrupted records `abandoned`. Scan and process runs have no known duration bound, so they are never reconciled.

- `GET /api/audit/runs?scope=messages|calendar|traffic&limit=N&before=ID`, `GET /api/audit/runs/{id}`, `GET /api/audit/filtered?days=N&limit=N&offset=N`
- `POST /api/execution/run`, `GET /api/execution/runs`
- `GET /api/execution/runs/{kind}/{id}` (a `db-<id>` serves the run's captured record when one exists), `POST /api/execution/runs/{kind}/{id}/kill`

## Messages

**Choosing what is watched.** One scope switch (Monitored · Ignored · All) and one always-shown filter field — a name search with the source (All sources · WhatsApp · Gmail) as a chip inside it — over one list ordered by last activity. Each chat is one row: an initial avatar with a small source mark, the name, and one line of relative time plus the last message's preview, with a plain watch icon button (accent when monitored). A count line shows only while the search or the source chip narrows the list. Tapping a row opens the chat sheet (a right-hand pane on a wide screen), which pages older messages in. Marking a chat monitored baselines its review cursor to only new messages.

A **Worth a look** section appears above the list only when a recent message in a discovered chat matches that source's Stage-1 rules; it shows only the distilled roots/buckets and offers one-tap promotion (an unpainted icon button), and its scope (Stage 1 only, last N days) sits in the section title's tooltip and in that chat's sheet. This tripwire reads only the last 7 days by default, caps both total messages and messages per chat, excludes monitored / explicitly ignored / linked-child chats, and never invokes Stage 2. Its bounds live under `tripwire` in `config/default.json` — `window_days`, `max_messages`, `max_messages_per_chat`, `nudge_cadence_days` — each overridable by `WR_TRIPWIRE_WINDOW_DAYS`, `WR_TRIPWIRE_MAX_MESSAGES`, `WR_TRIPWIRE_MAX_MESSAGES_PER_CHAT`, and `WR_TRIPWIRE_NUDGE_CADENCE_DAYS`. The in-app section is always available; Telegram stays quiet by default — set `tripwire.telegram_nudge_enabled: true` (or `WR_TRIPWIRE_TELEGRAM_NUDGE_ENABLED=true`) to send at most one nudge per `nudge_cadence_days` while hits remain.

**Per-chat operator metadata.** The chat sheet's header shows the source, the message count and the monitoring state, and its one labelled menu offers **Rename…**, **Link chats…** (with the linked count) and **Monitor / Stop monitoring**. **Rename** sets an alias that shows first with the connector-derived name in parentheses, e.g. `Tom (+44123…)`, and **Link chats…** merges the same person reached under two different numbers into one family. Linked children drop out of the chat list (the parent's menu shows how many are linked), the parent overlay shows a time-ordered merged history across the family, and monitoring/review/digest treat the family as one subject. Linking is pure local metadata — reversible, moves no message data, manual only.

**Per-source behaviour.** The tab has independent monitoring-state and source selectors, a source mark on every channel's avatar (source identity comes from the glyph, not colour), and source-appropriate history. Gmail shows timestamp, sender, subject, body, and thread id, and the chat sheet's subtitle carries the **sender address**; WhatsApp retains linking, aliases, voice playback, and merged history. The Monitored/Ignored vocabulary and watch-toggle wording are shared across sources: with the Gmail filter active the list is of **senders (email addresses)** from the last `discovery_days` rather than WhatsApp "channels" — the one wording difference — but the not-monitored bucket still reads **Ignored** and the toggle still reads "tap to ignore" / "tap to monitor". Internally a Gmail demote still lands on the `discovered` status, preserving the discovery window and retention exemptions.

**Send to Task-OS** is a per-message control in the history overlay: tapping it POSTs the message to [task-os](../../task-os)'s Inbox (`POST /api/tasks`) as a task titled from the message text, with sender/chat/timestamp folded into the description. Off by default — set `task_os.enabled: true` (or `WR_TASK_OS_ENABLED=1`) and `WR_TASK_OS_TOKEN` in `.env` to turn it on; `WR_TASK_OS_BASE_URL` defaults to `http://127.0.0.1:8448`. The export is idempotent: the first successful send persists to `messages.task_exported_at` and every later tap on the same message reads that back instead of posting again (task-os's own `POST /api/tasks` does not dedupe on `external_id` — [task-os#98](https://github.com/ferraroroberto/task-os/issues/98) — so this repo is what actually protects a retry). Not-configured and upstream failures surface as a toast, never a silent no-op.

- `GET /api/chats`, `GET /api/chats/tripwire`, `GET /api/chats/{id}/history`, `POST /api/chats/{id}/status`
- `POST /api/chats/{id}/alias`, `POST /api/chats/{id}/link`, `POST /api/chats/{id}/unlink`
- `GET /api/messages/{id}/audio` — streams a voice note's retained audio for in-overlay playback ([voice notes](voice-and-summaries.md))
- `POST /api/messages/{id}/summarize` — on-demand, **read-through** hub summary of a long message; the first call persists it to `messages.summary`, every later call returns the stored text with no further hub call
- `POST /api/messages/{id}/task-export` — send a message to task-os's Inbox as a task; read-through on `messages.task_exported_at` once sent
- `GET /api/tts/health`, `POST /api/tts/speak` — reachability probe plus an ephemeral headerless PCM16 stream of a message's stored summary, `{message_id}`-addressed with the voice profile resolved server-side
- `GET`/`POST /api/config`

## Family

The rules command center for the two family checks, set once and then left, laid out as inset groups whose rows show each rule's current value. **Household** reads the home address, the calendars (a sheet lists them with the token state) and the scan window, all read-only and file-provisioned. **Schedule** holds who's on duty (one row, then a week sheet with a person picker per day), kids home by, childcare windows (add, edit, remove inside their sheet) and quiet hours. **Calendar scan** carries the daily-scan and ask-for-missing-locations switches. **Traffic checks** carries the enable switch with the last-check / last-alert line, the cadence (`traffic.cadence_min`, self-skipped against by every `wr traffic-check` fire so an edit takes effect with no App Launcher re-arm), the significant delay, the train-commute leave-now exemption with its title keywords shown read-only, and Check now (live or dry). **Travel blocks** is one row showing Off, Dry run or Live; it opens the sheet with that feature's toggles, its dwell/title knobs, the per-calendar write-capability readout, the last sweep's counts and the Rehearse / Run sweep controls.

A row that edits something opens a sheet with its control at 44px. A sheet commits on **Done** through `POST /api/family` with a toast; a bad value is rejected server-side with a message naming the field, shown inside the sheet, which stays open. The sheet's close button, the backdrop and Esc discard its edits. The three switches on the page (calendar sync, ask for missing locations, traffic checks) apply on tap. Edits save to `config/local.json` and the next run picks them up with no restart; each sheet names where its rule lives on one *Where this lives* line.

Full behaviour and every knob: [`family-checks.md`](family-checks.md).

- `GET /api/family` — rules, recent runs, the travel-block knobs, per-calendar write capability, last-sweep summary, and `live_sweep_blockers` (the reasons a live sweep would write nothing, reported for the run control and enforced independently by the sweep itself)
- `POST /api/family` — the full editable schedule, toggles, and threshold, plus `travel_blocks_enabled` / `travel_blocks_dry_run` / `min_home_dwell_min` / `title_template`; all validated, and a rejection names the field

## Settings

Settings is the header gear on every pane, never a tab: a dialog laid out as the same inset groups as Family, each row showing its current value and opening a sheet with its control at 44px. A sheet commits on **Done** with a partial `POST /api/config` holding only its own fields, then a toast. A rejected value (the server names it) keeps the sheet open. The sheet's close button, the backdrop and Esc discard its edits.

- **Display** — Text size (Small, Default, Large), inline.
- **Sources** — a row each for WhatsApp, Gmail and Calendar with the connection state and last sync. Each opens that source's sheet: the enable switch (WhatsApp, Gmail) and for WhatsApp the connector, then its stored and monitored counts and latest message, its health, its **Recent syncs** (*timestamp · source · chats/messages added*), and for WhatsApp one-tap **Reconnect** with the **pairing QR right in the phone UI** when re-linking is required. Each source distinguishes configured, enabled, authorized/connected, whitelisted, stored, monitored, and last-checked state; Gmail displays only a masked connected account and never returns token, secret, client, OAuth-payload, or credential-path values. Calendar is read-only here (its daily scan is switched on in Family).
- **How messages are judged** — the Classifier (a sheet with its select), and read-only sheets for the keyword rules (WhatsApp; Gmail, with the effective whitelist and history scope), the Gmail survey taxonomy (a rule-generation reference, never sent to the model) and the instructions to the model (the shared system prompt; each message adds which source it came from). They are edited in `src/analysis/prompts/` by design, and each sheet names its file on one *Where this lives* line. Activity's run sheet shows the exact rendered prompts actually sent, so configured intent and runtime behavior can be compared directly.
- **Alerts** — Delivery (the notifier, the Telegram chat id and the bot token; the token is write-only — the sheet says set or not set, never echoes it, and sends one only when typed) and Model hub (base URL and model). Connector, classifier, notifier and hub persist to `config/local.json`; the Telegram chat id and token to `config/webapp_config.json`. Scan frequency stays in App Launcher's Jobs tab.
- **Maintenance** — *Rebuild message cache* (a full rebuild: backs up the DB, preserves monitored/ignored/alias state and family links, resets run history) opens a confirm sheet stating what it preserves and what resets; it operates on the local message cache.
- **This install** — Passkeys (the enrolled devices, each removed behind an in-sheet confirm, and, only while the tray's enrollment window is open, a device name and *Enroll this device*) and the build. While the window is open, Home's and Settings' header lines read *Passkey enrollment open* (Home's opens the Passkeys sheet).

- `GET /api/config`, `POST /api/config` (partial)
- `GET /api/execution/health`, `GET /api/execution/syncs`
- `GET /api/sidecar/status`, `POST /api/sidecar/start`, `GET /api/sidecar/qr`
- `GET /api/webauthn/status`, `DELETE /api/webauthn/devices/{id}`, `POST /api/webauthn/enroll/{begin,finish}`

## Running the webapp

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

Secrets — bearer token, login password, passkey state, **and the Telegram token/chat id** — live in the gitignored `config/webapp_config.json` (`config/webapp_config.sample.json` is the template). `WR_TELEGRAM_*` env still overrides it. The same file holds the summary-speech sender-gender preferences (`sender_voice_genders`, `default_voice_gender`); edit the JSON directly, as with `tailnet_allowlist`. Confirm the live build with `GET /api/version` → `{git_sha, built_at, asset_hash}`.

**Compression.** `create_app()` registers the vendored `StreamSafeGZipMiddleware` (`src/compression.py`) after every other middleware, so it is the outermost layer and a 401 or 304 passes through unchanged. It gzips HTML, JS, CSS, JSON and SVG of at least 1000 bytes for a client that accepts gzip, and skips streams and media by content type. The one stream, `POST /api/tts/speak` (`audio/L16`), is skipped that way, so no `bypass_paths` are needed; a new stream or media route whose content type is not recognisable goes in `bypass_paths`. `tests/webapp/test_compression.py` pins both the gzip on the shell and assets and the first-chunk-at-once delivery of the TTS stream.

**Usage log.** The PWA records which views and controls get used, and on which device class, so a design review reads data instead of asking. It is local-only: the vendored `usage-log` client posts to this app's own `POST /api/usage/events` (behind the same bearer gate) and `src/usage_log.py` stores kind, view id, control id, a per-visit session id, device class and timestamps in `usage.sqlite3` under the runtime-data root (override with `WR_USAGE_DB_PATH`). Never message text, chat or contact names, phone numbers, chat ids, queries, IPs or the raw user-agent; control ids are the constant `data-track` values in `index.html`. `GET /api/usage/summary?days=N` reads it back.

**Logs.** The tray, the webapp and the CLI each write a rotating file under the ignored `webapp/` directory — `tray.log`, `webapp.log`, `cli.log` (1 MB × 3 backups; plus `auth.log` for sign-in events). The tray runs under `pythonw` with no console, so these files are where its warnings and errors land. They carry ids, counts and exception text, never chat names or message bodies, and only this repo's own loggers are written (not third-party HTTP clients). `WR_LOG_DIR` relocates the directory; set it empty to turn file logging off.

## HTTPS certificate (Tailscale)

Provision a **real Let's Encrypt cert** via `tailscale cert` — no self-signed CA, no per-device trust dance:

```powershell
.\.venv\Scripts\python.exe scripts\gen_tailscale_cert.py
# then: tray.bat --restart
```

One-time prereq: enable **DNS → HTTPS Certificates** in the [Tailscale admin console](https://login.tailscale.com/admin/dns). The script auto-detects the MagicDNS name and writes `webapp/certificates/cert.pem` + `key.pem`. Every device on the tailnet then trusts `https://<host>.<tailnet>.ts.net:8455` natively — no CA install, no profile, no Certificate Trust toggle.

**Renewal is automatic.** The Let's Encrypt leaf lives ~90 days, so every uvicorn-boot path (`tray.bat` via the webapp manager, `webapp.bat`) runs `gen_tailscale_cert.py --check` first, which renews only a `.ts.net` cert expiring within 30 days and no-ops on any other cert. No calendar entry needed.

> **Loopback and LAN URLs:** the Tailscale cert is issued *only* for the ts.net name, so `https://127.0.0.1:8455` and LAN-IP URLs show a hostname-mismatch warning by design — open the webapp via the ts.net URL on the PC too. With no cert at all the server runs plain HTTP on loopback, which is fine for a fresh clone; but iOS Safari needs HTTPS for the PWA and the WebAuthn passkey ceremonies, so provision the Tailscale cert before phone use.
