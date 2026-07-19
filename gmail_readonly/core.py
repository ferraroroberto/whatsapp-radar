"""Framework-neutral Gmail whitelist, search, and message normalization."""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Protocol

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_SELECTED_HEADERS = (
    "message-id",
    "in-reply-to",
    "references",
    "from",
    "to",
    "subject",
    "date",
)


@dataclass(frozen=True)
class GmailSender:
    """One allowed sender and the name callers show for it."""

    address: str
    display_name: str


@dataclass(frozen=True)
class GmailLabel:
    """One allowed Gmail label and the name callers show for it."""

    name: str
    display_name: str


@dataclass(frozen=True)
class GmailSearch:
    """One read-only Gmail search with optional server-side constraints."""

    query: str = ""
    label_ids: tuple[str, ...] = ()
    lookback_days: int | None = None

    def api_query(self) -> str:
        """Return the Gmail query string after validating caller input."""
        query = self.query.strip()
        if any(character in query for character in ("\x00", "\r", "\n")):
            raise ValueError("Gmail query must be a single line without NUL bytes")
        if self.lookback_days is not None:
            if self.lookback_days < 1:
                raise ValueError("lookback_days must be at least 1")
            query = " ".join(part for part in (query, f"newer_than:{self.lookback_days}d") if part)
        return query or "in:anywhere"


@dataclass(frozen=True)
class GmailSource:
    """Resolved whitelist entry with a stable id and precedence-safe search."""

    source_id: str
    display_name: str
    search: GmailSearch


@dataclass(frozen=True)
class DiscoveredSender:
    """A distinct sender seen while scanning a recent time window (#166).

    Produced by :meth:`GmailMailbox.discover_senders` from message metadata only —
    never full bodies. ``address`` is lowercased for stable identity; ``display_name``
    is the friendliest ``From`` name seen (falling back to the address).
    """

    address: str
    display_name: str
    last_timestamp: str
    message_count: int


@dataclass(frozen=True)
class GmailProfile:
    """Safe mailbox identity and aggregate counts returned by Gmail."""

    email_address: str
    messages_total: int | None = None
    threads_total: int | None = None

    @property
    def masked_email_address(self) -> str:
        """Return an operator-friendly identity without exposing the full address."""
        return masked_email_address(self.email_address)


@dataclass(frozen=True)
class NormalizedEmail:
    """Provider-neutral email record produced from a Gmail API message."""

    message_id: str
    thread_id: str | None
    timestamp: str
    subject: str | None
    body_text: str | None
    sender_name: str | None
    sender_address: str | None
    headers: dict[str, str] = field(default_factory=dict)
    label_ids: tuple[str, ...] = ()

    @property
    def text(self) -> str | None:
        """Return the subject/body form used by text-classification callers."""
        parts = (
            f"Subject: {self.subject}" if self.subject else "",
            self.body_text or "",
        )
        value = "\n\n".join(part for part in parts if part)
        return value or None


class GmailReadError(RuntimeError):
    """A privacy-safe Gmail failure suitable for logs and status surfaces."""


class GmailReadClient(Protocol):
    """Minimal Gmail read surface; implementations must expose no writes."""

    def get_profile(self) -> dict[str, Any]: ...

    def list_labels(self) -> list[dict[str, Any]]: ...

    def list_message_ids(
        self,
        *,
        query: str,
        label_ids: list[str] | None = None,
    ) -> list[str]: ...

    def get_message(
        self,
        message_id: str,
        *,
        metadata_only: bool = False,
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


class GmailMailbox:
    """Reusable whitelist and search facade over a narrow Gmail client."""

    def __init__(self, client: GmailReadClient) -> None:
        self._client = client

    def profile(self) -> GmailProfile:
        """Return mailbox identity and aggregate counts."""
        try:
            raw = self._client.get_profile()
            return GmailProfile(
                email_address=str(raw.get("emailAddress") or ""),
                messages_total=_optional_int(raw.get("messagesTotal")),
                threads_total=_optional_int(raw.get("threadsTotal")),
            )
        except Exception as exc:
            raise GmailReadError(_safe_error_detail(exc)) from exc

    def resolve_sources(
        self,
        *,
        senders: tuple[GmailSender, ...] = (),
        labels: tuple[GmailLabel, ...] = (),
        lookback_days: int | None = None,
    ) -> tuple[GmailSource, ...]:
        """Validate and resolve whitelist entries with deterministic ownership."""
        sender_addresses = tuple(item.address.strip().lower() for item in senders)
        label_names = tuple(item.name.strip() for item in labels)
        if not senders and not labels:
            raise ValueError("whitelist is empty; configure at least one sender or label")
        if any(not address for address in sender_addresses):
            raise ValueError("sender whitelist contains an empty address")
        if len(set(sender_addresses)) != len(sender_addresses):
            raise ValueError("duplicate sender whitelist entry")
        if any(not name for name in label_names):
            raise ValueError("label whitelist contains an empty name")
        if len(set(label_names)) != len(label_names):
            raise ValueError("duplicate label whitelist entry")

        try:
            available = {
                str(label.get("name")): str(label.get("id"))
                for label in self._client.list_labels()
                if label.get("name") and label.get("id")
            }
        except Exception as exc:
            raise GmailReadError(_safe_error_detail(exc)) from exc
        missing = [name for name in label_names if name not in available]
        if missing:
            raise ValueError(f"{len(missing)} configured Gmail label(s) were not found")

        sources = [
            GmailSource(
                source_id=f"sender:{address}",
                display_name=sender.display_name,
                search=GmailSearch(
                    query=f"from:{_escape_query(address)}",
                    lookback_days=lookback_days,
                ),
            )
            for sender, address in zip(senders, sender_addresses, strict=True)
        ]
        for index, label in enumerate(labels):
            exclusions = [
                *(f"-from:{_escape_query(address)}" for address in sender_addresses),
                *(f'-label:"{_escape_query(name)}"' for name in label_names[:index]),
            ]
            label_id = available[label_names[index]]
            sources.append(
                GmailSource(
                    source_id=f"label:{label_id}",
                    display_name=label.display_name,
                    search=GmailSearch(
                        query=" ".join(exclusions),
                        label_ids=(label_id,),
                        lookback_days=lookback_days,
                    ),
                )
            )
        return tuple(sources)

    def discover_senders(
        self, *, days: int, limit: int
    ) -> tuple[DiscoveredSender, ...]:
        """Distinct senders active in the last ``days``, from metadata only (#166).

        Scans at most ``limit`` recent messages (the mailbox is huge — this hard cap
        bounds the metadata reads), groups them by lowercased ``From`` address, and
        returns one :class:`DiscoveredSender` per address with its friendliest name,
        latest send time, and how many of the scanned messages it accounts for.
        Sorted most-recent first. Never downloads message bodies or attachments.
        """
        if days < 1:
            raise ValueError("days must be at least 1")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        metadata = self.metadata(GmailSearch(lookback_days=days), limit=limit)
        by_address: dict[str, DiscoveredSender] = {}
        for email in metadata:
            address = (email.sender_address or "").strip().lower()
            if not address:
                continue
            name = (email.sender_name or "").strip() or address
            existing = by_address.get(address)
            if existing is None:
                by_address[address] = DiscoveredSender(
                    address=address,
                    display_name=name,
                    last_timestamp=email.timestamp,
                    message_count=1,
                )
            else:
                # Keep the latest timestamp and a non-address display name if we find one.
                display_name = (
                    name if existing.display_name == address and name != address
                    else existing.display_name
                )
                last_timestamp = max(existing.last_timestamp, email.timestamp)
                by_address[address] = DiscoveredSender(
                    address=address,
                    display_name=display_name,
                    last_timestamp=last_timestamp,
                    message_count=existing.message_count + 1,
                )
        return tuple(
            sorted(
                by_address.values(),
                key=lambda sender: (sender.last_timestamp, sender.address),
                reverse=True,
            )
        )

    def count(self, search: GmailSearch) -> int:
        """Count matching messages without retrieving metadata or content."""
        return len(self._message_ids(search))

    def metadata(
        self, search: GmailSearch, *, limit: int | None = None
    ) -> list[NormalizedEmail]:
        """Retrieve only selected headers and identifiers for matching messages."""
        return self._retrieve(search, metadata_only=True, limit=limit)

    def messages(
        self, search: GmailSearch, *, limit: int | None = None
    ) -> list[NormalizedEmail]:
        """Retrieve and normalize matching messages, sorted oldest first."""
        return self._retrieve(search, metadata_only=False, limit=limit)

    def close(self) -> None:
        """Release the underlying HTTP transport."""
        self._client.close()

    def _message_ids(self, search: GmailSearch) -> list[str]:
        try:
            return self._client.list_message_ids(
                query=search.api_query(),
                label_ids=list(search.label_ids) or None,
            )
        except Exception as exc:
            raise GmailReadError(_safe_error_detail(exc)) from exc

    def _retrieve(
        self,
        search: GmailSearch,
        *,
        metadata_only: bool,
        limit: int | None,
    ) -> list[NormalizedEmail]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        try:
            message_ids = self._message_ids(search)
            if limit is not None:
                message_ids = message_ids[:limit]
            messages = [
                normalize_message(
                    self._client.get_message(message_id, metadata_only=metadata_only)
                )
                for message_id in message_ids
            ]
        except GmailReadError:
            raise
        except Exception as exc:
            raise GmailReadError(_safe_error_detail(exc)) from exc
        messages.sort(key=lambda message: (message.timestamp, message.message_id))
        return messages


def normalize_message(raw: dict[str, Any]) -> NormalizedEmail:
    """Normalize one Gmail API message without downloading attachments."""
    message_id = str(raw.get("id") or "")
    if not message_id:
        raise ValueError("Gmail message has no id")
    payload = raw.get("payload") or {}
    headers = {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in payload.get("headers") or []
        if item.get("name")
    }
    sender_name, sender_address = parseaddr(headers.get("from", ""))
    selected_headers = {key: headers[key] for key in _SELECTED_HEADERS if headers.get(key)}
    return NormalizedEmail(
        message_id=message_id,
        thread_id=str(raw["threadId"]) if raw.get("threadId") else None,
        timestamp=_message_timestamp(raw, headers),
        subject=headers.get("subject", "").strip() or None,
        body_text=_message_body(payload) or None,
        sender_name=sender_name or None,
        sender_address=sender_address or None,
        headers=selected_headers,
        label_ids=tuple(str(item) for item in raw.get("labelIds") or []),
    )


def masked_email_address(address: str) -> str:
    """Mask a mailbox address while leaving enough identity for an operator."""
    local, separator, domain = address.strip().partition("@")
    if not separator:
        return "***"
    visible = local[:1]
    return f"{visible}***@{domain}" if domain else "***"


def _safe_error_detail(exc: Exception) -> str:
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


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


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
