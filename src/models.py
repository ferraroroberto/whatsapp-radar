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
    # Which connector produced this chat. Connectors keep the default; a second
    # source (Gmail, #46) sets its own so identity is (source, source_chat_id).
    source: str = "whatsapp"


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
    # Voice-note transcription (#36): the sidecar sets these on a voice note —
    # ``transcription_status`` ('pending'|'failed'|…) and ``media_path`` (relative
    # path to the downloaded audio). Both stay ``None`` for typed messages and the
    # fixture, so the rest of the pipeline is unaffected.
    transcription_status: str | None = None
    media_path: str | None = None
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
    # Voice-note transcription state (#36); ``None`` for non-voice messages.
    transcription_status: str | None = None
    # Relative path to the downloaded audio while it awaits transcription; ``None``
    # once transcribed/skipped or when the download never succeeded. Distinguishes a
    # recoverable not-yet-transcribed note (audio on disk) from an unrecoverable one.
    media_path: str | None = None
    # Connector payload retained locally. The web API exposes only an explicit,
    # source-safe subset (for Gmail: subject + thread id), never this mapping.
    raw: dict[str, Any] = field(default_factory=dict)
