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

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from src.connector.base import ConnectorStatus, MessageConnector
from src.connector.factory import ConnectorBinding
from src.connector.preflight import ConnectorOffline, ensure_connected
from src.db import store

logger = logging.getLogger(__name__)


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
    source_errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return (self.chats_added, self.chats_updated, self.messages_added) == (0, 0, 0)


def ingest_chats(
    conn: sqlite3.Connection,
    connector: MessageConnector,
    *,
    source: str | None = None,
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
        if source is not None and chat.source != source:
            chat = replace(chat, source=source)
        existing_id = store.chat_id_for_source(conn, chat.source_chat_id, source=chat.source)
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
        # Incremental capability (#180): a connector exposing fetch_messages_new
        # is given the ids already stored so it can skip re-downloading them.
        # The store's INSERT OR IGNORE keeps either path idempotent.
        fetch_new = getattr(connector, "fetch_messages_new", None)
        if callable(fetch_new):
            records = fetch_new(
                chat.source_chat_id, store.message_source_ids(conn, chat_id)
            )
        else:
            records = connector.fetch_messages(chat.source_chat_id)
        delta.messages_added += store.insert_messages(conn, chat_id, records)
    return delta


@dataclass
class SourceSyncOutcome:
    """One enabled source's isolated ingest result."""

    source: str
    delta: IngestDelta = field(default_factory=IngestDelta)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class MultiSourceSyncOutcome:
    """Aggregated result of one fan-out across enabled sources."""

    results: list[SourceSyncOutcome] = field(default_factory=list)

    @property
    def successful_sources(self) -> set[str]:
        return {result.source for result in self.results if result.ok}

    @property
    def source_errors(self) -> list[tuple[str, str]]:
        return [
            (result.source, result.error)
            for result in self.results
            if result.error is not None
        ]

    @property
    def delta(self) -> IngestDelta:
        return IngestDelta(
            chats_seen=sum(result.delta.chats_seen for result in self.results),
            chats_added=sum(result.delta.chats_added for result in self.results),
            chats_updated=sum(result.delta.chats_updated for result in self.results),
            messages_added=sum(result.delta.messages_added for result in self.results),
        )


PrepareSource = Callable[[str, MessageConnector], ConnectorStatus]
Progress = Callable[[str], None]


def sync_sources(
    conn: sqlite3.Connection,
    bindings: Sequence[ConnectorBinding],
    *,
    operation: str,
    prepare: PrepareSource | None = None,
    gmail_retention_days: int = 30,
    progress: Progress | None = None,
) -> MultiSourceSyncOutcome:
    """Ingest every binding independently and record a truthful per-source row.

    After a successful Gmail ingest, unmonitored senders' messages past
    ``gmail_retention_days`` are pruned (#166) so a discovered mailbox never floods
    the store; monitored senders and all WhatsApp data are untouched. The pruned
    counts are logged and folded into that source's sync-log detail.
    ``gmail_retention_days=0`` skips the prune entirely — used by the destructive
    reprocess rebuild, which re-applies monitored status only *after* re-ingest, so
    pruning mid-rebuild would wrongly drop a monitored sender's history.

    ``progress`` streams one line per source stage (counts only, never message
    content or sender addresses) so a long ingest is distinguishable from a hang
    in the Run tab's output.log (#180).
    """
    outcome = MultiSourceSyncOutcome()
    prepare_source = prepare or (lambda _source, connector: ensure_connected(connector))

    def _emit(line: str) -> None:
        if progress is not None:
            progress(line)

    for binding in bindings:
        result = SourceSyncOutcome(source=binding.source)
        _emit(f"• {binding.source}: syncing…")
        try:
            prepare_source(binding.source, binding.connector)
            result.delta = ingest_chats(
                conn,
                binding.connector,
                source=binding.source,
            )
        except ConnectorOffline as exc:
            result.error = str(exc)
        finally:
            binding.connector.stop()
        if result.ok:
            _emit(
                f"✓ {binding.source}: {result.delta.chats_seen} chat(s) · "
                f"+{result.delta.chats_added} new chat(s) · "
                f"+{result.delta.messages_added} message(s)"
            )
        else:
            _emit(f"✗ {binding.source}: {result.error}")
        detail = result.error or ""
        if binding.source == "gmail" and result.ok and gmail_retention_days > 0:
            pruned = store.prune_gmail_unmonitored(
                conn, retention_days=gmail_retention_days
            )
            if not pruned.is_noop:
                logger.info(
                    "ℹ️ Gmail retention: pruned %d message(s) and %d empty sender(s) "
                    "older than %s (monitored senders exempt)",
                    pruned.messages_pruned,
                    pruned.senders_removed,
                    pruned.cutoff,
                )
                detail = (
                    f"retention pruned {pruned.messages_pruned} msg / "
                    f"{pruned.senders_removed} sender(s)"
                )
                _emit(f"• gmail: {detail}")
        outcome.results.append(result)
        store.record_sync(
            conn,
            source=operation,
            connector_source=binding.source,
            status="success" if result.ok else "failed",
            detail=detail,
            chats_added=result.delta.chats_added,
            chats_updated=result.delta.chats_updated,
            messages_added=result.delta.messages_added,
        )
    return outcome


def resync(
    conn: sqlite3.Connection,
    connector: MessageConnector | Sequence[ConnectorBinding],
    *,
    source: str = "resync",
    prepare: PrepareSource | None = None,
    gmail_retention_days: int = 30,
    progress: Progress | None = None,
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
    bindings = (
        list(connector)
        if not isinstance(connector, MessageConnector)
        else [ConnectorBinding(source="whatsapp", connector=connector)]
    )
    synced = sync_sources(
        conn,
        bindings,
        operation=source,
        prepare=prepare,
        gmail_retention_days=gmail_retention_days,
        progress=progress,
    )
    delta = synced.delta
    if not synced.successful_sources:
        detail = "; ".join(f"{name}: {error}" for name, error in synced.source_errors)
        raise ConnectorOffline(
            ConnectorStatus(name="all_sources", connected=False, detail=detail)
        )
    outcome = ResyncOutcome(
        chats_added=delta.chats_added,
        chats_updated=delta.chats_updated,
        messages_added=delta.messages_added,
        source_errors=synced.source_errors,
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
        "source_errors": outcome.source_errors,
    }
