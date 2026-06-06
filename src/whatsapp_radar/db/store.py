"""SQLite store: connection/migration plus typed repository functions.

Storage owns chat metadata, messages, the per-chat review cursor, review runs,
analysis results, and notification state. Cursor advancement is exposed as an
explicit call (:func:`advance_cursor`) so callers can guarantee it happens only
after analysis has been persisted.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ..models import ChatRecord, MessageRecord, StoredMessage

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _rowid(cur: sqlite3.Cursor) -> int:
    """Return a cursor's last inserted rowid, asserting it exists (for type-checkers)."""
    assert cur.lastrowid is not None
    return cur.lastrowid


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating parent dirs) and migrate a database, returning the connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


# --- chats -----------------------------------------------------------------

def upsert_chat(conn: sqlite3.Connection, chat: ChatRecord) -> int:
    """Insert a chat or refresh its last-seen metadata. Returns the internal id."""
    now = _now()
    conn.execute(
        """
        INSERT INTO chats (source_chat_id, display_name, chat_type, status,
                           first_seen_at, last_seen_at)
        VALUES (?, ?, ?, 'discovered', ?, ?)
        ON CONFLICT(source_chat_id) DO UPDATE SET
            display_name = excluded.display_name,
            chat_type    = excluded.chat_type,
            last_seen_at = excluded.last_seen_at
        """,
        (chat.source_chat_id, chat.display_name, chat.chat_type, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM chats WHERE source_chat_id = ?", (chat.source_chat_id,)
    ).fetchone()
    return int(row["id"])


def set_chat_status(conn: sqlite3.Connection, chat_id: int, status: str) -> bool:
    """Set a chat's status ('monitored'|'ignored'|'discovered'). True if a row changed."""
    cur = conn.execute("UPDATE chats SET status = ? WHERE id = ?", (status, chat_id))
    conn.commit()
    return cur.rowcount > 0


def list_chats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT id, source_chat_id, display_name, chat_type, status, last_message_at "
            "FROM chats ORDER BY id"
        ).fetchall()
    )


def monitored_chats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT id, source_chat_id, display_name FROM chats "
            "WHERE status = 'monitored' ORDER BY id"
        ).fetchall()
    )


# --- messages --------------------------------------------------------------

def insert_message(conn: sqlite3.Connection, chat_id: int, msg: MessageRecord) -> bool:
    """Insert a message idempotently. Returns True if a new row was created."""
    import json

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (chat_id, source_message_id, sender_label, message_timestamp,
             text, message_type, raw_json, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            msg.source_message_id,
            msg.sender_label,
            msg.message_timestamp,
            msg.text,
            msg.message_type,
            json.dumps(msg.raw, ensure_ascii=False) if msg.raw else None,
            _now(),
        ),
    )
    if cur.rowcount > 0:
        conn.execute(
            "UPDATE chats SET last_message_at = MAX(COALESCE(last_message_at, ''), ?) "
            "WHERE id = ?",
            (msg.message_timestamp, chat_id),
        )
    conn.commit()
    return cur.rowcount > 0


def messages_since_cursor(conn: sqlite3.Connection, chat_id: int) -> list[StoredMessage]:
    """Messages newer than the chat's cursor, ordered by (timestamp, id).

    Uses the lexicographic ``(message_timestamp, id)`` ordering so a tie on
    timestamp is broken deterministically by insertion id.
    """
    state = conn.execute(
        "SELECT last_processed_message_timestamp AS ts, last_processed_message_id AS mid "
        "FROM chat_review_state WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    ts = state["ts"] if state else None
    mid = state["mid"] if state else None

    if ts is None or mid is None:
        rows = conn.execute(
            "SELECT id, chat_id, source_message_id, message_timestamp, text, "
            "sender_label, message_type FROM messages "
            "WHERE chat_id = ? ORDER BY message_timestamp, id",
            (chat_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, chat_id, source_message_id, message_timestamp, text, "
            "sender_label, message_type FROM messages "
            "WHERE chat_id = ? AND (message_timestamp > ? OR "
            "(message_timestamp = ? AND id > ?)) ORDER BY message_timestamp, id",
            (chat_id, ts, ts, mid),
        ).fetchall()

    return [
        StoredMessage(
            id=int(r["id"]),
            chat_id=int(r["chat_id"]),
            source_message_id=r["source_message_id"],
            message_timestamp=r["message_timestamp"],
            text=r["text"],
            sender_label=r["sender_label"],
            message_type=r["message_type"],
        )
        for r in rows
    ]


def get_rolling_context(conn: sqlite3.Connection, chat_id: int) -> str | None:
    row = conn.execute(
        "SELECT rolling_context_json FROM chat_review_state WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    return row["rolling_context_json"] if row else None


def advance_cursor(
    conn: sqlite3.Connection,
    chat_id: int,
    last_message_id: int,
    last_message_timestamp: str,
    rolling_context_json: str | None = None,
) -> None:
    """Advance the per-chat cursor. Call ONLY after analysis has been persisted."""
    conn.execute(
        """
        INSERT INTO chat_review_state
            (chat_id, last_reviewed_at, last_processed_message_id,
             last_processed_message_timestamp, rolling_context_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            last_reviewed_at = excluded.last_reviewed_at,
            last_processed_message_id = excluded.last_processed_message_id,
            last_processed_message_timestamp = excluded.last_processed_message_timestamp,
            rolling_context_json = excluded.rolling_context_json
        """,
        (chat_id, _now(), last_message_id, last_message_timestamp, rolling_context_json),
    )
    conn.commit()


# --- runs / analysis / notifications --------------------------------------

def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO review_runs (started_at, status) VALUES (?, 'running')", (_now(),)
    )
    conn.commit()
    return _rowid(cur)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    chats_reviewed: int,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE review_runs SET completed_at = ?, status = ?, chats_reviewed = ?, error = ? "
        "WHERE id = ?",
        (_now(), status, chats_reviewed, error, run_id),
    )
    conn.commit()


def insert_analysis_item(
    conn: sqlite3.Connection,
    run_id: int,
    chat_id: int,
    *,
    action_required: bool,
    priority: str | None,
    summary: str | None,
    suggested_next_action: str | None,
    deadline: str | None,
    confidence: float | None,
    evidence_message_ids_json: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO analysis_items
            (run_id, chat_id, action_required, priority, summary,
             suggested_next_action, deadline, confidence,
             evidence_message_ids_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            chat_id,
            1 if action_required else 0,
            priority,
            summary,
            suggested_next_action,
            deadline,
            confidence,
            evidence_message_ids_json,
            _now(),
        ),
    )
    conn.commit()
    return _rowid(cur)


def actionable_items_for_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT ai.*, c.display_name FROM analysis_items ai "
            "JOIN chats c ON c.id = ai.chat_id "
            "WHERE ai.run_id = ? AND ai.action_required = 1 "
            "ORDER BY ai.chat_id, ai.id",
            (run_id,),
        ).fetchall()
    )


def record_notification(
    conn: sqlite3.Connection, run_id: int, channel: str, status: str, error: str | None = None
) -> int:
    sent_at = _now() if status == "sent" else None
    cur = conn.execute(
        "INSERT INTO notifications (run_id, channel, status, sent_at, error) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, channel, status, sent_at, error),
    )
    conn.commit()
    return _rowid(cur)
