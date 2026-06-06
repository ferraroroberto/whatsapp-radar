# WhatsApp Radar — Full Onboarding Manual

This is the end-to-end operator guide: go from a clean checkout to receiving a consolidated digest of actionable WhatsApp items in a separate channel (Telegram). It assumes no prior setup.

> **Privacy first.** Everything you pair, ingest, and store stays on this machine under ignored paths (`auth/`, `data/`). Never commit credentials, session state, chat names, phone numbers, or message exports. Run `git status --ignored` before any commit. See `CLAUDE.md` for the full privacy rules.

## What the system does

1. A **Node sidecar** pairs a WhatsApp **linked device** (read-only) and writes every chat/message it sees to a local buffer.
2. The **Python core** (`wr`) ingests that buffer into SQLite, lets you choose which chats to monitor, and reviews only *new* messages since the last run.
3. An **LLM classifier** decides whether the new messages contain anything actionable (deadlines, payments, forms, RSVPs, direct requests).
4. A **Telegram notifier** delivers one consolidated digest to a chat of your choice — only when something needs attention.

The connection is **read-only**: there is no code path that sends a WhatsApp message, reaction, or read receipt. See `docs/linked-device.md` for the design and the unofficial-library risk.

## Prerequisites

- **Python 3.11+** and **Node.js 18+** on the host.
- A **phone with WhatsApp** to pair as a linked device. This can be any account you are authorised to use (for personal use, e.g. a family member's phone, with their consent). The phone keeps working normally and keeps receiving its own notifications.
- Optional but recommended: the [local-llm-hub](../../local-llm-hub) running on `127.0.0.1:8000` for real LLM classification.
- A **Telegram bot** and the **chat id** you want the digest delivered to (setup below).

## Step 1 — Install the Python core

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
```

Verify the offline path works with no credentials and no network (uses the sanitized fixture connector and the deterministic stub classifier):

```powershell
.\wr.bat ingest
.\wr.bat chats
.\wr.bat review --dry-run
```

## Step 2 — Install and run the WhatsApp sidecar

```powershell
cd sidecar
npm install
npm start        # or: node index.js
```

On first run the sidecar prints a **QR code** in the terminal. On the phone you want to monitor:

**WhatsApp → Settings → Linked devices → Link a device → scan the QR.**

After pairing, the sidecar:

- Persists the session under the ignored `auth/` directory (you only pair once).
- Pulls recent history, then listens for live messages.
- Writes to the ignored buffer `data/linked_device/` (`chats.ndjson`, `messages.ndjson`, `status.json`).
- Reconnects automatically if the connection drops; it only needs re-pairing if you log the device out from the phone.

**Leave the sidecar running.** It must stay up to capture live messages — it is a long-running process, not a one-shot. (App Launcher's Jobs/Apps tabs are the intended way to keep it running; that wiring is a later issue.)

## Step 3 — Point the Python core at the live connector

Create an ignored `.env` in the repo root (copy from `.env.example`):

```
WR_CONNECTOR=linked_device
```

Check the connection from Python:

```powershell
.\wr.bat status
```

You should see `Connector: linked_device (connected=True)`. If it says *not paired* or *stale*, the sidecar is not running or not yet paired — revisit Step 2.

## Step 4 — Ingest, then choose what to monitor

```powershell
.\wr.bat ingest                 # pull buffered chats/messages into local SQLite
.\wr.bat chats --recent --limit 40  # most recently-active chats first, with ids
.\wr.bat monitor 3              # mark chat #3 as monitored (repeat per chat)
.\wr.bat ignore 5               # optionally silence a chat
```

On a busy account with hundreds of chats, `chats --recent` (most recent message first) and `--limit N` make the list scannable. Marking a chat **monitored** baselines its cursor to the latest message, so the first review only classifies messages that arrive *after* you start monitoring — you won't get a digest of months of backlog. Only monitored chats are reviewed. Re-running `ingest` is safe and idempotent — storage deduplicates on `(chat_id, source_message_id)`.

## Step 5 — Choose the classifier

- **Default (offline stub):** keyword-based, deterministic, no network. Good for a first run.
- **Real LLM (`hub`):** routes the whole delta through local-llm-hub.
- **Cascade (recommended):** a cheap multilingual keyword prefilter (Spanish/English/Catalan) runs first; only deltas that show an actionable signal cost an LLM call. With the hub running, set in `.env`:

  ```
  WR_CLASSIFIER=cascade
  ```

Two prompt assets are plain-text and loaded verbatim, so you can **read and tune them freely** without touching code:
- `src/analysis/prompts/classification_system.md` — the instruction sent to the model.
- `src/analysis/prompts/keyword_roots.txt` — the cascade's actionable roots (add your own).

The hub call pins `temperature=0` so identical messages classify identically. The digest summary language follows the model's default (currently English); change it by editing the system prompt.

## Step 6 — Set up Telegram delivery

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts. Copy the **bot token** it gives you.
2. Decide the destination chat (e.g. a chat you share with your partner). Add the bot to it if it's a group, or just start a direct chat with the bot.
3. Get the **chat id**: send any message to the destination, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read `chat.id` from the JSON.
4. Put the secrets in your ignored `.env` (never commit them):

   ```
   WR_NOTIFIER=telegram
   WR_TELEGRAM_BOT_TOKEN=123456:abcdef...
   WR_TELEGRAM_CHAT_ID=-1001234567890
   ```

## Step 7 — Run a review and deliver

```powershell
.\wr.bat review              # classify the delta and deliver if actionable
.\wr.bat review --dry-run    # classify and print, but do not deliver
```

Or use **`scan`** — the one-shot that does the whole flow (sync → keyword prefilter → LLM → digest → deliver) and writes a full audit trace of every decision. This is the command to schedule:

```powershell
.\wr.bat scan               # sync all chats, analyze monitored deltas, deliver one digest
.\wr.bat scan --dry-run     # replay stored messages: no connector, no delivery, no cursor advance
.\wr.bat scan --dry-run --days 7   # dry-run windowed to the last 7 days
```

Every `scan` run records its funnel (chats synced, what passed each stage, LLM calls, actionable count, delivery status) on `review_runs`, and a per-chat decision trail — the analyzed delta, the keyword evidence, the exact LLM prompt and raw response, the verdict, and the delivered text — on `analysis_trace`. A later step surfaces these in the admin UI; until then you can read them straight from the SQLite DB.

- If nothing is actionable, **no notification is sent**.
- If delivery fails (network, bad token), the run's analysis and cursors are already saved, so you can retry delivery alone:

  ```powershell
  .\wr.bat notify             # re-deliver the latest run's digest
  .\wr.bat notify --run 12    # re-deliver a specific run
  ```

Running `review` again with no new messages does nothing and produces no notification. The per-chat cursor advances only **after** analysis is persisted, so a classifier error safely reprocesses the same delta next time.

## Routine operation

- Keep the **sidecar running** (it captures live messages and history).
- Schedule `wr scan` periodically (App Launcher Jobs, or run it by hand) — it syncs, analyzes only the delta, delivers at most one digest, and records a full audit trace. (`wr review` remains for analyzing an already-ingested buffer without a sync.)
- Pairing survives restarts; only a phone-side logout requires re-pairing (delete `auth/` and re-run the sidecar).

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `status` shows *sidecar not started* | Node sidecar isn't running | Start it (Step 2) |
| `status` shows *not paired* | QR not scanned yet | Scan the QR in the sidecar terminal |
| `status` shows *stale* | Sidecar process stopped/crashed | Restart `npm start` |
| `ingest` finds 0 chats | History not synced yet | Wait a moment after pairing, re-run `ingest` |
| Delivery `failed` | Bad token/chat id or no network | Fix `.env`, then `wr notify` |
| Empty digest every run | Classifier too strict, or chats not monitored | Check `wr chats`, tune the prompt |

## Verification gate (for contributors)

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src app
```

The test suite runs entirely offline against sanitized fixtures — no WhatsApp credentials, no network, no Telegram.
