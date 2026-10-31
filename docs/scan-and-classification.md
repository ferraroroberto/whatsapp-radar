# Scan and classification

How a review turns synced messages into one digest, and how the run fails loudly when it cannot. The offline walkthrough that exercises this path with no credentials is in the [README](../README.md#running-offline-no-personal-data); day-to-day operation and troubleshooting are in [`manual.md`](manual.md).

## One-shot `scan` (the scheduled job)

`scan` collapses the monitored-message flow — sync → keyword prefilter (Stage 1) → LLM (Stage 2) → digest → deliver — into one run, and persists a **full per-run audit trace** so every decision is inspectable: what synced, what passed the keyword stage, the exact LLM prompt and raw response, the verdict, and what was delivered.

```powershell
.\wr.bat scan                 # live: sync all chats, analyze monitored deltas, deliver one digest
.\wr.bat scan --dry-run       # replay stored messages with no connector, no delivery, no cursor advance
.\wr.bat scan --dry-run --days 7   # dry-run windowed to the last 7 days
```

Live `scan` advances each cursor only after that chat's analysis and trace are persisted — the same retry-safe guarantee as `review`. `--dry-run` replays history straight from SQLite: it never touches the connector, never delivers, and never advances a cursor, so it is the safe way to see what a run *would* do. Funnel counters land on `review_runs`; the per-chat decision record lands on `analysis_trace`.

## Multi-source sync

The committed default is WhatsApp-only. Set `sources` in the ignored `config/local.json`, use the source switches in Settings → Sources (the header gear), or set `WR_SOURCES=whatsapp,gmail`. The `connector` / `WR_CONNECTOR` setting still chooses WhatsApp's fixture or linked-device reader. Each enabled source is preflighted, ingested, tagged, and logged independently.

## Preflight — a run that cannot check anything fails loudly

A live `scan` / `resync` preflights each source before reading it. If the WhatsApp sidecar's heartbeat is stale (the process stopped) it first tries to relaunch the sidecar — when the device is still paired — and re-checks; set `WR_SIDECAR_AUTOSTART=0` to disable that self-heal. If the source still is not live it **aborts loudly**: exits non-zero, records the run as failed, advances no cursor, and fires an alert to the notification channel, rather than reporting green while checking nothing.

The **classifier** end of the pipeline aborts the same way: if the LLM hub is unreachable — down, timing out, or erroring — the run stops at the first chat rather than timing out once per monitored chat, is recorded as failed with `notification_status=classifier_offline` (distinct from a source `offline`), fires the alert, exits non-zero, and holds every cursor so the unanalysed deltas are retried next run.

## Keeping the buffer warm

Two mechanisms stop a scan ever reading a down or half-loaded source.

- **Keep-alive.** While the tray is open a supervisor re-checks the sidecar every `WR_SIDECAR_SUPERVISE_SECONDS` (default 90) and relaunches it if the process died — never killing a live one, logging every non-live state change (a launch that fails, e.g. Node missing, is its own `launch_failed` error line in `tray.log`), and toasting you to re-pair on a phone-side logout.
- **Settled-buffer gate.** Before a live `scan` reads, it waits until the buffer stops growing for `WR_SYNC_SETTLE_SECONDS` (default 12, capped by `WR_SYNC_SETTLE_TIMEOUT`, then reads anyway; `0` disables), so a scan coinciding with a reconnect's async history backfill cannot read early and advance cursors over messages it never saw.

With keep-alive holding the buffer warm the gate is a near-instant no-op, so **one `scan` whenever you like is enough — no pre-warming syncs**. `resync` skips the gate by design: it is idempotent and never advances a cursor.

## Classifier

Defaults to the offline stub. Set `WR_CLASSIFIER=hub` (or its alias `cascade`) to route through [local-llm-hub](../../local-llm-hub) (the `claude_sonnet` model on `127.0.0.1:8000`). Stage 1, a cheap multilingual keyword prefilter, runs for every classifier and gates the LLM call, so "utter noise" deltas never reach the model. See [`manual.md`](manual.md#choosing-the-classifier).

Use a model that answers with JSON directly — the default `claude_sonnet` does. A reasoning model that emits a long `<think>` trace can overrun the token budget and return nothing parseable; that case is recorded as a distinct `llm_truncated` trace state rather than a generic contract error. The output budget is configurable (`WR_HUB_MAX_TOKENS`) and the per-prompt delta is capped (`WR_HUB_MAX_PROMPT_CHARS`) so a whole-history scan cannot blow the model's context window.

All four prompt assets are inspectable plain-text files, tunable without touching code: the system prompt at `src/analysis/prompts/classification_system.md`, the cascade's actionable roots (Spanish/English/Catalan) at `src/analysis/prompts/keyword_roots.txt`, the Gmail survey/reference bucket definitions at `src/analysis/prompts/gmail_classification_taxonomy.md` (not sent to Stage 2), and Gmail's `bucket | root` Stage-1 rules at `src/analysis/prompts/gmail_keyword_roots.txt`.

## Short-term alert memory

To stop a repeated to-do being re-alerted every run, Stage 2 also receives the actionable items already surfaced for that chat (or family) over the last `WR_HUB_RECENT_ALERT_DAYS` days (default 7) and is instructed not to raise them again unless the information is genuinely new or the matter is now more urgent (e.g. a deadline moved closer). It is built fresh from the persisted alert log each run, so an intervening noise message cannot wipe it.
