"""Shared data-transfer objects passed across boundaries.

These are intentionally plain dataclasses so the connector, storage, analysis,
and report layers share one vocabulary without depending on each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChatRecord:
    """A chat as reported by a connector (sanitized metadata only)."""

    source_chat_id: str
    display_name: str
    chat_type: str = "group"


@dataclass(frozen=True)
class MessageRecord:
    """A single message as reported by a connector.

    ``message_timestamp`` is an ISO-8601 string; ``(message_timestamp, id)`` is the
    ordering key used for cursoring. ``raw`` carries the connector's original payload
    for local-only storage.
    """

    source_message_id: str
    message_timestamp: str
    text: str | None = None
    sender_label: str | None = None
    message_type: str = "text"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredMessage:
    """A message row read back from storage (carries the internal id used as cursor)."""

    id: int
    chat_id: int
    source_message_id: str
    message_timestamp: str
    text: str | None
    sender_label: str | None
    message_type: str
    transcription_status: str = "none"
