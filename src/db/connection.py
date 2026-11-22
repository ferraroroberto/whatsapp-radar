"""SQLite connection, schema bootstrap, and migration.

The single entry point (:func:`connect`) plus the shared helpers other
``src/db`` submodules use internally (``_now``, ``_rowid``, ``_to_stored``,
``_MESSAGE_COLUMNS``). Kept separate from the table-scoped submodules
(``chats``, ``messages``, ``runs``, ``dashboard``, ``sync_log``,
``reprocess_support``) because every one of them needs this, not the other
way around.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from src.models import StoredMessage
from src.runtime_files import checked, make_dirs

_MESSAGE_COLUMNS = (
    "id, chat_id, source_message_id, message_timestamp, text, sender_label, "
    "message_type, transcription_status, media_path, summary, task_exported_at, raw_json"
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
        summary=row["summary"],
        task_exported_at=row["task_exported_at"],
        raw=json.loads(row["raw_json"]) if row["raw_json"] else {},
    )

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Columns added after each table's initial schema. `CREATE TABLE IF NOT EXISTS`
# never backfills columns on a pre-existing table, so an older on-disk DB is
# missing them. These additive, non-destructive ALTERs bring it up to date — each
# has a constant default (or is nullable), which SQLite allows. Old rows stay
# NULL / take the default; a new additive column is one more tuple here.
_REVIEW_RUNS_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("mode", "TEXT NOT NULL DEFAULT 'review'"),
    ("params_json", "TEXT"),
    ("kind", "TEXT NOT NULL DEFAULT 'scan'"),
    ("summary_json", "TEXT"),
    ("chats_synced", "INTEGER NOT NULL DEFAULT 0"),
    ("messages_synced", "INTEGER NOT NULL DEFAULT 0"),
    ("chats_monitored", "INTEGER NOT NULL DEFAULT 0"),
    ("stage1_passed", "INTEGER NOT NULL DEFAULT 0"),
    ("stage2_llm_calls", "INTEGER NOT NULL DEFAULT 0"),
    ("transcriptions", "INTEGER NOT NULL DEFAULT 0"),
    ("actionable", "INTEGER NOT NULL DEFAULT 0"),
    ("notification_status", "TEXT"),
    ("source_funnel_json", "TEXT"),
    ("owner_pid", "INTEGER"),
)

# `alias`: operator override label. `parent_chat_id`: the parent↔child link.
# `source`: which connector owns the chat (existing rows are WhatsApp).
_CHATS_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("alias", "TEXT"),
    ("parent_chat_id", "INTEGER REFERENCES chats(id) ON DELETE SET NULL"),
    ("source", "TEXT NOT NULL DEFAULT 'whatsapp'"),
)

# `messages_json`: per-message audit record (id/sender/text and the Stage-1 keyword
# roots each matched); old rows stay NULL and the audit view falls back to the
# rendered `input_text` blob. `stage1_buckets_json`: the Stage-1 bucket tally.
_ANALYSIS_TRACE_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("messages_json", "TEXT"),
    ("stage1_buckets_json", "TEXT"),
)

# `deadline_date`: the model-resolved absolute date beside the free-text
# `deadline`, so the digest flags today/overdue deterministically.
# `child`/`task_category`/`prep_complexity`: resolved Gmail-school classification
# (NULL on every non-Gmail-school row). `calendar_event_id`: the created Google
# Calendar reminder event id for a routine-prep item (NULL when none was made).
_ANALYSIS_ITEMS_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("deadline_date", "TEXT"),
    ("child", "TEXT"),
    ("task_category", "TEXT"),
    ("prep_complexity", "TEXT"),
    ("calendar_event_id", "TEXT"),
)

# `transcription_status` + `media_path`: the voice-note transcription lifecycle and
# a transient ref to the downloaded audio (NULL on every non-voice message, so the
# analysis pipeline — which reads only `text` — is untouched). `summary`: the
# persisted on-demand summary, read-through cached so a reopened overlay never
# re-pays the hub call. `task_exported_at`: when a message was sent to task-os's
# Inbox.
_MESSAGES_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("transcription_status", "TEXT"),
    ("media_path", "TEXT"),
    ("summary", "TEXT"),
    ("task_exported_at", "TEXT"),
)

# Multi-source sync visibility: the ``source`` operation tag (scan/resync/reprocess)
# stays, and these add which connector ran plus its outcome. The defaults make every
# older row a successful WhatsApp sync.
_SYNC_LOG_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("connector_source", "TEXT NOT NULL DEFAULT 'whatsapp'"),
    ("status", "TEXT NOT NULL DEFAULT 'success'"),
    ("detail", "TEXT NOT NULL DEFAULT ''"),
)


def _now() -> str:
    # Seconds precision, explicit +00:00 offset: unambiguous AND parseable by
    # every browser Date() (6-digit microsecond fractions are not — the UI's
    # fallback then renders raw UTC digits as if local).
    return datetime.now(UTC).isoformat(timespec="seconds")


def _rowid(cur: sqlite3.Cursor) -> int:
    """Return a cursor's last inserted rowid, asserting it exists (for type-checkers)."""
    assert cur.lastrowid is not None
    return cur.lastrowid


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating parent dirs) and migrate a database, returning the connection."""
    make_dirs(db_path.parent)
    # runtime-write-exempt: caller-supplied db path; checked() raises when it is under a code folder
    conn = sqlite3.connect(checked(db_path))
    conn.row_factory = sqlite3.Row
    # WAL + NORMAL keeps the many small commits in the review/ingest paths fast
    # while staying durable enough for a local single-writer store.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(
    conn: sqlite3.Connection, table: str, added: tuple[tuple[str, str], ...]
) -> set[str]:
    """ALTER in every ``(name, declaration)`` absent from ``table``; return the prior columns.

    The returned set is what the table held *before* this call, so a caller can
    gate a one-time backfill on a column having just landed.
    """
    existing = _columns(conn, table)
    for name, declaration in added:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    return existing


def _migrate(conn: sqlite3.Connection) -> None:
    """Additively backfill columns missing from an older on-disk schema.

    Idempotent: only adds a column when absent, so repeated opens are no-ops.
    """
    _migrate_review_runs(conn)
    _migrate_chats(conn)
    _add_missing_columns(conn, "analysis_trace", _ANALYSIS_TRACE_ADDED_COLUMNS)
    _add_missing_columns(conn, "analysis_items", _ANALYSIS_ITEMS_ADDED_COLUMNS)
    _add_missing_columns(conn, "messages", _MESSAGES_ADDED_COLUMNS)
    _add_missing_columns(conn, "sync_log", _SYNC_LOG_ADDED_COLUMNS)


def _migrate_review_runs(conn: sqlite3.Connection) -> None:
    existing = _add_missing_columns(conn, "review_runs", _REVIEW_RUNS_ADDED_COLUMNS)
    # One-time backfill when `kind` first lands on an older DB: rows the
    # legacy review verb wrote (mode='review') are process runs, not scans.
    # Idempotent — new process runs are written with kind='process' directly.
    if "kind" not in existing:
        conn.execute("UPDATE review_runs SET kind = 'process' WHERE mode = 'review'")
    # Created here, not in schema.sql, because that script runs before this
    # migration adds `kind` to an older table. It lets the per-kind "newest runs"
    # window read each kind in id order instead of sorting the whole table; the
    # chatty traffic-check kind dominates its size.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_runs_kind_id ON review_runs (kind, id DESC)"
    )


def _migrate_chats(conn: sqlite3.Connection) -> None:
    _add_missing_columns(conn, "chats", _CHATS_ADDED_COLUMNS)
    # `chats.source` lets a second connector (Gmail) share these tables:
    # chat identity becomes (source, source_chat_id). Existing rows backfill to
    # 'whatsapp' via the column default. SQLite can't add a composite table
    # constraint or drop the legacy column-level UNIQUE(source_chat_id) after the
    # fact without a full table rebuild, so composite uniqueness is enforced by a
    # unique *index* instead. The legacy single-column UNIQUE stays — harmless for
    # whatsapp-only rows and dropped only if a future rebuild is ever warranted.
    # The fresh schema (schema.sql) declares the
    # composite as a table constraint; both forms back the same ON CONFLICT target.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_chats_source_key "
        "ON chats(source, source_chat_id)"
    )
