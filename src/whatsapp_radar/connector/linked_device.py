"""DEFERRED: real WhatsApp Web linked-device connector.

This is intentionally a documented stub. The linked-device connector is the
highest-risk dependency of the spike (an unofficial integration path) and is
explicitly out of scope for the foundation PR. It is captured here so the
boundary and its hard constraints are recorded for the follow-up issue.

Implementation constraints when this is built:

- READ-ONLY ONLY. Implement exactly the :class:`MessageConnector` Protocol —
  ``connect`` / ``status`` / ``list_chats`` / ``fetch_messages`` / ``stop``.
  Do NOT add send, reply, reaction, read-receipt, broadcast, group-admin, or
  contact-scraping methods. The read-only guarantee lives in the boundary.
- LOCAL-ONLY STORAGE. Linked-device credentials/session state must be written
  only under ignored paths (``auth/``, ``sessions/``). Never under version
  control; run ``git status --ignored`` before committing.
- The likely runtime is a Node (Baileys) or Go (whatsmeow) sidecar process that
  this class drives over a local IPC/JSON boundary, keeping the Python core free
  of the unofficial library. Document the library risk in the README/docs before
  implementing (see CLAUDE.md "WhatsApp Integration Guardrails").
"""

from __future__ import annotations

from ..models import ChatRecord, MessageRecord
from .base import ConnectorStatus

_NOT_IMPLEMENTED = (
    "The linked-device connector is deferred. Use the fixture connector for the "
    "spike, or implement this behind the read-only MessageConnector Protocol per "
    "the constraints documented in this module."
)


class LinkedDeviceConnector:
    """Placeholder for the deferred linked-device connector (raises on use)."""

    def connect(self) -> ConnectorStatus:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(name="linked_device", connected=False, detail="deferred")

    def list_chats(self) -> list[ChatRecord]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def fetch_messages(self, source_chat_id: str) -> list[MessageRecord]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def stop(self) -> None:
        return None
