"""Shared test helpers (importable; fixtures live in conftest)."""

from __future__ import annotations

import sqlite3

from src.connector.fixture import FixtureConnector
from src.db import store
from src.models import MessageRecord


def ingest_all(conn: sqlite3.Connection, connector: FixtureConnector) -> None:
    connector.connect()
    for chat in connector.list_chats():
        chat_id = store.upsert_chat(conn, chat)
        for msg in connector.fetch_messages(chat.source_chat_id):
            store.insert_message(conn, chat_id, msg)


def chat_id_by_source(conn: sqlite3.Connection, source_chat_id: str) -> int:
    row = conn.execute(
        "SELECT id FROM chats WHERE source_chat_id = ?", (source_chat_id,)
    ).fetchone()
    return int(row["id"])


def append_message(
    conn: sqlite3.Connection,
    source_chat_id: str,
    source_message_id: str,
    text: str,
    *,
    timestamp: str = "2026-06-10T10:00:00+00:00",
) -> None:
    """Simulate a connector delivering one new message into an existing chat.

    ``timestamp`` defaults to a recent send-time; pass an *older* one to simulate
    a resync backfilling out-of-order history (a message ingested after the cursor
    whose send-time predates it).
    """
    chat_id = chat_id_by_source(conn, source_chat_id)
    store.insert_message(
        conn,
        chat_id,
        MessageRecord(
            source_message_id=source_message_id,
            message_timestamp=timestamp,
            text=text,
            sender_label="Tester",
        ),
    )
