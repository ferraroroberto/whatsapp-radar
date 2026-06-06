"""Deterministic fixture connector.

Reads sanitized generic chats/messages from a JSON file so the whole pipeline —
storage, cursoring, analysis, reporting — can be developed and tested with no
WhatsApp credentials and no network. Output is fully deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import ChatRecord, MessageRecord
from .base import ConnectorStatus

_DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_chats.json"


class FixtureConnector:
    """A :class:`MessageConnector` backed by a static JSON fixture file."""

    def __init__(self, fixture_path: Path | None = None) -> None:
        self._path = fixture_path or _DEFAULT_FIXTURE
        self._data: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        self._data = {chat["source_chat_id"]: chat for chat in raw["chats"]}
        self._loaded = True

    def connect(self) -> ConnectorStatus:
        self._load()
        return self.status()

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(
            name="fixture",
            connected=self._loaded,
            detail=f"{len(self._data)} fixture chats" if self._loaded else "not loaded",
        )

    def list_chats(self) -> list[ChatRecord]:
        self._load()
        return [
            ChatRecord(
                source_chat_id=chat["source_chat_id"],
                display_name=chat["display_name"],
                chat_type=chat.get("chat_type", "group"),
            )
            for chat in self._data.values()
        ]

    def fetch_messages(self, source_chat_id: str) -> list[MessageRecord]:
        self._load()
        chat = self._data.get(source_chat_id)
        if chat is None:
            return []
        messages = [
            MessageRecord(
                source_message_id=m["source_message_id"],
                message_timestamp=m["message_timestamp"],
                text=m.get("text"),
                sender_label=m.get("sender_label"),
                message_type=m.get("message_type", "text"),
                raw=m,
            )
            for m in chat.get("messages", [])
        ]
        messages.sort(key=lambda m: (m.message_timestamp, m.source_message_id))
        return messages

    def stop(self) -> None:
        self._data = {}
        self._loaded = False
