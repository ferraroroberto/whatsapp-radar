"""Resync — pull the latest from the connector buffer into the local store.

The SQLite store is a *cache* rebuildable from the connector buffer. Resync is
the cheap, incremental, idempotent half of keeping it current: it re-reads the
buffer and writes only genuine differences. Chats upsert on ``source_chat_id``,
messages ``INSERT OR IGNORE`` on ``(chat_id, source_message_id)``, so running it
twice in a row reports nothing the second time and never duplicates. It is safe
to run while monitoring is configured because it never touches status, alias, or
review cursors — that is what separates it from a reprocess (full rebuild).

This is ``wr ingest`` made structured: it returns *what changed* (chats added,
chats updated, messages added) so both the CLI and the Execution tab can report
the delta instead of a bare "done".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from src.connector.base import MessageConnector
from src.connector.preflight import ensure_connected
from src.db import store


@dataclass
class IngestDelta:
    """What one ingest pass over the connector changed in the local store.

    The shared currency of :func:`ingest_chats`: ``chats_seen`` is every chat the
    connector listed (the live scan reports it as ``chats_synced``); the other
    three are genuine differences written. Both sync paths build their own
    ``sync_log`` row and outcome object from these counters, so the add/update
    diff lives in exactly one place.
    """

    chats_seen: int = 0
    chats_added: int = 0
    chats_updated: int = 0
    messages_added: int = 0


@dataclass
class ResyncOutcome:
    """What one resync changed in the local store."""

    chats_added: int = 0
    chats_updated: int = 0
    messages_added: int = 0

    @property
    def is_noop(self) -> bool:
        return (self.chats_added, self.chats_updated, self.messages_added) == (0, 0, 0)


def ingest_chats(
    conn: sqlite3.Connection,
    connector: MessageConnector,
) -> IngestDelta:
    """Upsert the connector's chats/messages into the store, reporting the delta.

    The single connector→store ingest loop shared by both sync paths
    (:func:`resync` and the scan pipeline's ``_sync``). For each listed chat:
    a chat unseen by source id is inserted (``chats_added``); an already-stored
    chat is re-upserted *only* when its display name or type actually differs
    (``chats_updated``) — re-seeing an unchanged chat writes nothing, which is
    what keeps a second run a no-op. Messages ``INSERT OR IGNORE`` on
    ``(chat_id, source_message_id)``.

    Pure ingest only: the caller owns the connector lifecycle
    (connect/ensure-connected + stop), the ``sync_log`` ``record_sync`` row, and
    any progress reporting — so each path keeps its own ``source`` tag and
    connection semantics at the call site.
    """
    delta = IngestDelta()
    for chat in connector.list_chats():
        existing_id = store.chat_id_for_source(conn, chat.source_chat_id)
        if existing_id is None:
            chat_id = store.upsert_chat(conn, chat)
            delta.chats_added += 1
        else:
            chat_id = existing_id
            existing = store.get_chat(conn, existing_id)
            if existing is not None and (
                existing["display_name"] != chat.display_name
                or existing["chat_type"] != chat.chat_type
            ):
                store.upsert_chat(conn, chat)
                delta.chats_updated += 1
        delta.chats_seen += 1
        delta.messages_added += store.insert_messages(
            conn, chat_id, connector.fetch_messages(chat.source_chat_id)
        )
    return delta


def resync(
    conn: sqlite3.Connection,
    connector: MessageConnector,
    *,
    source: str = "resync",
) -> ResyncOutcome:
    """Upsert the connector's chats/messages into the store, reporting the delta.

    Idempotent: a second run over an unchanged buffer is a no-op (all zeros). A
    chat counts as *updated* only when its display name or type actually differs
    from what is stored — re-seeing an unchanged chat writes nothing, so the
    no-op guarantee holds even though every chat is re-read each run.

    ``source`` tags the ``sync_log`` row this run writes. It defaults to
    ``"resync"``; a full rebuild (:func:`src.db.reprocess.reprocess`) passes
    ``"reprocess"`` so its ingest is distinguishable from an incremental resync
    in the Audit timeline.
    """
    # Liveness gate (#29): never upsert from a dead/stale source. Raises
    # ConnectorOffline if the connector isn't connected (the fixture, which loads
    # on connect, is always live — so the offline suite is unaffected).
    ensure_connected(connector)
    try:
        delta = ingest_chats(conn, connector)
    finally:
        connector.stop()
    outcome = ResyncOutcome(
        chats_added=delta.chats_added,
        chats_updated=delta.chats_updated,
        messages_added=delta.messages_added,
    )
    # One sync_log row per resync — visible whether fired from the CLI, a
    # scheduled Job, or the webapp (the per-message ingest time is on
    # messages.ingested_at; this is the per-run summary). ``source`` lets a
    # reprocess tag its rebuild ingest distinctly from an incremental resync.
    store.record_sync(
        conn,
        source=source,
        chats_added=outcome.chats_added,
        chats_updated=outcome.chats_updated,
        messages_added=outcome.messages_added,
    )
    return outcome


def resync_outcome_to_dict(outcome: ResyncOutcome) -> dict[str, Any]:
    """Serialize a resync result to the Execution-tab payload (``kind: resync``)."""
    return {
        "kind": "resync",
        "ok": True,
        "chats_added": outcome.chats_added,
        "chats_updated": outcome.chats_updated,
        "messages_added": outcome.messages_added,
        "noop": outcome.is_noop,
    }
