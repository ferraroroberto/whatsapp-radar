"""Reprocess — rebuild the local cache from the connector buffer.

Unlike :func:`src.db.sync.resync` (incremental upsert), reprocess is the *full
rebuild*: it wipes the derived SQLite cache and re-ingests from the buffer with
the current reader logic. It exists for the rare case where the reader logic
itself changed — e.g. the #22/#23 fix that re-keys messages to canonical JIDs,
folds ``@lid`` aliases, and re-resolves display names. An incremental resync
can't fix already-stored rows that were keyed/named by the *old* reader; only a
rebuild can.

Because re-keying changes ``source_chat_id`` for some chats, the operator's
hand-set state (monitored/ignored status and aliases) would be stranded on the
old ids. So reprocess:

1. snapshots ``(source_chat_id, status, alias)`` for every operator-touched chat;
2. backs up the DB file first (a rebuild is destructive);
3. wipes the cache and re-ingests via :func:`resync` with the current reader;
4. re-applies the snapshot, mapping each old ``source_chat_id`` through the
   connector's *current* canonicalization so state lands on the rebuilt chat —
   re-baselining monitored chats so the first review skips the backlog.

Run/analysis history is **not** re-keyable and is intentionally discarded; the
caller must warn the operator before running.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.connector.base import MessageConnector
from src.db import store
from src.db.sync import resync


@dataclass
class ReprocessOutcome:
    """The result of a full cache rebuild."""

    backup_path: str
    chats_before: int = 0
    chats_after: int = 0
    messages_after: int = 0
    monitored_preserved: int = 0
    ignored_preserved: int = 0
    aliases_preserved: int = 0
    links_preserved: int = 0
    # Operator-touched chats whose old source id couldn't be matched to any
    # rebuilt chat (e.g. a chat that no longer exists in the buffer). Their
    # state is dropped — surfaced so the operator can re-set it by hand.
    unmapped: list[str] = field(default_factory=list)


def _backup_db(conn: sqlite3.Connection, db_path: Path) -> Path:
    """Checkpoint the WAL and copy the DB file to a timestamped ``.bak`` sibling."""
    conn.commit()
    # Fold the WAL back into the main file so the plain copy is complete.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.bak-{stamp}")
    shutil.copy2(db_path, backup)
    return backup


def reprocess(
    conn: sqlite3.Connection, connector: MessageConnector, db_path: Path
) -> ReprocessOutcome:
    """Rebuild the cache from the buffer, preserving operator state. Destructive.

    Backs the DB up first, wipes the derived tables, re-ingests with the current
    reader, then re-applies monitored/ignored/alias state mapped through the
    connector's canonicalization. Returns counts plus the backup path.
    """
    snapshot = store.snapshot_operator_state(conn)
    chats_before = store.count_chats(conn)

    backup = _backup_db(conn, db_path)

    store.clear_all_data(conn)
    resync(conn, connector)

    outcome = ReprocessOutcome(
        backup_path=str(backup),
        chats_before=chats_before,
        chats_after=store.count_chats(conn),
        messages_after=store.message_count_total(conn),
    )

    for row in snapshot:
        old_source = row["source_chat_id"]
        status = row["status"]
        alias = row["alias"]
        new_source = connector.canonical_source_id(old_source) or old_source
        chat_id = store.chat_id_for_source(conn, new_source)
        if chat_id is None:
            outcome.unmapped.append(old_source)
            continue
        if status == "monitored":
            store.set_chat_status(conn, chat_id, "monitored")
            # Re-baseline so the first post-rebuild review skips the backlog.
            store.baseline_cursor(conn, chat_id)
            outcome.monitored_preserved += 1
        elif status == "ignored":
            store.set_chat_status(conn, chat_id, "ignored")
            outcome.ignored_preserved += 1
        if alias:
            store.set_chat_alias(conn, chat_id, alias)
            outcome.aliases_preserved += 1

    # Re-apply parent↔child links in a second pass, after every chat's id and
    # state exist in the rebuilt cache. Both sides are mapped through the
    # connector's canonicalization; a link the rebuilt topology no longer permits
    # (e.g. either end vanished, or it would now form a chain) is silently dropped.
    for row in snapshot:
        parent_source = row["parent_source_chat_id"]
        if not parent_source:
            continue
        child_source = connector.canonical_source_id(row["source_chat_id"]) or row[
            "source_chat_id"
        ]
        new_parent_source = connector.canonical_source_id(parent_source) or parent_source
        child_id = store.chat_id_for_source(conn, child_source)
        parent_id = store.chat_id_for_source(conn, new_parent_source)
        if child_id is None or parent_id is None or child_id == parent_id:
            continue
        try:
            store.link_chats(conn, child_id, parent_id)
            outcome.links_preserved += 1
        except store.LinkError:
            continue

    return outcome


def reprocess_outcome_to_dict(outcome: ReprocessOutcome) -> dict[str, Any]:
    """Serialize a reprocess result to the Execution-tab payload (``kind: reprocess``)."""
    return {
        "kind": "reprocess",
        "ok": True,
        "backup_path": outcome.backup_path,
        "chats_before": outcome.chats_before,
        "chats_after": outcome.chats_after,
        "messages_after": outcome.messages_after,
        "monitored_preserved": outcome.monitored_preserved,
        "ignored_preserved": outcome.ignored_preserved,
        "aliases_preserved": outcome.aliases_preserved,
        "links_preserved": outcome.links_preserved,
        "unmapped": outcome.unmapped,
    }
