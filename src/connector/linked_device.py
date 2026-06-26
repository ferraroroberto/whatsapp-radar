"""Real WhatsApp Web linked-device connector (read-only reader half).

The unofficial WhatsApp protocol is spoken entirely by the Node Baileys sidecar
(``sidecar/``), which keeps a linked device paired and appends everything it sees
to a local NDJSON buffer. This class is a pure *reader* of that buffer: it never
talks to WhatsApp and exposes no write surface, so the read-only guarantee of the
:class:`MessageConnector` boundary holds by construction.

Buffer layout (all under an ignored path, written by the sidecar):

- ``chats.ndjson``    — one JSON line per chat upsert ``{jid, name, type, ts}``;
  the sidecar also emits *alias* rows ``{jid, alias_for, ts}`` that map WhatsApp's
  hidden ``@lid`` address for a contact onto its phone JID (see below)
- ``messages.ndjson`` — one JSON line per message ``{jid, msg_id, ts, sender, text, type, raw}``
- ``status.json``     — heartbeat ``{paired, connected, last_update, chats, messages}``

Both NDJSON files are append-only; duplicates are expected (history sync + live
events overlap, edits re-emit). The reader applies last-write-wins per key, and
storage deduplicates again idempotently downstream.

WhatsApp identifies the same contact under several JID forms — a phone JID
(``<number>@s.whatsapp.net``), a legacy business form (``@c.us``), a device-scoped
form (``<number>:<device>@…``), and an opaque privacy form (``<id>@lid``). Messages
and chat-metadata events can arrive under *different* forms for one identity, which
would otherwise strand a chat with the wrong (raw-JID) name or zero associated
messages. To keep one row per identity the reader (a) normalizes every JID to a
canonical form (lower-cased, device/agent suffix dropped, ``@c.us`` → ``@s.whatsapp.net``)
and (b) folds ``alias_for`` rows so an ``@lid`` JID collapses onto its phone JID
before chats and messages are keyed. When no human name can be resolved it falls
back to a readable form (``+<number>``) rather than surfacing the raw JID suffix.
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


def read_status_file(buffer_dir: Path) -> dict[str, Any] | None:
    """Parse the sidecar's ``status.json`` heartbeat, or ``None`` if absent/torn.

    The single reader of the heartbeat schema, shared by the connector and the
    sidecar supervisor (:mod:`src.connector.sidecar`) so there is one owner.
    """
    status_file = buffer_dir / "status.json"
    if not status_file.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(status_file.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError):
        return None


def is_heartbeat_fresh(last_update: Any) -> bool:
    """True when ``last_update`` is within :data:`_STALE_AFTER_SECONDS` of now.

    The single definition of "stale" across the codebase (the connector's status
    view and the sidecar supervisor both call it)."""
    if not isinstance(last_update, str):
        return False
    try:
        seen = datetime.fromisoformat(last_update.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = (datetime.now(UTC) - seen).total_seconds()
    return age <= _STALE_AFTER_SECONDS


class LinkedDeviceConnector:
    """A read-only :class:`MessageConnector` backed by the sidecar's NDJSON buffer."""

    def __init__(self, buffer_dir: Path) -> None:
        self._dir = buffer_dir
        self._chats_file = buffer_dir / "chats.ndjson"
        self._messages_file = buffer_dir / "messages.ndjson"
        # Lazily-built {jid: {msg_id: row}} index so the (potentially large)
        # messages file is parsed once per connector, not once per chat.
        self._index: dict[str, dict[str, dict[str, Any]]] | None = None
        # Lazily-built {alias_jid: canonical_jid} map from the sidecar's alias rows.
        self._aliases: dict[str, str] | None = None

    # --- connection status -------------------------------------------------

    def connect(self) -> ConnectorStatus:
        return self.status()

    def status(self) -> ConnectorStatus:
        raw = read_status_file(self._dir)
        if raw is None:
            return ConnectorStatus(
                name="linked_device",
                connected=False,
                detail="sidecar not started (no status.json) — run the Node sidecar",
            )
        fresh = is_heartbeat_fresh(raw.get("last_update"))
        connected = bool(raw.get("connected")) and bool(raw.get("paired")) and fresh
        if not raw.get("paired"):
            detail = "not paired — scan the QR printed by the sidecar"
        elif not fresh:
            detail = "sidecar heartbeat stale — process may have stopped"
        else:
            # The sidecar's counters are per-session, not buffer totals — don't
            # present them as "buffered" (stored totals are surfaced from the DB).
            detail = "connected — receiving live updates"
        return ConnectorStatus(name="linked_device", connected=connected, detail=detail)

    # --- read surface ------------------------------------------------------

    def list_chats(self) -> list[ChatRecord]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self._read_ndjson(self._chats_file):
            if row.get("alias_for"):
                continue  # alias rows carry no chat metadata — folded in _alias_map
            jid = self._canonical_jid(row.get("jid"))
            if not jid:
                continue
            # Keep a previously-seen name if a later event omitted it (last write
            # wins on a real name, but a later nameless event never clears one).
            name = row.get("name") or (latest[jid].get("name") if jid in latest else None)
            latest[jid] = {"name": name, "type": row.get("type")}
        index = self._message_index()
        return [
            ChatRecord(
                source_chat_id=jid,
                display_name=row.get("name") or self._derive_name(jid, index.get(jid, {})),
                chat_type=row.get("type") or self._chat_type(jid),
            )
            for jid, row in latest.items()
        ]

    def fetch_messages(self, source_chat_id: str) -> list[MessageRecord]:
        key = self._canonical_jid(source_chat_id)
        latest = self._message_index().get(key, {}) if key else {}
        messages = [
            MessageRecord(
                source_message_id=row["msg_id"],
                message_timestamp=row.get("ts", ""),
                text=row.get("text"),
                sender_label=self._sender_label(row),
                message_type=row.get("type", "text"),
                # Voice-note transcription (#36): the sidecar tags voice notes with
                # a download status and a relative path to the audio it fetched.
                transcription_status=row.get("transcription_status"),
                media_path=row.get("media_path"),
                raw=row,
            )
            for row in latest.values()
        ]
        messages.sort(key=lambda m: (m.message_timestamp, m.source_message_id))
        return messages

    def canonical_source_id(self, source_chat_id: str) -> str | None:
        """Public canonicalization for reprocess: normalize + fold alias rows.

        Maps a stored ``source_chat_id`` (possibly keyed by an older reader) onto
        the identity key the current reader uses, so operator state survives a
        rebuild even when the keying rules changed (e.g. ``@lid`` folding).
        """
        return self._canonical_jid(source_chat_id)

    def stop(self) -> None:
        # The reader does not own the sidecar's lifecycle, so there is nothing to
        # release. The sidecar is started and stopped as its own process.
        return None

    def _message_index(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Parse messages.ndjson once into {canonical_jid: {msg_id: row}} (last write wins).

        Keys are canonicalized (and alias-folded) so messages that arrive under a
        JID variant of a chat associate to that chat rather than vanishing.
        """
        if self._index is None:
            index: dict[str, dict[str, dict[str, Any]]] = {}
            for row in self._read_ndjson(self._messages_file):
                jid = self._canonical_jid(row.get("jid"))
                msg_id = row.get("msg_id")
                if jid and msg_id:
                    index.setdefault(jid, {})[msg_id] = row
            self._index = index
        return self._index

    # --- JID canonicalization & name resolution ----------------------------

    @staticmethod
    def _normalize_jid(jid: Any) -> str | None:
        """Canonicalize a single JID: lower-case, drop device/agent suffix, c.us→net.

        Mirrors Baileys' ``jidNormalizedUser`` so the Python reader keys identities
        the same way the sidecar does. Does not resolve ``@lid`` to a phone JID —
        that mapping is only known to the sidecar and arrives as an alias row.
        """
        if not isinstance(jid, str):
            return None
        text = jid.strip().lower()
        if not text:
            return None
        at = text.find("@")
        if at < 0:
            return text
        user, server = text[:at], text[at + 1 :]
        # The user part may carry an agent ("_N") and/or device (":N") suffix.
        user = user.split(":", 1)[0].split("_", 1)[0]
        if server == "c.us":
            server = "s.whatsapp.net"
        return f"{user}@{server}"

    def _alias_map(self) -> dict[str, str]:
        """Parse the sidecar's ``alias_for`` rows into {alias_jid: canonical_jid}."""
        if self._aliases is None:
            aliases: dict[str, str] = {}
            for row in self._read_ndjson(self._chats_file):
                target = row.get("alias_for")
                if not target:
                    continue
                src = self._normalize_jid(row.get("jid"))
                dst = self._normalize_jid(target)
                if src and dst and src != dst:
                    aliases[src] = dst
            self._aliases = aliases
        return self._aliases

    def _canonical_jid(self, jid: Any) -> str | None:
        """Normalize then fold through the alias map to one identity key."""
        norm = self._normalize_jid(jid)
        if norm is None:
            return None
        aliases = self._alias_map()
        seen: set[str] = set()
        while norm in aliases and norm not in seen:
            seen.add(norm)
            norm = aliases[norm]
        return norm

    @staticmethod
    def _chat_type(jid: str) -> str:
        return "group" if jid.endswith("@g.us") else "dm"

    def _derive_name(self, jid: str, messages: dict[str, dict[str, Any]]) -> str:
        """Best readable name for a chat the sidecar never labelled.

        For a 1:1 chat the remote's most recent push name *is* the contact name, so
        use it before falling back to a formatted phone number. Group chats always
        receive a subject from the sidecar, so they only reach the humanized form
        when genuinely unlabelled.
        """
        if self._chat_type(jid) == "dm":
            for row in sorted(
                messages.values(), key=lambda r: r.get("ts", ""), reverse=True
            ):
                sender = row.get("sender")
                if sender and sender != "me" and not (row.get("raw") or {}).get("from_me"):
                    return str(sender)
        return self._humanize_jid(jid)

    @classmethod
    def _humanize_jid(cls, jid: Any) -> str:
        """A readable label for a JID — ``+<number>`` for phone JIDs, else the bare
        user part — so the raw ``@domain`` suffix never reaches the UI."""
        norm = cls._normalize_jid(jid) or (jid if isinstance(jid, str) else "")
        at = norm.find("@")
        user = norm[:at] if at >= 0 else norm
        server = norm[at + 1 :] if at >= 0 else ""
        if server == "s.whatsapp.net" and user.isdigit():
            return f"+{user}"
        return user or norm

    def _sender_label(self, row: dict[str, Any]) -> str | None:
        """Resolve a message's sender, falling back to the participant JID.

        History-synced messages often lack a push name, leaving the UI to render a
        blank sender. For group messages the participant JID is still available in
        ``raw`` and can be humanized; an own message resolves to "me"."""
        sender = row.get("sender")
        if sender:
            return str(sender)
        raw = row.get("raw") or {}
        if raw.get("from_me"):
            return "me"
        participant = raw.get("participant")
        return self._humanize_jid(participant) if participant else None

    # --- internals ---------------------------------------------------------

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
