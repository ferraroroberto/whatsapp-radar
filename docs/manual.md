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
| `notify [--run N]` | Re-deliver a run's digest (retry a failed send without re-analyzing) |
| `resync` | Force a fresh sync from the connector buffer |
| `reprocess --confirm` | Full cache rebuild after a reader-logic change (backs up DB, preserves monitor/ignore/alias) |
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

`review` remains for analyzing an already-ingested buffer without a sync; `notify` re-delivers if a send failed (the analysis and cursors are already saved):

```powershell
.\wr.bat notify             # re-deliver the latest run's digest
.\wr.bat notify --run 12    # re-deliver a specific run
```

## Choosing the classifier

Set `WR_CLASSIFIER` (in `.env` or via the PWA's Chats & Config settings):

- **`stub` (default, offline):** keyword-based, deterministic, no network. Good for a first run.
- **`hub`:** routes the whole delta through [local-llm-hub](../../local-llm-hub) on `127.0.0.1:8000`.
- **`cascade` (recommended for real use):** a cheap multilingual keyword prefilter (Spanish/English/Catalan) runs first; only deltas that show an actionable signal cost an LLM call.

Two prompt assets are plain-text and loaded verbatim, so **read and tune them freely** without touching code (they're shown read-only in the PWA, edited in-file by design):

- `src/analysis/prompts/classification_system.md` — the instruction sent to the model.
- `src/analysis/prompts/keyword_roots.txt` — the cascade's actionable roots (add your own).

The hub call pins `temperature=0` so identical messages classify identically. Use a model that answers with JSON directly (the default `claude_sonnet` does). A reasoning model whose `<think>` trace overruns the output budget (`WR_HUB_MAX_TOKENS`, default 8192) truncates before the JSON — recorded as a distinct `llm_truncated` audit state, not a generic `contract_error`. The delta sent in one prompt is capped at `WR_HUB_MAX_PROMPT_CHARS` (default 24000, oldest messages dropped) so a whole-history scan can't blow the model's context window.

## Voice notes

PTT voice notes are a first-class message type. The read-only sidecar downloads encrypted audio to `data/linked_device/media/<msg_id>.ogg` when a voice note arrives; the Python store records `transcription_status=pending` and keeps the placeholder text `[voice note]` until transcription completes.

On a **live `scan`**, after sync and before analysis, the pipeline transcribes pending voice notes via the fleet's Whisper turbo server on **`127.0.0.1:8090`** (`POST /v1/audio/transcriptions`, language auto-detect, no translation). Ensure [local-llm-hub](../../local-llm-hub) has the audio service running before expecting transcripts. On success the transcript overwrites `messages.text` and the audio file is deleted locally.

Configuration (also editable in the PWA's Chats & Config tab):

- `transcription.enabled` in `config/default.json` or `config/local.json` (env: `WR_TRANSCRIPTION_ENABLED`)
- `transcription.window_days` — only voice notes within this many days are transcribed on first run; older ones are marked `skipped_old` (env: `WR_TRANSCRIPTION_WINDOW_DAYS`, default 7)
- `transcription.hub_base_url` / `transcription.model` — Whisper endpoint defaults (`http://127.0.0.1:8090`, `whisper-1`); override via env if needed

Failure behavior: a transcription error marks the row `failed` and retries on the next scan. The per-chat analysis cursor **does not advance** past a pending or failed voice note, so real content is never silently skipped.

## Routine operation

- Keep the **sidecar running** — it captures live messages and history. App Launcher supervises it; see [`bootstrapping.md`](bootstrapping.md) Step 7.
- The scheduled **`wr scan`** Job (App Launcher Jobs tab) syncs, analyzes only the delta, delivers at most one digest, and records a full audit trace.
- For day-to-day driving, use the **admin PWA** (`tray.bat`, then open it on the phone): the Execution tab runs the pipeline live with a connection-health dot and one-tap reconnect/re-pair; the Audit tab is the read-only trust surface over every run's trace.
- Pairing survives restarts; only a phone-side logout requires re-pairing (delete `auth/`, re-run the sidecar, or re-pair from the Execution tab).

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `status` shows *sidecar not started* | Node sidecar isn't running | Start it (`cd sidecar && npm start`) or use the Execution tab's Reconnect |
| `status` shows *not paired* | QR not scanned yet | Scan the QR (sidecar terminal, or the Execution tab) |
| `status` shows *stale* | Sidecar process stopped/crashed | Restart `npm start`, or let a live `scan` self-heal it |
| `ingest` finds 0 chats | History not synced yet | Wait a moment after pairing, re-run `ingest` |
| Delivery `failed` | Bad token/chat id or no network | Fix the Telegram secrets, then `wr notify` |
| Empty digest every run | Classifier too strict, or chats not monitored | Check `wr chats` / Chats & Config, tune the prompt |
| Voice notes stay `[voice note]` | Whisper hub not running on `:8090` | Start local-llm-hub audio service; re-run `wr scan` |
| Scheduled scan exits non-zero | Source was dead (sidecar down) — by design | Bring the sidecar back; the run alerted rather than reporting green |

## Verification gate (for contributors)

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src app
powershell -File scripts\verify-before-ship.ps1   # the above + Playwright e2e (Chromium + WebKit/iPhone)
```

The suite runs entirely offline against sanitized fixtures — no WhatsApp credentials, no network, no Telegram.
