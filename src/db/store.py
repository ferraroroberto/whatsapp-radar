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
    "id, chat_id, source_message_id, message_timestamp, text, sender_label, "
    "message_type, transcription_status, media_path"
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
        media_path=row["media_path"],
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
    ("transcriptions", "INTEGER NOT NULL DEFAULT 0"),
    ("actionable", "INTEGER NOT NULL DEFAULT 0"),
    ("notification_status", "TEXT"),
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
    # `chats.alias` (operator override label) and `chats.parent_chat_id` (the
    # parent↔child link) were both added after the initial schema. Each is an
    # additive, non-destructive ALTER with a constant default.
    chat_cols = {row["name"] for row in conn.execute("PRAGMA table_info(chats)")}
    if "alias" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN alias TEXT")
    if "parent_chat_id" not in chat_cols:
        conn.execute(
            "ALTER TABLE chats ADD COLUMN parent_chat_id INTEGER "
            "REFERENCES chats(id) ON DELETE SET NULL"
        )
    # `chats.source` (#57) lets a second connector (Gmail, #46) share these tables:
    # chat identity becomes (source, source_chat_id). Existing rows backfill to
    # 'whatsapp' via the column default. SQLite can't add a composite table
    # constraint or drop the legacy column-level UNIQUE(source_chat_id) after the
    # fact without a full table rebuild, so composite uniqueness is enforced by a
    # unique *index* instead. The legacy single-column UNIQUE stays — harmless for
    # whatsapp-only rows (the only source until #46 lands) and dropped only if a
    # future rebuild is ever warranted. The fresh schema (schema.sql) declares the
    # composite as a table constraint; both forms back the same ON CONFLICT target.
    if "source" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN source TEXT NOT NULL DEFAULT 'whatsapp'")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_chats_source_key "
        "ON chats(source, source_chat_id)"
    )
    # `analysis_trace.messages_json` (per-message audit record: id/sender/text and
    # the Stage-1 keyword roots each message matched) was added after the initial
    # trace schema (#12). Additive, non-destructive; old rows stay NULL and the
    # audit view falls back to the rendered `input_text` blob for them.
    trace_cols = {row["name"] for row in conn.execute("PRAGMA table_info(analysis_trace)")}
    if "messages_json" not in trace_cols:
        conn.execute("ALTER TABLE analysis_trace ADD COLUMN messages_json TEXT")
    # `analysis_items.deadline_date` (#71): the model-resolved absolute date that
    # sits beside the free-text `deadline`, letting the digest flag today/overdue
    # deterministically. Additive, non-destructive; old rows stay NULL.
    item_cols = {row["name"] for row in conn.execute("PRAGMA table_info(analysis_items)")}
    if "deadline_date" not in item_cols:
        conn.execute("ALTER TABLE analysis_items ADD COLUMN deadline_date TEXT")
    # `messages.transcription_status` + `messages.media_path` (#36): the voice-note
    # transcription lifecycle and a transient ref to the downloaded audio. Additive,
    # non-destructive; old rows (and every non-voice message) stay NULL, so the
    # analysis pipeline — which reads only `messages.text` — is untouched.
    msg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "transcription_status" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN transcription_status TEXT")
    if "media_path" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN media_path TEXT")


# --- chats -----------------------------------------------------------------

def upsert_chat(conn: sqlite3.Connection, chat: ChatRecord) -> int:
    """Insert a chat or refresh its last-seen metadata. Returns the internal id."""
    now = _now()
    conn.execute(
        """
        INSERT INTO chats (source, source_chat_id, display_name, chat_type, status,
                           first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, 'discovered', ?, ?)
        ON CONFLICT(source, source_chat_id) DO UPDATE SET
            display_name = excluded.display_name,
            chat_type    = excluded.chat_type,
            last_seen_at = excluded.last_seen_at
        """,
        (chat.source, chat.source_chat_id, chat.display_name, chat.chat_type, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM chats WHERE source = ? AND source_chat_id = ?",
        (chat.source, chat.source_chat_id),
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


class LinkError(ValueError):
    """An operator link request that violates the parent↔child rules.

    Raised by :func:`link_chats` so the API can translate it to a 400. Existence
    (404) is checked by the caller before linking.
    """


def child_count(conn: sqlite3.Connection, parent_id: int) -> int:
    """How many chats are linked as children of ``parent_id`` (0 if it's not a parent)."""
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM chats WHERE parent_chat_id = ?", (parent_id,)
        ).fetchone()["n"]
    )


def child_chats(conn: sqlite3.Connection, parent_id: int) -> list[sqlite3.Row]:
    """The child chats linked under ``parent_id``, ordered by id (empty if none)."""
    return list(
        conn.execute(
            "SELECT id, source_chat_id, display_name, alias, chat_type, status, "
            "last_message_at, parent_chat_id FROM chats WHERE parent_chat_id = ? ORDER BY id",
            (parent_id,),
        ).fetchall()
    )


def family_member_ids(conn: sqlite3.Connection, head_id: int) -> list[int]:
    """Chat ids that make up a family: the head first, then its children by id.

    For a standalone or childless chat this is just ``[head_id]``. The review path
    folds these members' deltas into one analysis under the head; each member still
    keeps its own per-chat cursor.
    """
    rows = conn.execute(
        "SELECT id FROM chats WHERE parent_chat_id = ? ORDER BY id", (head_id,)
    ).fetchall()
    return [head_id, *(int(r["id"]) for r in rows)]


def link_chats(conn: sqlite3.Connection, child_id: int, parent_id: int) -> None:
    """Link ``child_id`` as a child of ``parent_id`` (also used to re-parent).

    The link lives on the child (``parent_chat_id``). Enforces depth-1 families:
    a chat can't link to itself, can't link under a chat that is itself a child,
    and a chat that already has children can't become a child. Raises
    :class:`LinkError` on any violation. Callers verify both chats exist first.
    """
    if child_id == parent_id:
        raise LinkError("a chat cannot be linked to itself")
    parent = get_chat(conn, parent_id)
    if parent is None:
        raise LinkError("parent chat not found")
    if parent["parent_chat_id"] is not None:
        raise LinkError("cannot link under a chat that is itself a child")
    if child_count(conn, child_id) > 0:
        raise LinkError("cannot link a chat that already has linked children")
    conn.execute(
        "UPDATE chats SET parent_chat_id = ? WHERE id = ?", (parent_id, child_id)
    )
    conn.commit()


def unlink_chat(conn: sqlite3.Connection, child_id: int) -> bool:
    """Clear a chat's parent link, restoring it as an independent chat.

    Returns True if the chat was a child (a row changed); False if it had no link.
    No message data or cursor is touched — only the link metadata is removed.
    """
    cur = conn.execute(
        "UPDATE chats SET parent_chat_id = NULL WHERE id = ? AND parent_chat_id IS NOT NULL",
        (child_id,),
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
            "SELECT id, source, source_chat_id, display_name, alias, chat_type, status, "
            f"last_message_at FROM chats {order}"
        ).fetchall()
    )


def monitored_chats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # Review iterates *family heads* only: a monitored chat that has been linked
    # as a child (``parent_chat_id`` set) is folded into its parent's family
    # review, never reviewed standalone. Its own status is left intact and takes
    # effect again once it is unlinked.
    return list(
        conn.execute(
            "SELECT id, source_chat_id, display_name FROM chats "
            "WHERE status = 'monitored' AND parent_chat_id IS NULL ORDER BY id"
        ).fetchall()
    )


def chat_id_for_source(
    conn: sqlite3.Connection, source_chat_id: str, *, source: str = "whatsapp"
) -> int | None:
    """Return the internal id for a chat's ``(source, source_chat_id)``, or None.

    Chat identity is the composite ``(source, source_chat_id)`` (#57); ``source``
    defaults to ``'whatsapp'`` so existing single-source callers are unchanged.
    The resync path uses this to classify an incoming chat as new (insert) vs
    existing (compare-then-maybe-update) without an upsert that would always
    touch ``last_seen_at`` and so report a phantom change on a no-op run.
    """
    row = conn.execute(
        "SELECT id FROM chats WHERE source = ? AND source_chat_id = ?",
        (source, source_chat_id),
    ).fetchone()
    return int(row["id"]) if row else None


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
             text, message_type, transcription_status, media_path, raw_json, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                chat_id,
                m.source_message_id,
                m.sender_label,
                m.message_timestamp,
                m.text,
                m.message_type,
                m.transcription_status,
                m.media_path,
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
    """Messages ingested after the chat's cursor, ordered by (timestamp, id).

    The cursor key is the monotonic ingestion id (``messages.id``, an
    AUTOINCREMENT rowid), **not** the send-timestamp. Ingestion is not monotonic
    in send-time: a resync can backfill history whose ``message_timestamp``
    predates messages already past the cursor. Keying the delta on send-time
    would filter those out forever; keying on ``id`` guarantees nothing ingested
    after the cursor is ever skipped (#37). The rows are still *ordered* by
    ``(message_timestamp, id)`` so the delta reads in send-time order for the
    classifier and digest — only the cursor predicate uses ``id``.
    """
    state = conn.execute(
        "SELECT last_processed_message_id AS mid FROM chat_review_state WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    mid = state["mid"] if state else None

    if mid is None:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            "WHERE chat_id = ? ORDER BY message_timestamp, id",
            (chat_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            "WHERE chat_id = ? AND id > ? ORDER BY message_timestamp, id",
            (chat_id, mid),
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


def _merge_by_send_order(deltas: list[list[StoredMessage]]) -> list[StoredMessage]:
    """Flatten per-member deltas into one list ordered by ``(message_timestamp, id)``.

    The same total order the cursor and history paging use, so a merged family
    delta reads in send-time order for the classifier and digest while each
    message keeps its origin ``chat_id`` for per-member cursor advancement.
    """
    merged = [m for delta in deltas for m in delta]
    merged.sort(key=lambda m: (m.message_timestamp, m.id))
    return merged


def family_delta_since_cursor(conn: sqlite3.Connection, head_id: int) -> list[StoredMessage]:
    """The live review delta for a whole family: each member's messages past *its
    own* cursor, merged in send-time order. For a standalone chat this is exactly
    :func:`messages_since_cursor`. Members keep independent cursors (#37), so the
    caller must advance each member it consumed — not just the head.
    """
    members = family_member_ids(conn, head_id)
    return _merge_by_send_order([messages_since_cursor(conn, cid) for cid in members])


def family_delta_replay(
    conn: sqlite3.Connection, head_id: int, *, since_days: int | None = None
) -> list[StoredMessage]:
    """The dry-run replay delta for a whole family: every member's stored messages
    (optionally windowed to ``since_days``), merged in send-time order, ignoring
    cursors. The family counterpart of :func:`messages_for_chat`.
    """
    members = family_member_ids(conn, head_id)
    return _merge_by_send_order(
        [messages_for_chat(conn, cid, since_days=since_days) for cid in members]
    )


def baseline_cursor(conn: sqlite3.Connection, chat_id: int) -> bool:
    """Set the cursor to the last-ingested stored message so only newer review.

    Used when a chat is first monitored: it baselines past the existing backlog so
    the first review does not classify months of history. No-op (returns False) if
    the chat already has a cursor or has no messages yet. Baselines by the max
    ingestion ``id`` (not max send-time) to match the cursor key — so a chat whose
    backlog includes backfilled, out-of-send-order history is still fully skipped
    rather than replaying any message ingested before monitoring began (#37).
    """
    if conn.execute(
        "SELECT 1 FROM chat_review_state WHERE chat_id = ?", (chat_id,)
    ).fetchone():
        return False
    row = conn.execute(
        "SELECT id, message_timestamp FROM messages WHERE chat_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    if row is None:
        return False
    advance_cursor(conn, chat_id, int(row["id"]), row["message_timestamp"], None)
    return True


def recent_actionable_items(
    conn: sqlite3.Connection,
    head_id: int,
    *,
    since_days: int,
    now: datetime | None = None,
    exclude_run_id: int | None = None,
) -> list[sqlite3.Row]:
    """Actionable items already surfaced for a family within the last ``since_days``.

    The short-term alert memory (#66): every actionable ``analysis_items`` row for
    the family (head + children, :func:`family_member_ids`), within the window
    measured from each row's run ``started_at``, ordered oldest-first. ``summary``
    is required (it's what we'd re-surface). ``exclude_run_id`` drops the
    in-progress run so a run can never feed itself. The cutoff is compared as a
    UTC-ISO string, matching :func:`_now`, so lexicographic order is chronological.
    """
    members = family_member_ids(conn, head_id)
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=since_days)).isoformat()
    placeholders = ",".join("?" for _ in members)
    params: list[object] = [*members, cutoff]
    exclude_clause = ""
    if exclude_run_id is not None:
        exclude_clause = " AND ai.run_id != ?"
        params.append(exclude_run_id)
    return list(
        conn.execute(
            f"""
            SELECT ai.summary, ai.priority, ai.deadline, rr.started_at
            FROM analysis_items ai
            JOIN review_runs rr ON rr.id = ai.run_id
            WHERE ai.action_required = 1
              AND ai.chat_id IN ({placeholders})
              AND ai.summary IS NOT NULL
              AND rr.started_at >= ?{exclude_clause}
            ORDER BY rr.started_at, ai.id
            """,
            params,
        ).fetchall()
    )


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


# --- voice-note transcription (#36) ----------------------------------------

def pending_transcriptions(
    conn: sqlite3.Connection, *, within_days: int, now: datetime | None = None
) -> list[sqlite3.Row]:
    """Voice notes awaiting transcription within the recent window, oldest-first.

    Rows whose audio was downloaded (``media_path`` set) and whose status is
    'pending' or 'failed' — a failed row retries on the next live scan — limited to
    the last ``within_days`` of send time. Older notes are handled by
    :func:`stale_voice_notes` so a first pairing never transcribes a long backlog.
    """
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=within_days)).isoformat()
    return list(
        conn.execute(
            "SELECT id, chat_id, source_message_id, message_timestamp, media_path "
            "FROM messages "
            "WHERE message_type = 'voice' AND media_path IS NOT NULL "
            "AND transcription_status IN ('pending', 'failed') AND message_timestamp >= ? "
            "ORDER BY message_timestamp, id",
            (cutoff,),
        ).fetchall()
    )


def stale_voice_notes(
    conn: sqlite3.Connection, *, within_days: int, now: datetime | None = None
) -> list[sqlite3.Row]:
    """Pending voice notes older than the window — to skip (not transcribe).

    The first-run backlog guard: a freshly paired device can surface years of
    voice notes, so we transcribe only the last ``within_days`` and skip the rest.
    Returns id + ``media_path`` so the caller can delete any already-downloaded
    audio before marking the row :func:`mark_transcription` 'skipped_old'.
    """
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=within_days)).isoformat()
    return list(
        conn.execute(
            "SELECT id, media_path FROM messages "
            "WHERE message_type = 'voice' "
            "AND transcription_status IN ('pending', 'failed') AND message_timestamp < ?",
            (cutoff,),
        ).fetchall()
    )


def _merge_placeholder(raw_json: str | None, placeholder_text: str | None) -> str:
    """Tuck a voice note's original ``[voice note]`` placeholder into raw_json.

    Preserves what the message held before transcription overwrote ``text``, so the
    original is recoverable. Tolerant of a NULL or non-dict legacy raw_json.
    """
    import json

    try:
        data = json.loads(raw_json) if raw_json else {}
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {"_raw": data}
    data["placeholder_text"] = placeholder_text
    return json.dumps(data, ensure_ascii=False)


def mark_transcription(
    conn: sqlite3.Connection, message_id: int, *, status: str, transcript: str | None = None
) -> None:
    """Record a voice note's transcription outcome on its existing message row.

    ``status='done'`` overwrites ``messages.text`` with ``transcript`` in place (the
    original placeholder is preserved under ``raw_json.placeholder_text``) so the
    analysis pipeline — which reads only ``text`` — treats it as a normal message,
    and clears the transient ``media_path`` (the audio file is deleted by the
    caller). ``'failed'`` leaves ``text`` and ``media_path`` intact so the next
    live scan retries. ``'skipped_old'`` just clears ``media_path``.
    """
    row = conn.execute(
        "SELECT text, raw_json FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return
    if status == "done" and transcript is not None:
        conn.execute(
            "UPDATE messages SET text = ?, transcription_status = 'done', "
            "media_path = NULL, raw_json = ? WHERE id = ?",
            (transcript, _merge_placeholder(row["raw_json"], row["text"]), message_id),
        )
    elif status == "skipped_old":
        conn.execute(
            "UPDATE messages SET transcription_status = 'skipped_old', media_path = NULL "
            "WHERE id = ?",
            (message_id,),
        )
    else:  # 'failed' (or any non-terminal state) — keep text + audio for retry
        conn.execute(
            "UPDATE messages SET transcription_status = ? WHERE id = ?",
            (status, message_id),
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
    transcriptions: int = 0,
) -> None:
    """Persist a run's funnel counters and final notification status."""
    conn.execute(
        "UPDATE review_runs SET chats_synced = ?, messages_synced = ?, chats_monitored = ?, "
        "stage1_passed = ?, stage2_llm_calls = ?, transcriptions = ?, actionable = ?, "
        "notification_status = ? WHERE id = ?",
        (
            chats_synced,
            messages_synced,
            chats_monitored,
            stage1_passed,
            stage2_llm_calls,
            transcriptions,
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
    deadline_date: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO analysis_items
            (run_id, chat_id, action_required, priority, summary,
             suggested_next_action, deadline, deadline_date, confidence,
             evidence_message_ids_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            chat_id,
            1 if action_required else 0,
            priority,
            summary,
            suggested_next_action,
            deadline,
            deadline_date,
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
    messages_json: str | None,
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
            (run_id, chat_id, input_message_ids_json, input_text, messages_json,
             stage1_passed, stage1_roots_json, llm_called, llm_system_prompt,
             llm_user_prompt, llm_raw_response, parsed_result_json, final_action,
             telegram_text, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            chat_id,
            input_message_ids_json,
            input_text,
            messages_json,
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
    which is what the Dashboard's per-channel table shows. Linked child chats are
    excluded from the monitored view so a family that is one person isn't listed
    twice; the child's messages remain on its own row in the all-chats view.

    The count and last-message time are computed over the chat's whole **family**
    — itself plus any linked children — so a monitored parent's row represents the
    merged family (matching ``chats_overview`` and the Chats tab), not just its own
    messages. For an unlinked/standalone chat the family is just itself, so counts
    and ordering are unchanged.
    """
    where = (
        "WHERE c.status = 'monitored' AND c.parent_chat_id IS NULL" if monitored_only else ""
    )
    family = (
        "m.chat_id IN (SELECT x.id FROM chats x WHERE x.id = c.id OR x.parent_chat_id = c.id)"
    )
    return list(
        conn.execute(
            "SELECT c.id, c.display_name, c.status, "
            f"(SELECT MAX(m.message_timestamp) FROM messages m WHERE {family}) AS last_message_at, "
            f"(SELECT COUNT(*) FROM messages m WHERE {family}) AS message_count "
            f"FROM chats c {where} "
            "ORDER BY last_message_at IS NULL, last_message_at DESC, c.id"
        ).fetchall()
    )


# --- chats & config tab (read-only listing + bounded history) --------------
# Powers the Chats & Config tab (#10). Listing and history are SELECT-only; the
# tab's only writes go through set_chat_status / baseline_cursor (above).

def chats_overview(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All chats with their status, message count, and latest message preview.

    Columns: id, source_chat_id, display_name, chat_type, status,
    last_message_at, message_count, last_message_text. The count, latest time, and
    preview are computed over the chat's whole **family** — itself plus any linked
    children — so a parent row represents the merged family, not just its own
    messages (a child's newer message correctly floats the parent to the top and
    sets the preview). The family set is ``c`` plus chats whose ``parent_chat_id``
    is ``c``; for a standalone or child chat that is just itself. Most recently
    active first (NULLs last) so the operator's live chats surface at the top.
    """
    family = (
        "m.chat_id IN (SELECT x.id FROM chats x WHERE x.id = c.id OR x.parent_chat_id = c.id)"
    )
    return list(
        conn.execute(
            "SELECT c.id, c.source, c.source_chat_id, c.display_name, c.alias, c.chat_type, "
            "c.status, c.parent_chat_id, "
            f"(SELECT COUNT(*) FROM messages m WHERE {family}) AS message_count, "
            f"(SELECT MAX(m.message_timestamp) FROM messages m WHERE {family}) AS last_message_at, "
            f"(SELECT m.text FROM messages m WHERE {family} "
            " ORDER BY m.message_timestamp DESC, m.id DESC LIMIT 1) AS last_message_text "
            "FROM chats c "
            "ORDER BY last_message_at IS NULL, last_message_at DESC, c.id"
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
    return recent_messages_family(
        conn, [chat_id], limit=limit, before_ts=before_ts, before_id=before_id
    )


def recent_messages_family(
    conn: sqlite3.Connection,
    chat_ids: list[int],
    *,
    limit: int = 100,
    before_ts: str | None = None,
    before_id: int | None = None,
) -> tuple[list[StoredMessage], bool]:
    """A page of messages across one or more chats — the merged family history.

    Same paging contract as :func:`recent_messages` (newest→oldest internally,
    returned oldest→newest, ``has_more`` flag), but over a *set* of chat ids so a
    parent's overlay shows a time-ordered merge of itself and its linked children.
    The ``(message_timestamp, id)`` key is a global total order across chats, so a
    single cursor pages the whole family correctly. Each :class:`StoredMessage`
    keeps its ``chat_id`` so callers can attribute every message to its origin.
    """
    if not chat_ids:
        return [], False
    placeholders = ",".join("?" for _ in chat_ids)
    if before_ts is not None and before_id is not None:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            f"WHERE chat_id IN ({placeholders}) AND (message_timestamp < ? OR "
            "(message_timestamp = ? AND id < ?)) "
            "ORDER BY message_timestamp DESC, id DESC LIMIT ?",
            (*chat_ids, before_ts, before_ts, before_id, limit + 1),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            f"WHERE chat_id IN ({placeholders}) ORDER BY message_timestamp DESC, id DESC LIMIT ?",
            (*chat_ids, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return [_to_stored(r) for r in reversed(rows)], has_more


def get_chat(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    """Return a single chat row by internal id, or None if it doesn't exist."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, source, source_chat_id, display_name, alias, chat_type, status, "
        "last_message_at, parent_chat_id FROM chats WHERE id = ?",
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
            "stage1_passed, stage2_llm_calls, transcriptions, actionable, "
            "notification_status, error "
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
    """Operator-set state worth preserving across a rebuild: status, alias, link.

    Returns (source_chat_id, status, alias, parent_source_chat_id) for every chat
    the operator has touched — anything not in the default 'discovered'/no-alias/
    unlinked resting state. The parent is captured by *its* ``source_chat_id`` (not
    internal id, which the rebuild reassigns) so the parent↔child link can be
    re-resolved after re-ingest. A linked child with otherwise-default state is
    still included because of its ``parent_chat_id``.
    """
    return list(
        conn.execute(
            "SELECT c.source_chat_id, c.status, c.alias, "
            "p.source_chat_id AS parent_source_chat_id "
            "FROM chats c LEFT JOIN chats p ON p.id = c.parent_chat_id "
            "WHERE c.status != 'discovered' OR c.alias IS NOT NULL "
            "OR c.parent_chat_id IS NOT NULL"
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
