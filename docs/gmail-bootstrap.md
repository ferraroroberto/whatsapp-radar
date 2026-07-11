# Gmail connector bootstrap

This runbook enables the read-only Gmail source added in issue #59. The runtime is a plain scheduled process: it talks directly to the Gmail API with an installed-app OAuth refresh token and does not depend on Claude, MCP, a browser session, or a Google password after bootstrap.

## Architecture and safety boundary

The source path is:

```text
Google Gmail API
  -> GmailConnector (gmail.readonly only)
  -> source-tagged chat/message records in local SQLite
  -> existing keyword + LLM analysis
  -> one consolidated digest with WhatsApp
```

A Gmail chat is one configured sender or Gmail label. A Gmail message is one email. Emails matching multiple whitelist entries have one owner: a sender match wins; otherwise the first matching label in configured order wins. Attachments are never downloaded. The connector exposes only label listing, message-id listing, and message retrieval; it has no send, draft, modify, archive, trash, or label-write method.

OAuth files and mail data stay under ignored local paths. Never commit `auth/gmail/`, `config/local.json`, real addresses, message samples, or token output.

## 1. Install dependencies

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. Create a Google Cloud project

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Use the project picker to create a project, for example `WhatsApp Radar Gmail`.
3. Open **APIs & Services → Library**.
4. Search for **Gmail API** and select **Enable**.

No Pub/Sub topic, service account, Workspace administrator delegation, or Gmail MCP server is needed.

## 3. Configure Google Auth Platform

1. Open **Google Auth Platform → Branding**.
2. Enter an app name such as `WhatsApp Radar`, a support email, and a developer contact email.
3. Open **Audience**:
   - For a personal Gmail account, choose **External** and keep the app in testing.
   - For a managed Workspace account where you control the organization, **Internal** may be available.
4. If the app is External/testing, add the Gmail account that will be read under **Test users**.
5. Open **Data Access** and add only:

   ```text
   https://www.googleapis.com/auth/gmail.readonly
   ```

`gmail.readonly` is a restricted scope. This private local utility does not need publication for other users, but Google may show an unverified/testing warning during personal bootstrap. Do not replace it with `https://mail.google.com/`; that broader IMAP scope permits write/delete capabilities the connector does not need.

An External app left in **Testing** receives refresh tokens that expire after **7 days** because Gmail is not a basic identity scope. That is useful for initial validation but unsuitable for a scheduled long-lived job. After validating the connector, move the OAuth app to **In production** (and complete any Google verification steps shown for the restricted scope), or use an Internal/administrator-trusted Workspace app where available. See Google's [refresh-token expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration).

Official references: [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes), [Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python).

## 4. Create the desktop OAuth client

1. Open **Google Auth Platform → Clients**.
2. Select **Create client**.
3. Choose **Desktop app**.
4. Name it, for example `WhatsApp Radar desktop`.
5. Download the JSON credentials.
6. Create the local auth directory and copy the downloaded file:

   ```powershell
   New-Item -ItemType Directory -Force auth\gmail
   Copy-Item "$HOME\Downloads\client_secret_*.json" auth\gmail\credentials.json
   ```

Confirm Git ignores it:

```powershell
git check-ignore auth\gmail\credentials.json
```

The expected output is the same path. Stop if it is not ignored.

## 5. Configure the whitelist

Create or update the ignored `config/local.json`. Keep the source order as shown so WhatsApp and Gmail participate in one scan:

```json
{
  "sources": ["whatsapp", "gmail"],
  "connector": "linked_device",
  "gmail": {
    "credentials_path": "auth/gmail/credentials.json",
    "token_path": "auth/gmail/token.json",
    "senders": [
      {
        "address": "school@example.com",
        "name": "School notices"
      }
    ],
    "labels": [
      {
        "name": "Family/Activities",
        "display_name": "Activity mail"
      }
    ]
  }
}
```

Replace the generic examples only in your ignored local file. Sender addresses are normalized to lowercase. Label names are resolved to Gmail's immutable label IDs at connection time; a configured label that does not exist makes Gmail report disconnected rather than broadening the query.

Whitelist order matters only when an email carries several configured labels: the first configured label owns it. Any whitelisted sender match takes precedence over every label.

The credential/token paths may instead be overridden with `WR_GMAIL_CREDENTIALS_PATH` and `WR_GMAIL_TOKEN_PATH`. The whitelist intentionally lives in `config/local.json`, where named entries remain readable and auditable.

## 6. Create the refresh token

Run the interactive bootstrap from the repository root:

```powershell
.\.venv\Scripts\python.exe -m scripts.auth_gmail
```

The script opens the system browser on a loopback OAuth callback. Sign in as the mailbox owner, review that the request is read-only, and approve it. Google redirects back to localhost and the script writes:

```text
auth/gmail/token.json
```

Confirm the token is ignored without displaying its contents:

```powershell
git check-ignore auth\gmail\token.json
Test-Path auth\gmail\token.json
```

Do not paste the token into `.env`, `config/local.json`, GitHub, logs, or support messages. The scheduled connector refreshes expired access tokens from this file automatically.

## 7. Verify the connector safely

Check source status:

```powershell
.\wr.bat status
```

Expected Gmail line:

```text
Source:     gmail / gmail (connected=True) — N whitelisted sender/label chat(s)
```

Ingest whitelisted mail:

```powershell
.\wr.bat ingest
.\wr.bat chats
```

Only configured sender/label chats should appear. Set the desired Gmail chats to monitored from the PWA or `wr monitor <id>`. Monitoring baselines the cursor, so existing mail is not alerted as new.

Run a dry analysis first:

```powershell
.\wr.bat scan --dry-run
```

Then run the real multi-source scan:

```powershell
.\wr.bat scan
```

WhatsApp and Gmail sync independently, then produce one consolidated digest. If Gmail authentication or quota fails, WhatsApp continues; Gmail's cursor does not advance and the command exits non-zero to make degraded coverage visible.

## Token lifecycle and revocation

Access tokens expire and are refreshed automatically from the persisted refresh token. An External/Testing refresh token expires after 7 days. In-production refresh tokens may still stop working if the user revokes the grant, changes the account password while Gmail scopes are present, leaves the token unused for six months, exceeds Google's live-token limit, or an administrator blocks the app.

To rotate or recover:

1. Remove the app grant from [Google Account third-party access](https://myaccount.google.com/connections).
2. Delete only `auth/gmail/token.json`.
3. Re-run `python -m scripts.auth_gmail`.

Never delete `data/` or rebuild the database merely to repair OAuth.

## Troubleshooting

### OAuth token missing

Run `python -m scripts.auth_gmail` interactively. Scheduled `wr scan` never launches a consent browser.

### Access blocked or app not verified

Confirm the mailbox is listed under **Audience → Test users**, the OAuth client type is **Desktop app**, and the consent request contains only `gmail.readonly`.

### Configured labels were not found

Match the Gmail label name exactly, including nesting such as `Family/Activities`. The connector fails closed; it never substitutes an all-mail query.

### Gmail is connected but no messages appear

Confirm the sender's exact `From` address rather than a display alias. Gmail API search does not perform every alias expansion the Gmail web UI performs. For label chats, confirm the label is applied to the individual messages.

### Quota or permission failure

The source reports disconnected and advances no Gmail cursor. Wait for quota recovery or repair the OAuth grant, then rerun the scan. Do not weaken the scope or bypass the multi-source preflight.
