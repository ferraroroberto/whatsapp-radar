# WhatsApp Radar — Operator Manual

The day-to-day reference once the system is set up: the CLI surface, the classifier choice, routine operation, and troubleshooting. To stand the system up from zero (install, pair WhatsApp, create the Telegram bot, certs/tunnels/passkeys, App Launcher wiring), follow [`bootstrapping.md`](bootstrapping.md) first. For the connector design and unofficial-library risk, see [`linked-device.md`](linked-device.md).

> **Privacy first.** Everything you pair, ingest, and store stays on this machine under ignored paths (`auth/`, `data/`, `config/webapp_config.json`). Never commit credentials, session state, chat names, phone numbers, or message exports. Run `git status --ignored` before any commit. See [`CLAUDE.md`](../CLAUDE.md) for the full rules.

## What the system does

1. A **Node sidecar** pairs a WhatsApp **linked device** (read-only) and writes every chat/message it sees to a local buffer.
2. The **Python core** (`wr`) ingests that buffer into SQLite, lets you choose which chats to monitor, and reviews only *new* messages since the last run.
3. An **LLM classifier** decides whether the new messages contain anything actionable (deadlines, payments, forms, RSVPs, direct requests).
4. A **Telegram notifier** delivers one consolidated digest to a chat of your choice — only when something needs attention.

The connection is **read-only**: there is no code path that sends a WhatsApp message, reaction, or read receipt.

## CLI surface

Run any command via `wr.bat <cmd>`, `python launcher.py <cmd>`, or `python -m app.cli.main <cmd>`.

| Command | What it does |
| --- | --- |
| `status` | Report connector pairing/heartbeat (`connected=True/False`) |
| `ingest` | Pull the connector buffer into SQLite (idempotent; dedupes on `(chat_id, source_message_id)`) |
| `chats [--recent] [--limit N]` | List discovered chats with ids; `--recent` orders by last activity |
| `monitor <id>` / `ignore <id>` | Mark a chat watched (baselines its cursor to "now") / silenced |
| `review [--dry-run]` | Classify the delta of already-ingested monitored chats; deliver unless `--dry-run` |
| `scan [--dry-run] [--days N]` | One-shot: sync → classify → digest → deliver, with a full audit trace |
| `gmail-survey [--days N] [--max-messages N]` | Count a bounded Gmail whitelist window and regenerate generic Gmail taxonomy/roots through the local hub |
| `notify [--run N]` | Re-deliver a run's digest (retry a failed send without re-analyzing) |
| `resync` | Force a fresh sync from the connector buffer |
| `reprocess --confirm` | Full cache rebuild after a reader-logic change (backs up DB, preserves monitor/ignore/alias) |
| `calendar-scan [--dry-run]` | Family calendar-conflict scan; a live scheduled run always sends one Telegram summary |
| `traffic-check [--dry-run]` | Traffic-jam check for the next upcoming commute; self-skips (no alert, no run row) when `traffic.cadence_min` hasn't elapsed since the last check (#170) |
| `tray` | Launch the tray surface |

## The one-shot `scan` (what you schedule)

`scan` collapses the whole flow — sync → keyword prefilter (Stage 1) → LLM (Stage 2) → digest → deliver — into one run, and persists a **full per-run audit trace** so every decision is inspectable.

```powershell
.\wr.bat scan                 # live: sync all chats, analyze monitored deltas, deliver one digest
.\wr.bat scan --dry-run       # replay stored messages: no connector, no delivery, no cursor advance
.\wr.bat scan --dry-run --days 7   # dry-run windowed to the last 7 days
```

Live `scan` advances each chat's cursor only **after** that chat's analysis and trace are persisted, so a classifier error safely reprocesses the same delta next run. `--dry-run` replays history straight from SQLite — it never touches the connector, delivers, or advances a cursor — so it's the safe way to preview what a run *would* do. Funnel counters land on `review_runs`; the per-chat decision record lands on `analysis_trace`. Both are surfaced read-only in the PWA's **Audit** tab.

A live `scan` / `resync` **preflights the source**: if the sidecar's heartbeat is stale it first tries to relaunch the sidecar (when the device is still paired) and re-checks; set `WR_SIDECAR_AUTOSTART=0` to disable the self-heal. If the source still isn't live it **aborts loudly** — exits non-zero, records the run failed, advances no cursor, and fires an alert — so a scheduled job against a dead sidecar can never report green while checking nothing.

A live `scan` then waits for the **buffer to settle** before it reads: a freshly (re)connected sidecar streams history in asynchronously, so reading mid-stream would under-report — and because a live scan advances cursors over what it read, the un-synced tail would be skipped for good. The gate waits until the buffer stops growing for `WR_SYNC_SETTLE_SECONDS` (default 12), hard-capped at `WR_SYNC_SETTLE_TIMEOUT` (default 90, then it reads anyway); set `WR_SYNC_SETTLE_SECONDS=0` to disable. With the tray keep-alive (below) holding the buffer warm this is a near-instant no-op — it only does real waiting right after a reconnect. `resync` skips the gate by design: it is idempotent and never advances a cursor, so a mid-backfill resync simply finishes on the next run. **Net effect: one `scan` whenever you like is enough — no pre-warming syncs, never a stale or half-loaded read.**

`review` remains for analyzing an already-ingested buffer without a sync; `notify` re-delivers if a send failed (the analysis and cursors are already saved):

```powershell
.\wr.bat notify             # re-deliver the latest run's digest
.\wr.bat notify --run 12    # re-deliver a specific run
```

## Choosing the classifier

Set `WR_CLASSIFIER` (in `.env` or via the PWA's Messages & Config settings):

- **`stub` (default, offline):** keyword-based, deterministic, no network. Good for a first run.
- **`hub`:** routes the whole delta through [local-llm-hub](../../local-llm-hub) on `127.0.0.1:8000`.
- **`cascade` (recommended for real use):** a cheap multilingual keyword prefilter (Spanish/English/Catalan) runs first; only deltas that show an actionable signal cost an LLM call.

The classifier assets are plain text, so **read and tune them freely** without touching code:

- `src/analysis/prompts/classification_system.md` — the channel-neutral instruction sent to Stage 2.
- `src/analysis/prompts/keyword_roots.txt` — WhatsApp's multilingual Stage-1 roots.
- `src/analysis/prompts/gmail_classification_taxonomy.md` — Gmail survey/reference bucket definitions used to generate Stage-1 rules; this file is not sent to Stage 2.
- `src/analysis/prompts/gmail_keyword_roots.txt` — Gmail's `bucket | root` Stage-1 rules.

The PWA's **Messages → Classifier & settings** disclosure renders all four assets read-only with their paths, plus the effective Gmail sender/label whitelist and actual history scope. Stage 2 is shared but receives an explicit `Source: WhatsApp` or `Source: Gmail` line. The **Audit** drill-down is the execution proof: it shows the exact source, input messages, Stage-1 buckets/roots, rendered system/user prompt, raw LLM response, parsed verdict, and delivered digest evidence.

To derive Gmail rules from the configured whitelist without copying personal mail into Git or logs:

```powershell
.\wr.bat gmail-survey                     # 60-day window, at most 100 full samples
.\wr.bat gmail-survey --days 30 --max-messages 50
```

The command resolves only configured senders/labels, retrieves metadata to print aggregate count/date scope before analysis, then sends one bounded sample to local-llm-hub. Structured output containing configured/sample identifiers, addresses, domains, or URLs is rejected before either asset is replaced. Review `git diff -- src/analysis/prompts` after every survey and never commit personal additions.

The hub call pins `temperature=0` so identical messages classify identically. Use a model that answers with JSON directly (the default `claude_sonnet` does). A reasoning model whose `<think>` trace overruns the output budget (`WR_HUB_MAX_TOKENS`, default 8192) truncates before the JSON — recorded as a distinct `llm_truncated` audit state, not a generic `contract_error`. The delta sent in one prompt is capped at `WR_HUB_MAX_PROMPT_CHARS` (default 24000, oldest messages dropped) so a whole-history scan can't blow the model's context window.

## Routine operation

- Keep the **sidecar running** — it captures live messages and history. While the **tray** is open it keeps itself alive: a keep-alive supervisor re-checks the sidecar every `WR_SIDECAR_SUPERVISE_SECONDS` (default 90) and relaunches it if the process died (never killing a live one; on a phone-side logout it toasts you to re-pair rather than crash-looping). So the buffer stays warm continuously and a scan reads an already-current source. App Launcher can also supervise it headlessly; see [`bootstrapping.md`](bootstrapping.md) Step 7.
- The scheduled **`family-radar-scan`** Job (App Launcher Jobs tab) syncs, analyzes only the delta, delivers at most one digest, and records a full audit trace. `family-radar-calendar-sync` and `family-radar-traffic-check` are its sibling family-check Jobs — see [`bootstrapping.md`](bootstrapping.md) Step 7 for the full table (schedule, and the traffic-check cadence self-skip, #170).
- For day-to-day driving, use the **admin PWA** (`tray.bat`, then open it on the phone): **Messages** filters stored channels by source and monitoring state; **Run** shows separate truthful WhatsApp/Gmail status cards and per-source funnels; **Audit** is the read-only trust surface over every message's Stage-1/LLM path. WhatsApp keeps one-tap reconnect/re-pair. Gmail shows a masked account plus the configured whitelist but never credentials or OAuth tokens.
- Pairing survives restarts; only a phone-side logout requires re-pairing (delete `auth/`, re-run the sidecar, or re-pair from the Execution tab).

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `status` shows *sidecar not started* | Node sidecar isn't running | Start it (`cd sidecar && npm start`) or use the Execution tab's Reconnect |
| `status` shows *not paired* | QR not scanned yet | Scan the QR (sidecar terminal, or the Execution tab) |
| `status` shows *stale* | Sidecar process stopped/crashed | Restart `npm start`, or let a live `scan` self-heal it |
| `ingest` finds 0 chats | History not synced yet | Wait a moment after pairing, re-run `ingest` |
| Delivery `failed` | Bad token/chat id or no network | Fix the Telegram secrets, then `wr notify` |
| Empty digest every run | Nothing ingested/monitored, no new delta, or Stage 1 rejected everything | Check Messages source/status filters, Run's per-source card/funnel, then Audit; tune source-specific rules only when the evidence shows they are too strict |
| Scheduled scan exits non-zero | Source was dead (sidecar down) — by design | Bring the sidecar back; the run alerted rather than reporting green |

## Verification gate (for contributors)

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src app
powershell -File scripts\verify-before-ship.ps1   # the above + Playwright e2e (Chromium + WebKit/iPhone)
```

The suite runs entirely offline against sanitized fixtures — no WhatsApp credentials, no network, no Telegram.
