"""Opening a pre-#7 database backfills the review_runs funnel columns.

A DB created by the original spike has a slimmer review_runs table. store.connect
must migrate it additively so the Dashboard (and #7's funnel) can read it without
crashing — and without losing the existing rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.db import store

_LEGACY_REVIEW_RUNS = """
CREATE TABLE review_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    completed_at   TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    chats_reviewed INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);
"""

_LEGACY_ANALYSIS_TRACE = """
CREATE TABLE analysis_trace (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                 INTEGER NOT NULL,
    chat_id                INTEGER NOT NULL,
    input_message_ids_json TEXT,
    input_text             TEXT,
    stage1_passed          INTEGER NOT NULL DEFAULT 0,
    stage1_roots_json      TEXT,
    llm_called             INTEGER NOT NULL DEFAULT 0,
    llm_system_prompt      TEXT,
    llm_user_prompt        TEXT,
    llm_raw_response       TEXT,
    parsed_result_json     TEXT,
    final_action           TEXT NOT NULL,
    telegram_text          TEXT,
    error                  TEXT,
    created_at             TEXT NOT NULL,
    UNIQUE (run_id, chat_id)
);
"""

_LEGACY_ANALYSIS_ITEMS = """
CREATE TABLE analysis_items (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                    INTEGER NOT NULL,
    chat_id                   INTEGER NOT NULL,
    action_required           INTEGER NOT NULL,
    priority                  TEXT,
    summary                   TEXT,
    suggested_next_action     TEXT,
    deadline                  TEXT,
    confidence                REAL,
    evidence_message_ids_json TEXT,
    created_at                TEXT NOT NULL
);
"""

_LEGACY_CHATS = """
CREATE TABLE chats (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_chat_id TEXT NOT NULL UNIQUE,
    display_name   TEXT NOT NULL,
    chat_type      TEXT NOT NULL DEFAULT 'group',
    status         TEXT NOT NULL DEFAULT 'discovered',
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    last_message_at TEXT
);
"""


def test_connect_migrates_legacy_review_runs(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(_LEGACY_REVIEW_RUNS)
    raw.execute(
        "INSERT INTO review_runs (started_at, status, chats_reviewed) "
        "VALUES ('2026-01-01T00:00:00+00:00', 'completed', 2)"
    )
    raw.commit()
    raw.close()

    conn = store.connect(db)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(review_runs)")}
        assert {"mode", "messages_synced", "actionable", "notification_status"} <= cols

        # The pre-existing row survives and reads with sane defaults.
        assert store.count_runs(conn) == 1
        last = store.last_run(conn)
        assert last is not None
        assert last["mode"] == "review"  # column default applied to the old row
        assert last["actionable"] == 0

        # Idempotent: a second open is a no-op.
        conn.close()
        conn2 = store.connect(db)
        assert store.count_runs(conn2) == 1
        conn2.close()
    finally:
        conn.close()


def test_connect_migrates_analysis_trace_messages_json(tmp_path: Path) -> None:
    db = tmp_path / "legacy_trace.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(_LEGACY_ANALYSIS_TRACE)
    raw.execute(
        "INSERT INTO analysis_trace (run_id, chat_id, final_action, created_at) "
        "VALUES (1, 1, 'not_actionable', '2026-01-01T00:00:00+00:00')"
    )
    raw.commit()
    raw.close()

    conn = store.connect(db)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(analysis_trace)")}
        assert "messages_json" in cols
        # The pre-existing row survives with a NULL per-message record (the audit
        # view falls back to the rendered input blob for it).
        row = conn.execute("SELECT messages_json FROM analysis_trace WHERE id = 1").fetchone()
        assert row["messages_json"] is None
    finally:
        conn.close()


_LEGACY_MESSAGES = """
CREATE TABLE chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_chat_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL, chat_type TEXT NOT NULL DEFAULT 'group',
    status TEXT NOT NULL DEFAULT 'discovered', first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL, last_message_at TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    source_message_id TEXT NOT NULL,
    sender_label TEXT,
    message_timestamp TEXT NOT NULL,
    text TEXT,
    message_type TEXT NOT NULL DEFAULT 'text',
    raw_json TEXT,
    ingested_at TEXT NOT NULL,
    UNIQUE (chat_id, source_message_id)
);
"""


def test_connect_migrates_messages_transcription_columns(tmp_path: Path) -> None:
    """A pre-#36 messages table gains transcription_status + media_path; rows stay NULL."""
    db = tmp_path / "legacy_messages.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(_LEGACY_MESSAGES)
    raw.execute(
        "INSERT INTO chats (source_chat_id, display_name, first_seen_at, last_seen_at) "
        "VALUES ('g1', 'Class 4A', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO messages (chat_id, source_message_id, message_timestamp, text, ingested_at) "
        "VALUES (1, 'm1', '2026-01-01T00:00:00+00:00', 'hi', '2026-01-01T00:00:00+00:00')"
    )
    raw.commit()
    raw.close()

    conn = store.connect(db)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        assert {"transcription_status", "media_path"} <= cols
        # The pre-existing row survives with NULLs (a normal non-voice message).
        row = conn.execute(
            "SELECT transcription_status, media_path FROM messages WHERE id = 1"
        ).fetchone()
        assert row["transcription_status"] is None
        assert row["media_path"] is None
    finally:
        conn.close()


def test_connect_migrates_review_runs_transcriptions(tmp_path: Path) -> None:
    """A pre-#36 review_runs gains the transcriptions counter, defaulting to 0."""
    db = tmp_path / "legacy_runs_tr.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(_LEGACY_REVIEW_RUNS)
    raw.execute(
        "INSERT INTO review_runs (started_at, status, chats_reviewed) "
        "VALUES ('2026-01-01T00:00:00+00:00', 'completed', 1)"
    )
    raw.commit()
    raw.close()

    conn = store.connect(db)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(review_runs)")}
        assert "transcriptions" in cols
        last = store.last_run(conn)
        assert last is not None and last["transcriptions"] == 0
    finally:
        conn.close()


def test_connect_migrates_analysis_items_deadline_date(tmp_path: Path) -> None:
    """A pre-#71 analysis_items table gains `deadline_date`; old rows stay NULL."""
    db = tmp_path / "legacy_items.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(_LEGACY_ANALYSIS_ITEMS)
    raw.execute(
        "INSERT INTO analysis_items (run_id, chat_id, action_required, summary, created_at) "
        "VALUES (1, 1, 1, 'Pay the fee', '2026-01-01T00:00:00+00:00')"
    )
    raw.commit()
    raw.close()

    conn = store.connect(db)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(analysis_items)")}
        assert "deadline_date" in cols
        # The pre-existing row survives with a NULL resolved date and accepts a new one.
        row = conn.execute("SELECT deadline_date FROM analysis_items WHERE id = 1").fetchone()
        assert row["deadline_date"] is None
        item_id = store.insert_analysis_item(
            conn, 1, 1,
            action_required=True, priority="high", summary="Trip tomorrow",
            suggested_next_action="Pack", deadline="tomorrow", deadline_date="2026-06-09",
            confidence=0.9, evidence_message_ids_json="[]",
        )
        got = conn.execute(
            "SELECT deadline_date FROM analysis_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert got["deadline_date"] == "2026-06-09"
    finally:
        conn.close()


def test_connect_migrates_chats_source(tmp_path: Path) -> None:
    """A pre-#57 chats table gains `source`, backfills to 'whatsapp', stays idempotent."""
    db = tmp_path / "legacy_source.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(_LEGACY_CHATS)
    raw.execute(
        "INSERT INTO chats (source_chat_id, display_name, first_seen_at, last_seen_at) "
        "VALUES ('g1', 'Class 4A Group', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    raw.commit()
    raw.close()

    conn = store.connect(db)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(chats)")}
        assert "source" in cols
        # The pre-existing row backfills to 'whatsapp' via the column default.
        chat = store.get_chat(conn, 1)
        assert chat is not None and chat["source"] == "whatsapp"
        # The composite-uniqueness index is in place and reachable by the default source.
        idx = {row["name"] for row in conn.execute("PRAGMA index_list(chats)")}
        assert "idx_chats_source_key" in idx
        assert store.chat_id_for_source(conn, "g1") == 1
        assert store.chat_id_for_source(conn, "g1", source="gmail") is None

        # Idempotent: a second open neither errors nor duplicates the row.
        conn.close()
        conn2 = store.connect(db)
        assert store.count_chats(conn2) == 1
        assert store.get_chat(conn2, 1)["source"] == "whatsapp"
        conn2.close()
    finally:
        conn.close()


def test_fresh_db_allows_same_source_chat_id_across_sources(tmp_path: Path) -> None:
    """On a fresh schema, identity is composite: a Gmail id may equal a WhatsApp JID."""
    from src.models import ChatRecord

    conn = store.connect(tmp_path / "fresh.sqlite3")
    try:
        wa = store.upsert_chat(conn, ChatRecord("shared-id", "WhatsApp Group"))
        gm = store.upsert_chat(conn, ChatRecord("shared-id", "Gmail Thread", source="gmail"))
        assert wa != gm
        assert store.count_chats(conn) == 2
        assert store.chat_id_for_source(conn, "shared-id") == wa
        assert store.chat_id_for_source(conn, "shared-id", source="gmail") == gm
        # Re-upserting the same identity updates in place — no third row.
        again = store.upsert_chat(conn, ChatRecord("shared-id", "WhatsApp Group v2"))
        assert again == wa
        assert store.count_chats(conn) == 2
    finally:
        conn.close()


def test_connect_migrates_chats_alias(tmp_path: Path) -> None:
    db = tmp_path / "legacy_chats.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(_LEGACY_CHATS)
    raw.execute(
        "INSERT INTO chats (source_chat_id, display_name, first_seen_at, last_seen_at) "
        "VALUES ('g1', 'Class 4A Group', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    raw.commit()
    raw.close()

    conn = store.connect(db)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(chats)")}
        assert "alias" in cols
        # The pre-existing row survives with a NULL alias and accepts a new one.
        chat = store.get_chat(conn, 1)
        assert chat is not None and chat["alias"] is None
        assert store.set_chat_alias(conn, 1, "Tom") is True
        assert store.get_chat(conn, 1)["alias"] == "Tom"
    finally:
        conn.close()
