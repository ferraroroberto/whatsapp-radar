"""SQLite connection, schema bootstrap, and migration.

The single entry point (:func:`connect`) plus the shared helpers other
``src/db`` submodules use internally (``_now``, ``_rowid``, ``_to_stored``,
``_MESSAGE_COLUMNS``). Kept separate from the table-scoped submodules
(``chats``, ``messages``, ``runs``, ``dashboard``, ``sync_log``,
``reprocess_support``) because every one of them needs this, not the other
way around.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from src.models import StoredMessage

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
    if "stage1_buckets_json" not in trace_cols:
        conn.execute("ALTER TABLE analysis_trace ADD COLUMN stage1_buckets_json TEXT")
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
    # Multi-source sync visibility (#58): keep the historical ``source``
    # operation tag (scan/resync/reprocess) and add which connector ran plus its
    # outcome. Defaults make every pre-#58 row a successful WhatsApp sync.
    sync_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sync_log)")}
    if "connector_source" not in sync_cols:
        conn.execute(
            "ALTER TABLE sync_log ADD COLUMN connector_source "
            "TEXT NOT NULL DEFAULT 'whatsapp'"
        )
    if "status" not in sync_cols:
        conn.execute(
            "ALTER TABLE sync_log ADD COLUMN status TEXT NOT NULL DEFAULT 'success'"
        )
    if "detail" not in sync_cols:
        conn.execute("ALTER TABLE sync_log ADD COLUMN detail TEXT NOT NULL DEFAULT ''")
