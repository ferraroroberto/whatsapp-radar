"""Per-ingest sync visibility log."""

from __future__ import annotations

import sqlite3

from src.db.chats import count_chats
from src.db.connection import _now, _rowid
from src.db.messages import message_count_total


def record_sync(
    conn: sqlite3.Connection,
    *,
    source: str,
    chats_added: int,
    chats_updated: int,
    messages_added: int,
) -> int:
    """Record one sync's delta + the running totals afterwards. Returns its id.

    Written by every sync path (resync, live scan) so a scheduled job is as
    visible as a webapp click. The per-message ingest time lives on
    ``messages.ingested_at``; this is the per-run summary on top of it.
    """
    cur = conn.execute(
        "INSERT INTO sync_log (ran_at, source, chats_added, chats_updated, "
        "messages_added, total_chats, total_messages) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            _now(),
            source,
            chats_added,
            chats_updated,
            messages_added,
            count_chats(conn),
            message_count_total(conn),
        ),
    )
    conn.commit()
    return _rowid(cur)


def recent_syncs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """The most recent sync_log rows, newest first."""
    return conn.execute(
        "SELECT id, ran_at, source, chats_added, chats_updated, messages_added, "
        "total_chats, total_messages FROM sync_log ORDER BY id DESC LIMIT ?",
        (max(1, limit),),
    ).fetchall()
