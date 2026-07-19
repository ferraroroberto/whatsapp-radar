"""Bounded reads and cadence state for the unmonitored-chat tripwire."""

from __future__ import annotations

import sqlite3


def recent_discovered_messages(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    max_messages: int,
    max_messages_per_chat: int,
) -> tuple[list[sqlite3.Row], bool]:
    """Return a bounded, fair slice of recent messages from discovered chats.

    A per-chat cap prevents one noisy conversation consuming the global budget.
    Linked children are excluded because their messages are already represented by
    a top-level family. Explicitly ignored and monitored chats never enter the CTE.
    """
    rows = list(
        conn.execute(
            """
            WITH recent AS (
                SELECT
                    m.id,
                    m.chat_id,
                    m.message_timestamp,
                    m.text,
                    c.source,
                    COALESCE(c.alias, c.display_name) AS display_name,
                    ROW_NUMBER() OVER (
                        PARTITION BY m.chat_id
                        ORDER BY m.message_timestamp DESC, m.id DESC
                    ) AS chat_rank,
                    COUNT(*) OVER (PARTITION BY m.chat_id) AS chat_message_count
                FROM messages m
                JOIN chats c ON c.id = m.chat_id
                WHERE c.status = 'discovered'
                  AND c.parent_chat_id IS NULL
                  AND m.text IS NOT NULL
                  AND TRIM(m.text) != ''
                  AND julianday(m.message_timestamp) >= julianday(?)
            )
            SELECT * FROM recent
            WHERE chat_rank <= ?
            ORDER BY message_timestamp DESC, id DESC
            LIMIT ?
            """,
            (cutoff, max_messages_per_chat, max_messages + 1),
        ).fetchall()
    )
    truncated = len(rows) > max_messages or any(
        int(row["chat_message_count"]) > max_messages_per_chat for row in rows
    )
    return rows[:max_messages], truncated


def last_tripwire_nudge_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT last_sent_at FROM tripwire_nudge_state WHERE id = 1"
    ).fetchone()
    return str(row["last_sent_at"]) if row is not None else None


def mark_tripwire_nudge_sent(conn: sqlite3.Connection, sent_at: str) -> None:
    conn.execute(
        "INSERT INTO tripwire_nudge_state (id, last_sent_at) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET last_sent_at = excluded.last_sent_at",
        (sent_at,),
    )
    conn.commit()
