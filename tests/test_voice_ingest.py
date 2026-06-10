"""Voice-note ingest: transcription_status and media_path from connector raw."""

from __future__ import annotations

import sqlite3

from src.db import store
from src.models import ChatRecord, MessageRecord


def _chat(conn: sqlite3.Connection) -> int:
    return store.upsert_chat(
        conn,
        ChatRecord(source_chat_id="test@g.us", display_name="Test Group", chat_type="group"),
    )


def test_voice_with_media_path_is_pending(conn: sqlite3.Connection) -> None:
    chat_id = _chat(conn)
    store.insert_message(
        conn,
        chat_id,
        MessageRecord(
            source_message_id="V1",
            message_timestamp="2026-06-10T10:00:00+00:00",
            text="[voice note]",
            message_type="voice",
            raw={"media_path": "media/V1.ogg", "ptt": True},
        ),
    )
    row = conn.execute(
        "SELECT transcription_status, media_path FROM messages WHERE source_message_id = 'V1'"
    ).fetchone()
    assert row["transcription_status"] == "pending"
    assert row["media_path"] == "media/V1.ogg"


def test_voice_without_media_is_failed(conn: sqlite3.Connection) -> None:
    chat_id = _chat(conn)
    store.insert_message(
        conn,
        chat_id,
        MessageRecord(
            source_message_id="V2",
            message_timestamp="2026-06-10T10:01:00+00:00",
            text="[voice note]",
            message_type="voice",
            raw={"placeholder_text": "[voice note]"},
        ),
    )
    row = conn.execute(
        "SELECT transcription_status, media_path FROM messages WHERE source_message_id = 'V2'"
    ).fetchone()
    assert row["transcription_status"] == "failed"
    assert row["media_path"] is None


def test_text_message_transcription_status_none(conn: sqlite3.Connection) -> None:
    chat_id = _chat(conn)
    store.insert_message(
        conn,
        chat_id,
        MessageRecord(
            source_message_id="T1",
            message_timestamp="2026-06-10T10:02:00+00:00",
            text="hello",
            message_type="text",
        ),
    )
    row = conn.execute(
        "SELECT transcription_status FROM messages WHERE source_message_id = 'T1'"
    ).fetchone()
    assert row["transcription_status"] == "none"


def test_reconcile_backfills_media_on_existing_voice(conn: sqlite3.Connection) -> None:
    chat_id = _chat(conn)
    store.insert_message(
        conn,
        chat_id,
        MessageRecord(
            source_message_id="V3",
            message_timestamp="2026-06-10T10:03:00+00:00",
            text="[voice note]",
            message_type="voice",
        ),
    )
    updated = store.reconcile_voice_media(
        conn,
        chat_id,
        [
            MessageRecord(
                source_message_id="V3",
                message_timestamp="2026-06-10T10:03:00+00:00",
                text="[voice note]",
                message_type="voice",
                raw={"media_path": "media/V3.ogg", "ptt": True},
            )
        ],
    )
    assert updated == 1
    row = conn.execute(
        "SELECT transcription_status, media_path FROM messages WHERE source_message_id = 'V3'"
    ).fetchone()
    assert row["transcription_status"] == "pending"
    assert row["media_path"] == "media/V3.ogg"
