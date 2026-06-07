"""The narrow, read-only connector interface.

This Protocol is the *entire* contract the rest of the system depends on. It is
deliberately read-only: connect, report status, list chats, fetch messages, and
stop. There is no method to send messages, react, mark-as-read, or administer
groups, so no connector implementation can introduce a write side effect through
this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from src.models import ChatRecord, MessageRecord


@dataclass(frozen=True)
class ConnectorStatus:
    name: str
    connected: bool
    detail: str = ""


@runtime_checkable
class MessageConnector(Protocol):
    """Read-only message source. Implementations must not perform writes."""

    def connect(self) -> ConnectorStatus:
        """Establish/confirm the session and return current status."""
        ...

    def status(self) -> ConnectorStatus:
        """Report current connection status without side effects."""
        ...

    def list_chats(self) -> list[ChatRecord]:
        """Return the chats this account can already see (sanitized metadata)."""
        ...

    def fetch_messages(self, source_chat_id: str) -> list[MessageRecord]:
        """Return available messages for a chat, oldest first.

        Cursoring is owned by storage, not the connector: the connector returns
        what it has and the store deduplicates idempotently.
        """
        ...

    def canonical_source_id(self, source_chat_id: str) -> str | None:
        """Normalize a stored ``source_chat_id`` to this connector's canonical key.

        Reprocess uses this to re-map operator state captured under an *older*
        reader's keying onto the chat the *current* reader produces. A connector
        with no notion of identity aliasing returns the id unchanged; None means
        the id can't be canonicalized (and so can't be re-mapped).
        """
        ...

    def stop(self) -> None:
        """Release any resources held by the connector."""
        ...
