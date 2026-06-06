"""SQLite store: connection/migration plus typed repository functions.

Storage owns chat metadata, messages, the per-chat review cursor, review runs,
analysis results, and notification state. Cursor advancement is exposed as an
explicit call (:func:`advance_cursor`) so callers can guarantee it happens only
after analysis has been persisted.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.models import ChatRecord, MessageRecord, StoredMessage

_MESSAGE_COLUMNS = (
    "id, chat_id, source_message_id, message_timestamp, text, sender_label, message_type"
)


def _to_stored(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        source_message_id=row["source_message_id"],
        message_timestamp=row["message_timestamp"],
        text=row["text"],
        sender_label=row["sender_label"],
        message_type=row["message_type"],
    )

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
    # WAL + NORMAL keeps the many small commits in the review/ingest paths fast
    # while staying durable enough for a local single-writer store.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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


def list_chats(
    conn: sqlite3.Connection, *, order_by_recent: bool = False
) -> list[sqlite3.Row]:
    # ``order_by_recent`` lists the most recently-active chats first (NULLs last),
    # which is how an operator scans a large account to pick what to monitor.
    order = (
        "ORDER BY last_message_at IS NULL, last_message_at DESC, id"
        if order_by_recent
        else "ORDER BY id"
    )
    return list(
        conn.execute(
            "SELECT id, source_chat_id, display_name, chat_type, status, last_message_at "
            f"FROM chats {order}"
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


def insert_messages(conn: sqlite3.Connection, chat_id: int, msgs: list[MessageRecord]) -> int:
    """Bulk-insert messages idempotently in one transaction. Returns rows created.

    The ingest path can deliver tens of thousands of messages; committing per row
    (as :func:`insert_message` does for the single-message review path) is far too
    slow at that scale, so this batches the whole chat into one commit.
    """
    import json

    if not msgs:
        return 0
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO messages
            (chat_id, source_message_id, sender_label, message_timestamp,
             text, message_type, raw_json, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                chat_id,
                m.source_message_id,
                m.sender_label,
                m.message_timestamp,
                m.text,
                m.message_type,
                json.dumps(m.raw, ensure_ascii=False) if m.raw else None,
                _now(),
            )
            for m in msgs
        ],
    )
    inserted = conn.total_changes - before
    conn.execute(
        "UPDATE chats SET last_message_at = MAX(COALESCE(last_message_at, ''), ?) WHERE id = ?",
        (max(m.message_timestamp for m in msgs), chat_id),
    )
    conn.commit()
    return inserted


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
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            "WHERE chat_id = ? ORDER BY message_timestamp, id",
            (chat_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            "WHERE chat_id = ? AND (message_timestamp > ? OR "
            "(message_timestamp = ? AND id > ?)) ORDER BY message_timestamp, id",
            (chat_id, ts, ts, mid),
        ).fetchall()

    return [_to_stored(r) for r in rows]


def messages_for_chat(
    conn: sqlite3.Connection, chat_id: int, *, since_days: int | None = None
) -> list[StoredMessage]:
    """All messages for a chat, ordered by (timestamp, id), ignoring the cursor.

    Used by the dry-run scan to *replay* stored history rather than the live
    delta. ``since_days`` windows the replay to messages from the last N days.
    """
    if since_days is None:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            "WHERE chat_id = ? ORDER BY message_timestamp, id",
            (chat_id,),
        ).fetchall()
    else:
        cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            "WHERE chat_id = ? AND message_timestamp >= ? ORDER BY message_timestamp, id",
            (chat_id, cutoff),
        ).fetchall()
    return [_to_stored(r) for r in rows]


def baseline_cursor(conn: sqlite3.Connection, chat_id: int) -> bool:
    """Set the cursor to the latest stored message so only newer messages review.

    Used when a chat is first monitored: it baselines past the existing backlog so
    the first review does not classify months of history. No-op (returns False) if
    the chat already has a cursor or has no messages yet.
    """
    if conn.execute(
        "SELECT 1 FROM chat_review_state WHERE chat_id = ?", (chat_id,)
    ).fetchone():
        return False
    row = conn.execute(
        "SELECT id, message_timestamp FROM messages WHERE chat_id = ? "
        "ORDER BY message_timestamp DESC, id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    if row is None:
        return False
    advance_cursor(conn, chat_id, int(row["id"]), row["message_timestamp"], None)
    return True


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

def start_run(
    conn: sqlite3.Connection, mode: str = "review", params_json: str | None = None
) -> int:
    cur = conn.execute(
        "INSERT INTO review_runs (started_at, status, mode, params_json) "
        "VALUES (?, 'running', ?, ?)",
        (_now(), mode, params_json),
    )
    conn.commit()
    return _rowid(cur)


def record_run_funnel(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    chats_synced: int,
    messages_synced: int,
    chats_monitored: int,
    stage1_passed: int,
    stage2_llm_calls: int,
    actionable: int,
    notification_status: str,
) -> None:
    """Persist a run's funnel counters and final notification status."""
    conn.execute(
        "UPDATE review_runs SET chats_synced = ?, messages_synced = ?, chats_monitored = ?, "
        "stage1_passed = ?, stage2_llm_calls = ?, actionable = ?, notification_status = ? "
        "WHERE id = ?",
        (
            chats_synced,
            messages_synced,
            chats_monitored,
            stage1_passed,
            stage2_llm_calls,
            actionable,
            notification_status,
            run_id,
        ),
    )
    conn.commit()


def latest_run_id(conn: sqlite3.Connection) -> int | None:
    """Return the id of the most recent review run, or None if there are none."""
    row = conn.execute("SELECT id FROM review_runs ORDER BY id DESC LIMIT 1").fetchone()
    return int(row["id"]) if row else None


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


def insert_analysis_trace(
    conn: sqlite3.Connection,
    run_id: int,
    chat_id: int,
    *,
    input_message_ids_json: str,
    input_text: str | None,
    stage1_passed: bool,
    stage1_roots_json: str,
    llm_called: bool,
    llm_system_prompt: str | None,
    llm_user_prompt: str | None,
    llm_raw_response: str | None,
    parsed_result_json: str | None,
    final_action: str,
    telegram_text: str | None,
    error: str | None,
) -> int:
    """Persist the full per-chat audit trace for one run (one row per chat)."""
    cur = conn.execute(
        """
        INSERT INTO analysis_trace
            (run_id, chat_id, input_message_ids_json, input_text, stage1_passed,
             stage1_roots_json, llm_called, llm_system_prompt, llm_user_prompt,
             llm_raw_response, parsed_result_json, final_action, telegram_text,
             error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            chat_id,
            input_message_ids_json,
            input_text,
            1 if stage1_passed else 0,
            stage1_roots_json,
            1 if llm_called else 0,
            llm_system_prompt,
            llm_user_prompt,
            llm_raw_response,
            parsed_result_json,
            final_action,
            telegram_text,
            error,
            _now(),
        ),
    )
    conn.commit()
    return _rowid(cur)


def traces_for_run(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    """Return a run's audit-trace rows joined to chat names, ordered by chat."""
    return list(
        conn.execute(
            "SELECT t.*, c.display_name FROM analysis_trace t "
            "JOIN chats c ON c.id = t.chat_id "
            "WHERE t.run_id = ? ORDER BY t.chat_id",
            (run_id,),
        ).fetchall()
    )


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
