"""Read-only, whitelist-only Gmail API connector.

The production adapter exposes only the three Gmail reads this connector needs:
list labels, list matching message ids, and retrieve one message. OAuth uses the
restricted-but-read-only gmail.readonly scope; no send, draft, modify, archive,
trash, or label-write method exists on either public class.
"""

from __future__ import annotations

import base64
import html
import re
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

from src.config import GmailConfig
from src.connector.base import ConnectorStatus
from src.connector.preflight import ConnectorOffline
from src.models import ChatRecord, MessageRecord

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GmailReadClient(Protocol):
    """Minimal read surface, injected in offline tests."""

    def list_labels(self) -> list[dict[str, Any]]: ...

    def list_message_ids(
        self,
        *,
        query: str,
        label_ids: list[str] | None = None,
    ) -> list[str]: ...

    def get_message(self, message_id: str) -> dict[str, Any]: ...

    def close(self) -> None: ...


class GoogleGmailReadClient:
    """Narrow adapter over the official Gmail discovery client."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def list_labels(self) -> list[dict[str, Any]]:
        response = self._service.users().labels().list(userId="me").execute()
        return list(response.get("labels") or [])

    def list_message_ids(
        self,
        *,
        query: str,
        label_ids: list[str] | None = None,
    ) -> list[str]:
        message_ids: list[str] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "userId": "me",
                "q": query,
                "maxResults": 500,
                "includeSpamTrash": False,
            }
            if label_ids:
                kwargs["labelIds"] = label_ids
            if page_token:
                kwargs["pageToken"] = page_token
            response = self._service.users().messages().list(**kwargs).execute()
            message_ids.extend(
                str(message["id"])
                for message in response.get("messages") or []
                if message.get("id")
            )
            page_token = response.get("nextPageToken")
            if not page_token:
                return message_ids

    def get_message(self, message_id: str) -> dict[str, Any]:
        response: dict[str, Any] = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        return response

    def close(self) -> None:
        http = getattr(self._service, "_http", None)
        close = getattr(http, "close", None)
        if callable(close):
            close()


def build_gmail_read_client(config: GmailConfig) -> GmailReadClient:
    """Load/refresh the persisted OAuth token and build the official API client."""
    if not config.token_path.is_file():
        raise FileNotFoundError(
            "Gmail OAuth token missing; run python -m scripts.auth_gmail interactively"
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
        str(config.token_path),
        [GMAIL_READONLY_SCOPE],
    )
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        write_gmail_token(config.token_path, credentials.to_json())
    if not credentials.valid:
        raise RuntimeError("Gmail OAuth token is invalid or has been revoked")
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return GoogleGmailReadClient(service)


def write_gmail_token(path: Path, token_json: str) -> None:
    """Persist a refreshed token atomically under the ignored auth directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(token_json, encoding="utf-8")
    tmp.replace(path)


class GmailConnector:
    """Normalize whitelisted Gmail senders/labels into MessageConnector records."""

    def __init__(
        self,
        config: GmailConfig,
        *,
        client: GmailReadClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._label_ids: dict[str, str] = {}
        self._status = ConnectorStatus("gmail", False, "not connected")

    def connect(self) -> ConnectorStatus:
        if not self._config.senders and not self._config.labels:
            self._status = ConnectorStatus(
                "gmail",
                False,
                "whitelist is empty; configure at least one sender or label",
            )
            return self._status
        if len({item.address for item in self._config.senders}) != len(
            self._config.senders
        ):
            self._status = ConnectorStatus("gmail", False, "duplicate sender whitelist entry")
            return self._status
        if len({item.name for item in self._config.labels}) != len(self._config.labels):
            self._status = ConnectorStatus("gmail", False, "duplicate label whitelist entry")
            return self._status

        try:
            if self._client is None:
                self._client = build_gmail_read_client(self._config)
            available = {
                str(label.get("name")): str(label.get("id"))
                for label in self._client.list_labels()
                if label.get("name") and label.get("id")
            }
            missing = [item.name for item in self._config.labels if item.name not in available]
            if missing:
                self._status = ConnectorStatus(
                    "gmail",
                    False,
                    f"{len(missing)} configured Gmail label(s) were not found",
                )
                return self._status
            self._label_ids = {item.name: available[item.name] for item in self._config.labels}
            count = len(self._config.senders) + len(self._config.labels)
            self._status = ConnectorStatus(
                "gmail",
                True,
                f"{count} whitelisted sender/label chat(s)",
            )
        except Exception as exc:
            self._status = ConnectorStatus("gmail", False, _safe_error_detail(exc))
        return self._status

    def status(self) -> ConnectorStatus:
        return self._status

    def list_chats(self) -> list[ChatRecord]:
        self._require_connected()
        chats = [
            ChatRecord(
                source_chat_id=f"sender:{sender.address}",
                display_name=sender.name,
                chat_type="email",
                source="gmail",
            )
            for sender in self._config.senders
        ]
        chats.extend(
            ChatRecord(
                source_chat_id=f"label:{self._label_ids[label.name]}",
                display_name=label.display_name,
                chat_type="email",
                source="gmail",
            )
            for label in self._config.labels
        )
        return chats

    def fetch_messages(self, source_chat_id: str) -> list[MessageRecord]:
        client = self._require_connected()
        try:
            query, label_ids = self._query_for_chat(source_chat_id)
            messages = [
                self._normalize_message(client.get_message(message_id))
                for message_id in client.list_message_ids(
                    query=query,
                    label_ids=label_ids,
                )
            ]
        except ConnectorOffline:
            raise
        except Exception as exc:
            self._status = ConnectorStatus("gmail", False, _safe_error_detail(exc))
            raise ConnectorOffline(self._status) from exc
        messages.sort(key=lambda message: (message.message_timestamp, message.source_message_id))
        return messages

    def canonical_source_id(self, source_chat_id: str) -> str | None:
        return source_chat_id

    def stop(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None
        self._label_ids = {}
        self._status = ConnectorStatus("gmail", False, "stopped")

    def _require_connected(self) -> GmailReadClient:
        if not self._status.connected or self._client is None:
            raise ConnectorOffline(self._status)
        return self._client

    def _query_for_chat(self, source_chat_id: str) -> tuple[str, list[str] | None]:
        if source_chat_id.startswith("sender:"):
            address = source_chat_id.removeprefix("sender:")
            if address not in {item.address for item in self._config.senders}:
                raise ValueError("sender chat is not whitelisted")
            return f"from:{address}", None

        if source_chat_id.startswith("label:"):
            label_id = source_chat_id.removeprefix("label:")
            configured = next(
                (
                    (index, label)
                    for index, label in enumerate(self._config.labels)
                    if self._label_ids.get(label.name) == label_id
                ),
                None,
            )
            if configured is None:
                raise ValueError("label chat is not whitelisted")
            index, _label = configured
            exclusions = [
                *(f"-from:{sender.address}" for sender in self._config.senders),
                *(
                    f'-label:"{_escape_query(label.name)}"'
                    for label in self._config.labels[:index]
                ),
            ]
            return " ".join(exclusions) or "in:anywhere", [label_id]

        raise ValueError("unknown Gmail source chat id")

    def _normalize_message(self, raw: dict[str, Any]) -> MessageRecord:
        message_id = str(raw.get("id") or "")
        if not message_id:
            raise ValueError("Gmail message has no id")
        payload = raw.get("payload") or {}
        headers = {
            str(item.get("name", "")).lower(): str(item.get("value", ""))
            for item in payload.get("headers") or []
            if item.get("name")
        }
        subject = headers.get("subject", "").strip()
        body = _message_body(payload)
        text = "\n\n".join(
            part
            for part in (
                f"Subject: {subject}" if subject else "",
                body,
            )
            if part
        )
        sender_name, sender_address = parseaddr(headers.get("from", ""))
        selected_headers = {
            key: headers[key]
            for key in (
                "message-id",
                "in-reply-to",
                "references",
                "from",
                "to",
                "subject",
                "date",
            )
            if headers.get(key)
        }
        return MessageRecord(
            source_message_id=message_id,
            message_timestamp=_message_timestamp(raw, headers),
            text=text or None,
            sender_label=sender_name or sender_address or None,
            message_type="email",
            raw={
                "thread_id": raw.get("threadId"),
                "label_ids": list(raw.get("labelIds") or []),
                "headers": selected_headers,
            },
        )


def _safe_error_detail(exc: Exception) -> str:
    """Return a diagnostic category without leaking queries or mail content."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 401:
        return "OAuth token is invalid or expired"
    if status == 403:
        return "Gmail API permission or quota denied"
    if status == 429:
        return "Gmail API quota exceeded"
    if isinstance(exc, FileNotFoundError):
        return str(exc)
    return f"Gmail API request failed ({type(exc).__name__})"


def _escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _decode_body(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _message_body(payload: dict[str, Any]) -> str:
    plain: list[str] = []
    rich: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        if part.get("filename"):
            return
        mime_type = str(part.get("mimeType") or "").lower()
        data = str((part.get("body") or {}).get("data") or "")
        if mime_type == "text/plain" and data:
            plain.append(_decode_body(data).strip())
        elif mime_type == "text/html" and data:
            rich.append(_html_to_text(_decode_body(data)).strip())
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return "\n\n".join(part for part in (plain or rich) if part).strip()


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)


def _html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(html.unescape(value))
    return re.sub(r"\s+([.,;:!?])", r"\1", " ".join(parser.parts))


def _message_timestamp(raw: dict[str, Any], headers: dict[str, str]) -> str:
    internal_date = raw.get("internalDate")
    if internal_date is not None:
        return datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC).isoformat()
    date_header = headers.get("date")
    if date_header:
        parsed = parsedate_to_datetime(date_header)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    raise ValueError("Gmail message has no timestamp")
