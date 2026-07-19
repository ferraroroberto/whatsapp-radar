"""Message ingestion, cursoring, voice-note transcription, and message reads.

Covers the messages table end to end: idempotent ingest, the per-chat/per-family
review cursor and delta, the #36 voice-note transcription lifecycle, and the
message-table read helpers (paging, counts) that originated under the old
"chats & config tab" and "dashboard aggregates" banners in the pre-split
``store.py`` — grouped here because they all read the same table, not the tab
that first consumed them.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from src.db.chats import family_member_ids
from src.db.connection import _MESSAGE_COLUMNS, _now, _to_stored
from src.models import MessageRecord, StoredMessage


def message_source_ids(conn: sqlite3.Connection, chat_id: int) -> set[str]:
    """Source message ids already stored for a chat.

    The known-ids oracle for incremental ingest (#180): a connector that can
    diff against these downloads full content only for genuinely new messages.
    """
    rows = conn.execute(
        "SELECT source_message_id FROM messages WHERE chat_id = ?",
        (chat_id,),
    ).fetchall()
    return {row["source_message_id"] for row in rows}


def insert_message(conn: sqlite3.Connection, chat_id: int, msg: MessageRecord) -> bool:
    """Insert a message idempotently. Returns True if a new row was created.

    Delegates to :func:`insert_messages` so there is one column list (and one
    idempotent-insert SQL statement) to maintain between the single-message and
    batch paths — the single-message call just pays its own commit.
    """
    return insert_messages(conn, chat_id, [msg]) > 0


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
    conn: sqlite3.Connection,
    *,
    within_days: int,
    failed_within_days: int | None = None,
    now: datetime | None = None,
) -> list[sqlite3.Row]:
    """Voice notes awaiting transcription, oldest-first.

    Rows whose audio was downloaded (``media_path`` set) and that still need a
    transcript, split by status so a transient failure isn't treated as backlog (#104):

    - 'pending' (never attempted) is gated by ``within_days`` — the first-pairing
      backlog guard, so a fresh device never transcribes years of notes.
    - 'failed' (we tried, it errored — e.g. the whisper backend was down) retries on
      every live scan up to ``failed_within_days``, *regardless* of ``within_days``, so
      an outage that outlasts the transcribe window still recovers. Defaults to
      ``within_days`` when not given (legacy behaviour: both gated identically).

    Older notes of each kind are handled by :func:`stale_voice_notes`.
    """
    base = now or datetime.now(UTC)
    failed_within_days = within_days if failed_within_days is None else failed_within_days
    pending_cutoff = (base - timedelta(days=within_days)).isoformat()
    failed_cutoff = (base - timedelta(days=failed_within_days)).isoformat()
    return list(
        conn.execute(
            "SELECT id, chat_id, source_message_id, message_timestamp, media_path "
            "FROM messages "
            "WHERE message_type = 'voice' AND media_path IS NOT NULL "
            "AND ((transcription_status = 'pending' AND message_timestamp >= ?) "
            "     OR (transcription_status = 'failed' AND message_timestamp >= ?)) "
            "ORDER BY message_timestamp, id",
            (pending_cutoff, failed_cutoff),
        ).fetchall()
    )


def stale_voice_notes(
    conn: sqlite3.Connection,
    *,
    within_days: int,
    failed_within_days: int | None = None,
    now: datetime | None = None,
) -> list[sqlite3.Row]:
    """Voice notes too old to (keep) transcribing — to skip and drop audio for.

    Two backlog guards, by status (#104):

    - 'pending' (never attempted) older than ``within_days`` — the first-run guard so
      a freshly paired device doesn't transcribe years of notes.
    - 'failed' (already retried) older than ``failed_within_days`` — give up on a note
      whose backend outage never recovered, so its retained audio isn't kept forever.
      Defaults to ``within_days`` (legacy behaviour) when not given.

    Returns id + ``media_path`` so the caller can delete any already-downloaded audio
    before marking the row :func:`mark_transcription` 'skipped_old'.
    """
    base = now or datetime.now(UTC)
    failed_within_days = within_days if failed_within_days is None else failed_within_days
    pending_cutoff = (base - timedelta(days=within_days)).isoformat()
    failed_cutoff = (base - timedelta(days=failed_within_days)).isoformat()
    return list(
        conn.execute(
            "SELECT id, media_path FROM messages "
            "WHERE message_type = 'voice' "
            "AND ((transcription_status = 'pending' AND message_timestamp < ?) "
            "     OR (transcription_status = 'failed' AND message_timestamp < ?))",
            (pending_cutoff, failed_cutoff),
        ).fetchall()
    )


def expired_retained_audio(
    conn: sqlite3.Connection, *, retain_days: int, now: datetime | None = None
) -> list[sqlite3.Row]:
    """Transcribed voice notes whose retained audio is past the retention window.

    The retention sweep (#86): rows already transcribed ('done') whose audio is
    still on disk (``media_path`` set) and whose send time is older than
    ``retain_days``. The caller deletes each file and calls :func:`clear_media_path`
    so the audio leaves disk while the transcript and 'done' status remain. Status
    is untouched, so the cursor barrier is unaffected.
    """
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=retain_days)).isoformat()
    return list(
        conn.execute(
            "SELECT id, media_path FROM messages "
            "WHERE transcription_status = 'done' AND media_path IS NOT NULL "
            "AND message_timestamp < ?",
            (cutoff,),
        ).fetchall()
    )


def clear_media_path(conn: sqlite3.Connection, message_id: int) -> None:
    """Forget a message's retained audio path (after its file is swept). #86.

    Only clears ``media_path``; ``transcription_status`` and ``text`` are left as
    they are, so a swept 'done' note keeps its transcript and simply loses playback.
    """
    conn.execute("UPDATE messages SET media_path = NULL WHERE id = ?", (message_id,))
    conn.commit()


def voice_audio_path(conn: sqlite3.Connection, message_id: int) -> str | None:
    """Relative audio path for a voice message, or ``None`` if there is no audio.

    Returns the retained ``media_path`` only for a ``message_type = 'voice'`` row
    whose audio is still on disk (#86 playback endpoint). ``None`` for a missing
    message, a non-voice message, or a note whose audio was never downloaded or has
    been swept — the caller maps that to a clean 404.
    """
    row = conn.execute(
        "SELECT media_path FROM messages WHERE id = ? AND message_type = 'voice'",
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    media_path = row["media_path"]
    return str(media_path) if media_path is not None else None


def message_summary_context(
    conn: sqlite3.Connection, message_id: int
) -> tuple[str, str | None, str | None] | None:
    """``(text, sender_label, stored_summary)`` for the summarize/speak endpoints
    (#86, #157), or ``None`` if the message is missing or has no (non-blank) text
    (e.g. an untranscribed voice note) — the caller maps that to a clean 404.

    One query for both endpoints: the summarize endpoint reads through
    ``stored_summary`` before dialling the hub, and the speak endpoint needs
    ``sender_label`` (voice-gender lookup) and the original ``text`` (language
    detection) alongside whatever summary is already stored.
    """
    row = conn.execute(
        "SELECT text, sender_label, summary FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return None
    text = row["text"]
    if text is None or not str(text).strip():
        return None
    return str(text), row["sender_label"], row["summary"]


def set_message_summary(conn: sqlite3.Connection, message_id: int, summary: str) -> None:
    """Persist the on-demand summary for a message (#157, read-through cache)."""
    conn.execute("UPDATE messages SET summary = ? WHERE id = ?", (summary, message_id))
    conn.commit()


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
    conn: sqlite3.Connection,
    message_id: int,
    *,
    status: str,
    transcript: str | None = None,
    keep_media: bool = False,
) -> None:
    """Record a voice note's transcription outcome on its existing message row.

    ``status='done'`` overwrites ``messages.text`` with ``transcript`` in place (the
    original placeholder is preserved under ``raw_json.placeholder_text``) so the
    analysis pipeline — which reads only ``text`` — treats it as a normal message.
    Also clears any persisted ``summary`` (#157): once the underlying text changes
    a prior summary is stale and must never be shown or spoken again. With
    ``keep_media=False`` (the default) it also clears the transient ``media_path``
    and the caller deletes the audio (#36). With ``keep_media=True`` the audio is
    retained for playback (#86): ``media_path`` keeps pointing at the file and a
    later retention sweep deletes it. A retained ``done`` row never trips the
    cursor barrier, which only holds 'pending'/'failed' notes. ``'failed'`` leaves
    ``text`` and ``media_path`` intact so the next live scan retries. ``'skipped_old'``
    just clears ``media_path``.
    """
    row = conn.execute(
        "SELECT text, raw_json FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return
    if status == "done" and transcript is not None:
        media_clause = "" if keep_media else "media_path = NULL, "
        conn.execute(
            f"UPDATE messages SET text = ?, transcription_status = 'done', "
            f"{media_clause}summary = NULL, raw_json = ? WHERE id = ?",
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


# --- message reads (paging + counts) ----------------------------------------
# Originated under the old "chats & config tab" / "dashboard aggregates"
# banners — grouped here because they read only the messages table.

def message_count_total(conn: sqlite3.Connection) -> int:
    """Total stored messages across every chat (monitored or not)."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"])


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


def count_messages_since(conn: sqlite3.Connection, ingested_after: str) -> int:
    """Messages ingested strictly after an ISO timestamp (the unscanned backlog)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE ingested_at > ?", (ingested_after,)
    ).fetchone()
    return int(row["n"])
