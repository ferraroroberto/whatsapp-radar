# Calendar + traffic bootstrap

This runbook provisions the two Google credentials the family checks need (issue #160):

1. A read-only **Google Calendar** installed-app OAuth refresh token (`calendar.readonly` only), mirroring [`gmail-bootstrap.md`](gmail-bootstrap.md).
2. A **Google Routes API** key for the traffic-jam check (API-key auth, a separate credential path from OAuth).

Both stay under ignored local paths. Never commit `auth/calendar/`, the Maps API key, real addresses, calendar ids, or token output.

## 1. Install dependencies

From the repository root (already present if Gmail was set up):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. Enable the APIs

In the [Google Cloud Console](https://console.cloud.google.com/) project, open **APIs & Services → Library** and enable:

- **Google Calendar API** (for the calendar read).
- **Routes API** (for the traffic check). This is the newer API; do not use the legacy Directions API.

## 3. Add the Calendar scope to the consent screen

Open **Google Auth Platform → Data Access** and add exactly:

```text
https://www.googleapis.com/auth/calendar.readonly
```

Do not add a broader read/write calendar scope — the checks never write. If the app is **External** and in **Testing**, add the Google account that owns the calendars under **Audience → Test users**.

> **7-day token note (same as Gmail):** an External/Testing app issues refresh tokens that expire after **7 days** — fine for validation, but move the OAuth app to **In production** before relying on the scheduled jobs, or the tokens will silently stop refreshing.

## 4. Desktop OAuth client

Reuse the **Desktop app** OAuth client already downloaded to `auth/calendar/credentials.json` (a Desktop client is not API-specific; the `installed` type is required for the loopback consent flow). Confirm it is ignored:

```powershell
git check-ignore auth\calendar\credentials.json
```

The expected output is the same path. Stop if it is not ignored.

## 5. Reading two calendars with one token

The token is authorized as **one** Google account and can read that account's own calendars plus any calendar **shared with it**. To cover both household calendars with a single token, the second person shares their calendar with the bootstrapping account at **"See all event details"** (Google Calendar → the calendar's *Settings and sharing → Share with specific people*). The shared calendar is then addressable by its id (the owner's email address).

If sharing is not possible, run the bootstrap a second time signed in as the other account, writing a second token path — but the default design is one token over two shared calendars.

## 6. Mint the refresh token (interactive)

Run once, interactively, from the repository root:

```powershell
.\.venv\Scripts\python.exe -m scripts.auth_calendar
```

It opens the system browser on a loopback callback. Sign in as the calendar owner, confirm the request is **read-only Calendar**, and approve. Google redirects back to localhost and the script writes:

```text
auth/calendar/token.json
```

Confirm the token is ignored without displaying its contents:

```powershell
git check-ignore auth\calendar\token.json
Test-Path auth\calendar\token.json
```

Never paste the token into config, docs, logs, or chat. The scheduled checks refresh access tokens from this file automatically and never launch a browser.

## 7. Validate the calendar read (non-interactive)

```powershell
.\.venv\Scripts\python.exe -m calendar_readonly.smoke --calendar you@example.com --days 3
```

Repeat `--calendar` for each household calendar id. The smoke prints only privacy-safe aggregates (a masked summary + event count + soonest date), never full titles.

## 8. Routes API key (traffic check)

Open **APIs & Services → Credentials → Create credentials → API key**. Restrict the key to the **Routes API** (Application restrictions may stay "None" for a server-side local job). Provide it to the checks via the ignored `config/local.json` (a `traffic` section, added with the main #160 build) or the `GOOGLE_MAPS_API_KEY` environment variable. Validate live:

```powershell
$env:GOOGLE_MAPS_API_KEY = "<key>"
.\.venv\Scripts\python.exe -m src.traffic.routes_client --origin "<home address>" --dest "<work address>"
```

Expected output is one line: normal vs. traffic minutes, the delay, and a `NORMAL`/`DELAY`/`SIGNIFICANT_DELAY` status.

## Rotation / recovery

1. Remove the app grant from [Google Account third-party access](https://myaccount.google.com/connections).
2. Delete only `auth/calendar/token.json` (never `data/`).
3. Re-run `python -m scripts.auth_calendar`.

Rotate the Routes API key from the Cloud Console Credentials page; update `config/local.json` / the env var.
