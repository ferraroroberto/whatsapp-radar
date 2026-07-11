# Reusing the read-only Gmail component

The canonical reusable component is the root-level [`gmail_readonly/`](../gmail_readonly/) package. It has no imports from `src`, `app`, FastAPI, SQLite, the scan pipeline, or WhatsApp Radar models. Another Python application can copy it unchanged and decide independently how to store, monitor, classify, or display the returned records.

WhatsApp Radar continuously exercises the same component through the thin adapter in `src/connector/gmail.py`. Do not copy that adapter into another application; it exists only to map portable records into this repository's `ChatRecord`, `MessageRecord`, and `ConnectorStatus` types.

## Canonical files

Copy these files byte-for-byte:

```text
gmail_readonly/__init__.py
gmail_readonly/core.py
gmail_readonly/google_client.py
gmail_readonly/oauth.py
```

The portable offline contract is `tests/test_gmail_readonly.py`. Copy it into the consumer's test directory when adopting or upgrading the component.

The only Google dependencies are:

```text
google-api-python-client>=2.100
google-auth-httplib2>=0.2
google-auth-oauthlib>=1.2
```

The package uses only the Python standard library beyond those dependencies. It is source reuse, not a published SDK, so it needs no build step or package registry.

## Security contract

- OAuth requests exactly `https://www.googleapis.com/auth/gmail.readonly`.
- The public component exposes profile lookup, label listing, message-id search, metadata retrieval, full-message retrieval, and transport cleanup only.
- It exposes no send, draft, modify, archive, trash, delete, insert, or label-write operation. The portable contract test fails if one appears.
- Access-token refresh is automatic and persisted with an atomic replace.
- Credentials, tokens, Gmail queries, and mailbox content are never logged by the component.
- Safe API errors reveal a category such as authentication, permission, or quota failure without including the query or message.
- MIME normalization prefers `text/plain`, falls back to HTML text, and skips every part with a filename, so attachments are not decoded or downloaded.
- Selected headers and the Gmail thread ID are preserved for callers that need threading or audit evidence.

The caller remains responsible for keeping credential/token paths ignored, restricting filesystem access, choosing a bounded retention policy, and preventing normalized mail from reaching telemetry or shared logs.

## Bootstrap OAuth in any application

First complete the Google Cloud registration in [`gmail-bootstrap.md`](gmail-bootstrap.md): enable Gmail API, configure the consent screen, request only `gmail.readonly`, create a Desktop client, and download its JSON file.

From the consumer application's root, with the copied package importable:

```powershell
python -m gmail_readonly.oauth `
  --credentials auth\gmail\credentials.json `
  --token auth\gmail\token.json
```

The command accepts optional `--host`, `--port`, and `--no-browser` settings. Both paths are explicit; the reusable command does not load WhatsApp Radar config or assume a repository layout. It opens installed-app consent, requires a refresh token, and atomically writes the token to the requested path.

## Count and search example

```python
from pathlib import Path

from gmail_readonly import (
    GmailLabel,
    GmailMailbox,
    GmailSearch,
    GmailSender,
    build_google_read_client,
)

client = build_google_read_client(Path("auth/gmail/token.json"))
mailbox = GmailMailbox(client)

try:
    print(mailbox.profile().masked_email_address)
    sources = mailbox.resolve_sources(
        senders=(GmailSender("school@example.com", "School notices"),),
        labels=(GmailLabel("Family/Activities", "Activity mail"),),
        lookback_days=60,
    )
    for source in sources:
        print(source.display_name, mailbox.count(source.search))
        metadata = mailbox.metadata(source.search)
        messages = mailbox.messages(source.search)

    unread = GmailSearch(query="is:unread", lookback_days=30)
    print(mailbox.count(unread))
finally:
    mailbox.close()
```

`count()` retrieves message IDs only. `metadata()` requests selected headers without bodies. `messages()` retrieves and normalizes content. All modes follow Gmail pagination and deterministic `(timestamp, message_id)` ordering.

## Adopt and upgrade byte-for-byte

Record the source repository commit used by the consumer. Export only the canonical directory from that commit:

```powershell
git -C E:\automation\whatsapp-radar archive <commit-sha> gmail_readonly |
  tar -x -C <consumer-root>
```

Copy `tests/test_gmail_readonly.py` separately and run it in the consumer:

```powershell
python -m pytest tests\test_gmail_readonly.py
```

To prove the consumer has not drifted, compare it with the canonical checkout:

```powershell
git diff --no-index -- E:\automation\whatsapp-radar\gmail_readonly <consumer-root>\gmail_readonly
```

No output means the directories match byte-for-byte. Upgrade by exporting a newer reviewed commit, rerunning the portable contract, and updating the recorded source SHA. Application-specific adapters stay outside the copied directory.

If this component gains multiple fleet adopters, register the canonical files and their source SHA through `project-scaffolding`'s vendored-component convention before propagation. Do not maintain divergent hand-edited copies.
