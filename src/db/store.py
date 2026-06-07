"""SQLite store: connection/migration plus typed repository functions.

Storage owns chat metadata, messages, the per-chat review cursor, review runs,
analysis results, and notification state. Cursor advancement is exposed as an
explicit call (:func:`advance_cursor`) so callers can guarantee it happens only
after analysis has been persisted.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.models import ChatRecord, MessageRecord, StoredMessage

_MESSAGE_COLUMNS = (
    "id, chat_id, source_message_id, message_timestamp, text, sender_label, "
    "message_type, transcription_status"
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
        transcription_status=row["transcription_status"],
    )

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Columns added to review_runs after the initial spike schema (#7's funnel).
# `CREATE TABLE IF NOT EXISTS` never backfills columns on a pre-existing table,
# so an older on-disk DB is missing them. These additive, non-destructive ALTERs
# bring it up to date — each has a constant default, which SQLite allows.
_REVIEW_RUNS_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("mode", "TEXT NOT NULL DEFAULT 'review'"),
    ("params_json", "TEXT"),
    ("chats_synced", "INTEGER NOT NULL DEFAULT 0"),
    ("messages_synced", "INTEGER NOT NULL DEFAULT 0"),
    ("chats_monitored", "INTEGER NOT NULL DEFAULT 0"),
    ("stage1_passed", "INTEGER NOT NULL DEFAULT 0"),
    ("stage2_llm_calls", "INTEGER NOT NULL DEFAULT 0"),
    ("actionable", "INTEGER NOT NULL DEFAULT 0"),
    ("notification_status", "TEXT"),
    ("voice_transcribed", "INTEGER NOT NULL DEFAULT 0"),
    ("voice_failed", "INTEGER NOT NULL DEFAULT 0"),
    ("voice_skipped_old", "INTEGER NOT NULL DEFAULT 0"),
)

_MESSAGES_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("transcription_status", "TEXT NOT NULL DEFAULT 'none'"),
    ("media_path", "TEXT"),
)


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
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additively backfill columns missing from an older on-disk schema.

    Idempotent: only adds a column when absent, so repeated opens are no-ops.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(review_runs)")}
    for name, declaration in _REVIEW_RUNS_ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE review_runs ADD COLUMN {name} {declaration}")
    # `chats.alias` (operator override label) was added after the initial schema.
    chat_cols = {row["name"] for row in conn.execute("PRAGMA table_info(chats)")}
    if "alias" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN alias TEXT")
    msg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    for name, declaration in _MESSAGES_ADDED_COLUMNS:
        if name not in msg_cols:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")


def _voice_ingest_fields(msg: MessageRecord) -> tuple[str, str | None]:
    """Derive transcription_status and media_path for ingest."""
    if msg.message_type != "voice":
        return "none", None
    raw = msg.raw or {}
    media_path = raw.get("media_path")
    if media_path:
        return "pending", str(media_path)
    return "failed", None


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


def set_chat_alias(conn: sqlite3.Connection, chat_id: int, alias: str | None) -> bool:
    """Set (or clear, with None) a chat's operator alias. True if a row changed.

    The alias overrides the connector-derived ``display_name`` in the UI; an empty
    or whitespace-only value is normalized to NULL so it falls back to that name.
    """
    cleaned = alias.strip() if alias else None
    cur = conn.execute(
        "UPDATE chats SET alias = ? WHERE id = ?", (cleaned or None, chat_id)
    )
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
            "SELECT id, source_chat_id, display_name, alias, chat_type, status, last_message_at "
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


def chat_id_for_source(conn: sqlite3.Connection, source_chat_id: str) -> int | None:
    """Return the internal id for a chat's ``source_chat_id``, or None if absent.

    The resync path uses this to classify an incoming chat as new (insert) vs
    existing (compare-then-maybe-update) without an upsert that would always
    touch ``last_seen_at`` and so report a phantom change on a no-op run.
    """
    row = conn.execute(
        "SELECT id FROM chats WHERE source_chat_id = ?", (source_chat_id,)
    ).fetchone()
    return int(row["id"]) if row else None


# --- messages --------------------------------------------------------------

def insert_message(conn: sqlite3.Connection, chat_id: int, msg: MessageRecord) -> bool:
    """Insert a message idempotently. Returns True if a new row was created."""
    import json

    transcription_status, media_path = _voice_ingest_fields(msg)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (chat_id, source_message_id, sender_label, message_timestamp,
             text, message_type, transcription_status, media_path, raw_json, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            msg.source_message_id,
            msg.sender_label,
            msg.message_timestamp,
            msg.text,
            msg.message_type,
            transcription_status,
            media_path,
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
    else:
        reconcile_voice_media(conn, chat_id, [msg])
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
    rows = [
        (
            chat_id,
            m.source_message_id,
            m.sender_label,
            m.message_timestamp,
            m.text,
            m.message_type,
            *_voice_ingest_fields(m),
            json.dumps(m.raw, ensure_ascii=False) if m.raw else None,
            _now(),
        )
        for m in msgs
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO messages
            (chat_id, source_message_id, sender_label, message_timestamp,
             text, message_type, transcription_status, media_path, raw_json, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    inserted = conn.total_changes - before
    reconcile_voice_media(conn, chat_id, msgs)
    conn.execute(
        "UPDATE chats SET last_message_at = MAX(COALESCE(last_message_at, ''), ?) WHERE id = ?",
        (max(m.message_timestamp for m in msgs), chat_id),
    )
    conn.commit()
    return inserted


def reconcile_voice_media(
    conn: sqlite3.Connection, chat_id: int, msgs: list[MessageRecord]
) -> int:
    """Backfill media_path on existing voice rows when the sidecar later delivers audio."""
    import json

    updated = 0
    for m in msgs:
        if m.message_type != "voice":
            continue
        raw = m.raw or {}
        media_path = raw.get("media_path")
        if not media_path:
            continue
        cur = conn.execute(
            """
            UPDATE messages SET media_path = ?, transcription_status = 'pending',
                   raw_json = ?
            WHERE chat_id = ? AND source_message_id = ?
              AND message_type = 'voice'
              AND transcription_status IN ('none', 'failed')
              AND (media_path IS NULL OR media_path = '')
            """,
            (
                str(media_path),
                json.dumps(raw, ensure_ascii=False),
                chat_id,
                m.source_message_id,
            ),
        )
        updated += cur.rowcount
    if updated:
        conn.commit()
    return updated


@dataclass(frozen=True)
class PendingTranscription:
    """A voice row awaiting hub transcription."""

    id: int
    media_path: str
    raw_json: str | None


def skip_old_voice_notes(conn: sqlite3.Connection, window_days: int) -> int:
    """Mark voice notes outside the transcription window as skipped_old."""
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    cur = conn.execute(
        """
        UPDATE messages SET transcription_status = 'skipped_old'
        WHERE message_type = 'voice'
          AND transcription_status NOT IN ('done', 'skipped_old')
          AND message_timestamp < ?
        """,
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def list_pending_transcriptions(
    conn: sqlite3.Connection, window_days: int
) -> list[PendingTranscription]:
    """Voice rows with pending status and a media file, within the window."""
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        """
        SELECT id, media_path, raw_json FROM messages
        WHERE transcription_status = 'pending'
          AND media_path IS NOT NULL
          AND message_timestamp >= ?
        ORDER BY message_timestamp, id
        """,
        (cutoff,),
    ).fetchall()
    return [
        PendingTranscription(int(r["id"]), r["media_path"], r["raw_json"]) for r in rows
    ]


def apply_transcription_done(
    conn: sqlite3.Connection,
    message_id: int,
    transcript: str,
    raw_json: str,
) -> None:
    """Persist a successful transcription and overwrite message text."""
    conn.execute(
        """
        UPDATE messages SET text = ?, transcription_status = 'done', raw_json = ?
        WHERE id = ?
        """,
        (transcript, raw_json, message_id),
    )
    conn.commit()


def apply_transcription_failed(conn: sqlite3.Connection, message_id: int) -> None:
    conn.execute(
        "UPDATE messages SET transcription_status = 'failed' WHERE id = ?",
        (message_id,),
    )
    conn.commit()


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


# --- dashboard aggregates (read-only) --------------------------------------
# These power the Dashboard tab (#9). They only ever SELECT — no writes, no
# cursor changes — so they are safe to call from the webapp request path.

def count_chats_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    """Chat counts keyed by status, always including the three known statuses."""
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM chats GROUP BY status").fetchall()
    counts = {row["status"]: int(row["n"]) for row in rows}
    return {
        "discovered": counts.get("discovered", 0),
        "monitored": counts.get("monitored", 0),
        "ignored": counts.get("ignored", 0),
    }


def message_count_total(conn: sqlite3.Connection) -> int:
    """Total stored messages across every chat (monitored or not)."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"])


def messages_per_chat(
    conn: sqlite3.Connection, *, monitored_only: bool = True
) -> list[sqlite3.Row]:
    """Per-chat message counts (id, display_name, status, last_message_at, message_count).

    Most-active chats first. ``monitored_only`` restricts to chats being watched,
    which is what the Dashboard's per-channel table shows.
    """
    where = "WHERE c.status = 'monitored'" if monitored_only else ""
    return list(
        conn.execute(
            "SELECT c.id, c.display_name, c.status, c.last_message_at, "
            "COUNT(m.id) AS message_count "
            "FROM chats c LEFT JOIN messages m ON m.chat_id = c.id "
            f"{where} GROUP BY c.id "
            "ORDER BY c.last_message_at IS NULL, c.last_message_at DESC, c.id"
        ).fetchall()
    )


# --- chats & config tab (read-only listing + bounded history) --------------
# Powers the Chats & Config tab (#10). Listing and history are SELECT-only; the
# tab's only writes go through set_chat_status / baseline_cursor (above).

def chats_overview(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All chats with their status, message count, and latest message preview.

    Columns: id, source_chat_id, display_name, chat_type, status,
    last_message_at, message_count, last_message_text. The latest text comes from
    a correlated subquery keyed by the same (timestamp, id) ordering the cursor
    uses. Most recently active first (NULLs last) so the operator's live chats
    surface at the top of the picker.
    """
    return list(
        conn.execute(
            "SELECT c.id, c.source_chat_id, c.display_name, c.alias, c.chat_type, c.status, "
            "c.last_message_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id) AS message_count, "
            "(SELECT m.text FROM messages m WHERE m.chat_id = c.id "
            " ORDER BY m.message_timestamp DESC, m.id DESC LIMIT 1) AS last_message_text "
            "FROM chats c "
            "ORDER BY c.last_message_at IS NULL, c.last_message_at DESC, c.id"
        ).fetchall()
    )


def recent_messages(
    conn: sqlite3.Connection,
    chat_id: int,
    *,
    limit: int = 100,
    before_ts: str | None = None,
    before_id: int | None = None,
) -> tuple[list[StoredMessage], bool]:
    """A page of the chat's messages (oldest→newest) plus whether older remain.

    No cursor → the newest ``limit`` messages. With a ``(before_ts, before_id)``
    cursor → the newest ``limit`` messages strictly *older* than it, which is how
    the history overlay lazily loads more as you scroll up. Ordering and the
    cursor both use the lexicographic ``(message_timestamp, id)`` key. One extra
    row is fetched so ``has_more`` is known without a second query. Bounded so a
    chat with tens of thousands of messages never floods the request path.
    """
    if before_ts is not None and before_id is not None:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            "WHERE chat_id = ? AND (message_timestamp < ? OR "
            "(message_timestamp = ? AND id < ?)) "
            "ORDER BY message_timestamp DESC, id DESC LIMIT ?",
            (chat_id, before_ts, before_ts, before_id, limit + 1),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            "WHERE chat_id = ? ORDER BY message_timestamp DESC, id DESC LIMIT ?",
            (chat_id, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return [_to_stored(r) for r in reversed(rows)], has_more


def get_chat(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    """Return a single chat row by internal id, or None if it doesn't exist."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, source_chat_id, display_name, alias, chat_type, status, last_message_at "
        "FROM chats WHERE id = ?",
        (chat_id,),
    ).fetchone()
    return row


def count_runs(conn: sqlite3.Connection) -> int:
    """Number of review/scan runs recorded."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM review_runs").fetchone()["n"])


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The most recent review run row, or None if no run has happened yet."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM review_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row


def list_review_runs(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Review/scan runs newest-first, with the funnel columns the Audit list needs.

    Read-only (SELECT only) so it is safe on the webapp request path; powers the
    Audit tab's run list where each run is shown with its mode, parameters, and
    funnel counters before drilling into the per-chat trace.
    """
    return list(
        conn.execute(
            "SELECT id, started_at, completed_at, status, mode, params_json, "
            "chats_synced, messages_synced, chats_monitored, chats_reviewed, "
            "stage1_passed, stage2_llm_calls, actionable, notification_status, error "
            "FROM review_runs ORDER BY id DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
    )


def review_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    """A single review run by id (full row), or None if it doesn't exist."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM review_runs WHERE id = ?", (run_id,)
    ).fetchone()
    return row


def count_messages_since(conn: sqlite3.Connection, ingested_after: str) -> int:
    """Messages ingested strictly after an ISO timestamp (the unscanned backlog)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE ingested_at > ?", (ingested_after,)
    ).fetchone()
    return int(row["n"])


def count_actionable_items(conn: sqlite3.Connection) -> int:
    """Total actionable analysis verdicts across all runs (real alerts raised)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM analysis_items WHERE action_required = 1"
    ).fetchone()
    return int(row["n"])


def count_notifications_sent(conn: sqlite3.Connection) -> int:
    """Total notifications successfully delivered across all runs."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM notifications WHERE status = 'sent'"
    ).fetchone()
    return int(row["n"])


def count_chats(conn: sqlite3.Connection) -> int:
    """Total chats stored, regardless of status."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()["n"])


def count_messages(conn: sqlite3.Connection) -> int:
    """Total messages stored across all chats."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"])


# --- sync log (per-ingest visibility) --------------------------------------

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
            count_messages(conn),
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


# --- reprocess (full cache rebuild) ----------------------------------------
# The local store is a cache rebuildable from the connector buffer. Reprocess
# (src/db/reprocess.py) snapshots operator-set state, wipes the derived cache,
# re-ingests with current reader logic, then re-applies the snapshot. These two
# helpers are the snapshot + wipe primitives; the orchestration lives in
# reprocess.py so the SQL stays here with the schema knowledge.

def snapshot_operator_state(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Operator-set state worth preserving across a rebuild: status + alias.

    Returns (source_chat_id, status, alias) for every chat the operator has
    touched — anything not in the default 'discovered'/no-alias resting state.
    Keyed by ``source_chat_id`` (not the internal id, which the rebuild reassigns).
    """
    return list(
        conn.execute(
            "SELECT source_chat_id, status, alias FROM chats "
            "WHERE status != 'discovered' OR alias IS NOT NULL"
        ).fetchall()
    )


def clear_all_data(conn: sqlite3.Connection) -> None:
    """Wipe every derived/cache table so a reprocess can rebuild from scratch.

    Deletes children before parents so the wipe holds whether or not SQLite's
    per-connection foreign-key enforcement happens to be on. Run/analysis history
    is intentionally discarded — it cannot be re-keyed to the rebuilt chat ids.
    """
    for table in (
        "notifications",
        "analysis_items",
        "analysis_trace",
        "chat_review_state",
        "messages",
        "review_runs",
        "chats",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
