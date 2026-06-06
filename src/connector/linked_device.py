"""Real WhatsApp Web linked-device connector (read-only reader half).

The unofficial WhatsApp protocol is spoken entirely by the Node Baileys sidecar
(``sidecar/``), which keeps a linked device paired and appends everything it sees
to a local NDJSON buffer. This class is a pure *reader* of that buffer: it never
talks to WhatsApp and exposes no write surface, so the read-only guarantee of the
:class:`MessageConnector` boundary holds by construction.

Buffer layout (all under an ignored path, written by the sidecar):

- ``chats.ndjson``    — one JSON line per chat upsert ``{jid, name, type, ts}``
- ``messages.ndjson`` — one JSON line per message ``{jid, msg_id, ts, sender, text, type, raw}``
- ``status.json``     — heartbeat ``{paired, connected, last_update, chats, messages}``

Both NDJSON files are append-only; duplicates are expected (history sync + live
events overlap, edits re-emit). The reader applies last-write-wins per key, and
storage deduplicates again idempotently downstream.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.connector.base import ConnectorStatus
from src.models import ChatRecord, MessageRecord

# A status.json older than this is treated as a dead sidecar (heartbeat is 30s).
_STALE_AFTER_SECONDS = 120


class LinkedDeviceConnector:
    """A read-only :class:`MessageConnector` backed by the sidecar's NDJSON buffer."""

    def __init__(self, buffer_dir: Path) -> None:
        self._dir = buffer_dir
        self._chats_file = buffer_dir / "chats.ndjson"
        self._messages_file = buffer_dir / "messages.ndjson"
        self._status_file = buffer_dir / "status.json"
        # Lazily-built {jid: {msg_id: row}} index so the (potentially large)
        # messages file is parsed once per connector, not once per chat.
        self._index: dict[str, dict[str, dict[str, Any]]] | None = None

    # --- connection status -------------------------------------------------

    def connect(self) -> ConnectorStatus:
        return self.status()

    def status(self) -> ConnectorStatus:
        raw = self._read_status()
        if raw is None:
            return ConnectorStatus(
                name="linked_device",
                connected=False,
                detail="sidecar not started (no status.json) — run the Node sidecar",
            )
        fresh = self._is_fresh(raw.get("last_update"))
        connected = bool(raw.get("connected")) and bool(raw.get("paired")) and fresh
        if not raw.get("paired"):
            detail = "not paired — scan the QR printed by the sidecar"
        elif not fresh:
            detail = "sidecar heartbeat stale — process may have stopped"
        else:
            detail = f"{raw.get('chats', 0)} chats, {raw.get('messages', 0)} messages buffered"
        return ConnectorStatus(name="linked_device", connected=connected, detail=detail)

    # --- read surface ------------------------------------------------------

    def list_chats(self) -> list[ChatRecord]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self._read_ndjson(self._chats_file):
            jid = row.get("jid")
            if not jid:
                continue
            # Keep a previously-seen name if a later event omitted it.
            if jid in latest and not row.get("name"):
                row["name"] = latest[jid].get("name")
            latest[jid] = row
        return [
            ChatRecord(
                source_chat_id=jid,
                display_name=row.get("name") or jid,
                chat_type=row.get("type", "group"),
            )
            for jid, row in latest.items()
        ]

    def fetch_messages(self, source_chat_id: str) -> list[MessageRecord]:
        latest = self._message_index().get(source_chat_id, {})
        messages = [
            MessageRecord(
                source_message_id=row["msg_id"],
                message_timestamp=row.get("ts", ""),
                text=row.get("text"),
                sender_label=row.get("sender"),
                message_type=row.get("type", "text"),
                raw=row,
            )
            for row in latest.values()
        ]
        messages.sort(key=lambda m: (m.message_timestamp, m.source_message_id))
        return messages

    def stop(self) -> None:
        # The reader does not own the sidecar's lifecycle, so there is nothing to
        # release. The sidecar is started and stopped as its own process.
        return None

    def _message_index(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Parse messages.ndjson once into {jid: {msg_id: row}} (last write wins)."""
        if self._index is None:
            index: dict[str, dict[str, dict[str, Any]]] = {}
            for row in self._read_ndjson(self._messages_file):
                jid = row.get("jid")
                msg_id = row.get("msg_id")
                if jid and msg_id:
                    index.setdefault(jid, {})[msg_id] = row
            self._index = index
        return self._index

    # --- internals ---------------------------------------------------------

    def _read_status(self) -> dict[str, Any] | None:
        if not self._status_file.exists():
            return None
        try:
            data: dict[str, Any] = json.loads(self._status_file.read_text(encoding="utf-8"))
            return data
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _is_fresh(last_update: Any) -> bool:
        if not isinstance(last_update, str):
            return False
        try:
            seen = datetime.fromisoformat(last_update.replace("Z", "+00:00"))
        except ValueError:
            return False
        age = (datetime.now(UTC) - seen).total_seconds()
        return age <= _STALE_AFTER_SECONDS

    @staticmethod
    def _read_ndjson(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn final line during a concurrent append: skip it.
                continue
        return rows
