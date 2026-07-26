"""Non-routine acknowledgment items (Step 5/5 of #206, #219).

A minimal queue-with-mutable-status table — distinct from analysis_items/
analysis_trace (per-run decision records) because acknowledgment is state
that changes over time, not a run-scoped fact. See schema.sql's ack_items
comment block for the shape rationale.
"""

from __future__ import annotations

import sqlite3

from src.db.connection import _now, _rowid


def insert_ack_item(
    conn: sqlite3.Connection,
    run_id: int,
    chat_id: int,
    *,
    child: str | None,
    task_category: str | None,
    summary: str | None,
    calendar_event_id: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO ack_items
            (run_id, chat_id, child, task_category, summary, calendar_event_id,
             status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (run_id, chat_id, child, task_category, summary, calendar_event_id, _now()),
    )
    conn.commit()
    return _rowid(cur)


def pending_ack_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Pending ack items, newest first, joined to their chat's display name."""
    return list(
        conn.execute(
            "SELECT a.*, c.display_name FROM ack_items a "
            "JOIN chats c ON c.id = a.chat_id "
            "WHERE a.status = 'pending' ORDER BY a.id DESC"
        ).fetchall()
    )


def get_ack_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT a.*, c.display_name FROM ack_items a "
        "JOIN chats c ON c.id = a.chat_id WHERE a.id = ?",
        (item_id,),
    ).fetchone()
    return row


def acknowledge_item(conn: sqlite3.Connection, item_id: int) -> None:
    """Mark one item acknowledged. Idempotent — a re-tap on an already-
    acknowledged item leaves its original ``acknowledged_at`` untouched."""
    conn.execute(
        "UPDATE ack_items SET status = 'acknowledged', "
        "acknowledged_at = COALESCE(acknowledged_at, ?) WHERE id = ?",
        (_now(), item_id),
    )
    conn.commit()
