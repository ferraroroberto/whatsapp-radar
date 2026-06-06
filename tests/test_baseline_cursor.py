"""Baselining a newly-monitored chat skips its backlog on the first review."""

from __future__ import annotations

import sqlite3

from src.db import store

from tests.helpers import append_message, chat_id_by_source


def test_baseline_skips_backlog_then_reviews_only_new(ingested_conn: sqlite3.Connection) -> None:
    conn = ingested_conn
    # Pick a fixture chat that has messages.
    source_chat_id = conn.execute("SELECT source_chat_id FROM chats LIMIT 1").fetchone()[0]
    chat_id = chat_id_by_source(conn, source_chat_id)

    backlog = store.messages_since_cursor(conn, chat_id)
    assert backlog, "fixture chat should have a backlog to baseline past"

    assert store.baseline_cursor(conn, chat_id) is True
    assert store.messages_since_cursor(conn, chat_id) == []  # backlog skipped

    # Baselining again is a no-op (cursor already exists).
    assert store.baseline_cursor(conn, chat_id) is False

    # A genuinely new message after the baseline is the only thing reviewed.
    append_message(conn, source_chat_id, "new-after-baseline", "please pay the deadline")
    delta = store.messages_since_cursor(conn, chat_id)
    assert [m.source_message_id for m in delta] == ["new-after-baseline"]


def test_baseline_no_messages_is_noop(conn: sqlite3.Connection) -> None:
    from src.models import ChatRecord

    chat_id = store.upsert_chat(conn, ChatRecord("empty@g.us", "Empty Group"))
    assert store.baseline_cursor(conn, chat_id) is False
