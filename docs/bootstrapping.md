# Bootstrapping — Stand WhatsApp Radar Up From Zero

This is the complete cold-start runbook: take a fresh clone (or a brand-new machine) and bring the whole system online — a new WhatsApp linked device, a new Telegram bot, the admin PWA with phone access, and the App Launcher wiring. Follow it top to bottom and you end with a running radar delivering actionable digests to Telegram.

> **Privacy first.** Everything you pair, ingest, and store stays on this machine under ignored paths (`auth/`, `data/`, `config/webapp_config.json`). Never commit credentials, session state, chat names, phone numbers, or message exports. Run `git status --ignored` before any commit. The full rules live in [`CLAUDE.md`](../CLAUDE.md).

For day-to-day operation once this is done, see [`manual.md`](manual.md). For the connector's design and unofficial-library risk, see [`linked-device.md`](linked-device.md).

## What you are building

```
WhatsApp ──▶ Node sidecar (Baileys) ──▶ data/linked_device/*.ndjson ──▶ wr (Python core) ──▶ SQLite
                                                                              │
                                          classifier (stub | hub | cascade) ──┤
                                                                              ▼
                                                              Telegram digest (read-only, actionable-only)

           Admin PWA (FastAPI + vanilla JS, :8455) ──▶ Dashboard · Chats & Config · Execution · Audit
                         access: Tailscale TLS (default) · Cloudflare named tunnel (optional)
```

The connection is **read-only by construction** — no code path sends a WhatsApp message, reaction, or read receipt.

## 0 — Prerequisites

- **Python 3.11+** and **Node.js 18+** on the host (Windows is the reference platform).
- A **phone with WhatsApp** to pair as a linked device. Use any account you are authorised to (for personal use, a household member's phone, with their consent). The phone keeps working normally and keeps its own notifications.
- Optional but recommended: [local-llm-hub](../../local-llm-hub) running on `127.0.0.1:8000` for real LLM classification.
- For phone access: **Tailscale** installed (default path), and optionally **cloudflared** + a domain on Cloudflare (public path). `winget install Cloudflare.cloudflared`.

## 1 — Install the Python core

From the repo root:

```powershell
.\setup.bat                 # creates .venv, installs runtime + dev deps, generates PWA icons
```

`setup.bat` is the one-shot installer. If you prefer to do it by hand:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts\gen_icons.py
```

Verify the offline path works with **no credentials and no network** (sanitized fixture connector + deterministic stub classifier):

```powershell
.\wr.bat ingest
.\wr.bat chats
.\wr.bat review --dry-run
```

You should see a deterministic digest. If this works, the core is sound and everything from here is about pointing it at real data.

## 2 — Provision a new WhatsApp linked device (the sidecar)

The Node sidecar speaks the WhatsApp Web protocol (Baileys) and writes everything it sees to a local buffer; the Python core only reads that buffer.

```powershell
cd sidecar
npm install
npm start        # or: node index.js
```

On first run the sidecar prints a **QR code** in the terminal. On the phone you want to monitor:

**WhatsApp → Settings → Linked devices → Link a device → scan the QR.**

After pairing, the sidecar:

- persists the session under the ignored `auth/` directory — **you only pair once**;
- pulls recent history, then listens for live messages;
- writes the ignored buffer `data/linked_device/` (`chats.ndjson`, `messages.ndjson`, `status.json`);
- reconnects automatically on transient drops; it only needs re-pairing if you log the device out from the phone.

**Leave the sidecar running** — it is a long-running process, not a one-shot. (App Launcher keeps it supervised; see Step 8. You can also re-pair from the phone later via the Execution tab's QR — see [`manual.md`](manual.md).)

## 3 — Point the core at the live connector

Copy `.env.example` to an ignored `.env` in the repo root and set the connector:

```
WR_CONNECTOR=linked_device
```

Confirm the link:

```powershell
.\wr.bat status
```

Expect `Connector: linked_device (connected=True)`. *Not paired* or *stale* means the sidecar isn't running or isn't paired yet — revisit Step 2.

Then ingest and pick what to watch:

```powershell
.\wr.bat ingest                     # pull the buffer into SQLite (idempotent)
.\wr.bat chats --recent --limit 40  # most recently active first, with ids
.\wr.bat monitor 3                  # watch chat #3 (baselines its cursor to "now")
```

Marking a chat **monitored** baselines its cursor to the latest message, so the first review only classifies messages that arrive *after* you start — no backlog dump. You can also do all of this from the PWA's **Chats & Config** tab once the webapp is up (Step 5).

## 4 — Create a new Telegram bot

1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts. Copy the **bot token**.
2. Decide the destination chat (e.g. a chat shared with your partner). Add the bot to it if it's a group, or start a direct chat with the bot.
3. Get the **chat id**: send any message to the destination, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read `chat.id` from the JSON.
4. Store the secrets. The canonical home is the gitignored `config/webapp_config.json` (`telegram_bot_token` / `telegram_chat_id`) — set them from the PWA's Chats & Config tab, or copy `config/webapp_config.sample.json` and edit. They can also be set as env in `.env` (which overrides the JSON):

   ```
   WR_NOTIFIER=telegram
   WR_TELEGRAM_BOT_TOKEN=123456:abcdef...
   WR_TELEGRAM_CHAT_ID=-1001234567890
   ```

Verify delivery end-to-end:

```powershell
.\wr.bat scan               # sync → classify the delta → deliver one digest if actionable
```

If nothing is actionable, no message is sent — that is correct. See [`manual.md`](manual.md) for the classifier choice (`stub` / `hub` / `cascade`) and tuning.

## 5 — Bring up the admin PWA + phone access (Tailscale)

The phone-first admin PWA (FastAPI + vanilla JS) runs on **:8455**.

```powershell
.\.venv\Scripts\python.exe scripts\gen_ssl_cert.py     # Tailscale TLS: mint local CA + leaf, install iOS trust profile
.\.venv\Scripts\python.exe scripts\gen_token.py        # turn the bearer gate ON (loopback still bypasses)
.\.venv\Scripts\python.exe scripts\set_password.py PW  # optional: add a memorable login password
.\tray.bat                                             # adopt-or-spawn the webapp behind a tray icon (daily use)
```

- `gen_ssl_cert.py` mints a local CA (10-year) + a leaf cert covering `localhost`, the hostname, and the **Tailscale** name, and installs the CA into your Windows trust store. It prints an **iOS trust profile** — install it on the phone so Safari trusts the tailnet HTTPS endpoint. The PWA then serves HTTPS on `:8455` over the tailnet.
- `gen_token.py` turns on the bearer gate; the tray bakes the token into the copied URL (`?token=…`). Open that URL once on the phone and it stashes the token in localStorage.
- Reach the PWA from the phone at `https://<machine>.<tailnet>.ts.net:8455` (on the tailnet).

### Enrol a WebAuthn passkey (Tailscale-only)

From the tray icon menu choose **🔐 Enroll device (5 min)** — it opens a one-time enrollment window. Complete the passkey ceremony **on a device reaching the webapp over Tailscale** (passkey ceremonies are Tailscale-only by design). After enrolling, that device can unlock the PWA with the passkey instead of the token/password.

## 6 — (Optional) Public access via a Cloudflare named tunnel

This path is **dormant until you wire it** — the tray stays Tailscale-only until `webapp/cloudflared.yml` exists. One-time setup (needs `cloudflared` and a domain on Cloudflare):

```powershell
cloudflared tunnel login
cloudflared tunnel create whatsapp-radar          # prints a UUID, writes ~/.cloudflared/<UUID>.json
cloudflared tunnel route dns whatsapp-radar radar.<your-domain>
copy config\cloudflared.sample.yml webapp\cloudflared.yml
# then edit webapp\cloudflared.yml: fill in `tunnel:` (the UUID) and `hostname:`
```

Launch it with `webapp_tunnel_named.bat` (or just `tray.bat` — the tray adopts the same config). The public URL stays the same every launch. Cloudflare's edge provides the public TLS; the origin uses the self-signed cert (`noTLSVerify: true` in the config).

## 7 — Schedule the digest + add the launch surfaces (App Launcher)

WhatsApp Radar runs as part of the home stack through [App Launcher](../../app-launcher): a scheduled `wr scan` digest from the **Jobs** tab, and the admin PWA opened from the **Apps** tab. This wiring lives in App Launcher's gitignored runtime registries (`config/jobs.json`, `config/apps.json`) — machine-local state, not committed in this repo — so it must be recreated per box from App Launcher's UI. Adding the entries below is also what arms the Windows Task Scheduler entry.

### Jobs tab — scheduled digest

From the Jobs tab's **+**, add one job, `whatsapp-radar-scan`, firing `wr scan` (live) **daily at 18:00** (machine-local / Madrid time). App Launcher's executor points at this repo's `launcher.py`, so it resolves this repo's own `.venv`, runs with `cwd` + `PYTHONPATH` set to the repo root, and invokes `…\whatsapp-radar\.venv\Scripts\python.exe launcher.py scan` — no PYTHONPATH juggling needed.

A live `scan` self-heals the sidecar when the device is still paired and **aborts loudly** (non-zero exit, run recorded failed, alert sent) if the source is dead, so a scheduled run can never report green while checking nothing. Change the cadence anytime in the Jobs tab; re-saving there re-syncs the schtasks entry. `wr resync` and `wr reprocess --confirm` can be registered the same way if you want them on a schedule.

### Apps tab — admin PWA

From an **Apps-tab scan** for the bats, add two rows, both named *WhatsApp Radar*:

- `whatsapp-radar-webapp` (`kind: webapp` → `webapp.bat`) — serves the PWA on `:8455`.
- `whatsapp-radar-webapp-tunnel-named` (`kind: tunnel` → `webapp_tunnel_named.bat`) — the same PWA behind its persistent named Cloudflare URL (needs Step 6 wired first).

Tapping either from the phone spawns the matching bat. For daily use prefer this repo's own `tray.bat` (adopt-or-spawn behind a tray icon); the Apps-tab rows are for launching from the phone when the tray isn't already up.

Phone access itself (Tailscale TLS, optional Cloudflare named tunnel) is Steps 5–6 above; the rotation/expiry schedule for the cert and tokens is in **Recurring maintenance** below.

## 8 — Verify the system is live

```powershell
.\wr.bat status                                  # connector connected
.\wr.bat scan --dry-run                          # replay stored messages, no delivery, no cursor advance
# open the PWA on the phone → Dashboard shows channels/messages/scans; Execution shows a green health dot
```

Build confirmation: `GET /api/version` returns `{git_sha, built_at, asset_hash}` — after a restart the `git_sha` should match `HEAD`.

## Recurring maintenance

| What | When | How |
| --- | --- | --- |
| Tailscale TLS leaf cert | Before **2027-07-06** (leaf validity 395 days) | Re-run `scripts\gen_ssl_cert.py` (reuses the long-lived CA); re-anchor the reminder to +395 days |
| Bearer token / login password | On suspected compromise only | `scripts\gen_token.py` / `scripts\set_password.py` |
| Telegram bot token | On leak only | Re-issue via BotFather, update `config\webapp_config.json` (or `WR_TELEGRAM_*`) |
| Sidecar re-pair | Only after a phone-side logout | Delete `auth/`, re-run the sidecar, scan the QR (or re-pair from the Execution tab) |
| Baileys (sidecar) updates | Occasionally — it tracks an unofficial protocol | `cd sidecar && npm update`; the buffer contract isolates churn to `sidecar/index.js` |

See [`manual.md`](manual.md) for routine operation and troubleshooting, and [`linked-device.md`](linked-device.md) for the connector design and unofficial-library risk.
