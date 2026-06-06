"""Shared test helpers (importable; fixtures live in conftest)."""

from __future__ import annotations

import sqlite3

from whatsapp_radar.connector.fixture import FixtureConnector
from whatsapp_radar.db import store
from whatsapp_radar.models import MessageRecord


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
    conn: sqlite3.Connection, source_chat_id: str, source_message_id: str, text: str
) -> None:
    """Simulate a connector delivering one new message into an existing chat."""
    chat_id = chat_id_by_source(conn, source_chat_id)
    store.insert_message(
        conn,
        chat_id,
        MessageRecord(
            source_message_id=source_message_id,
            message_timestamp="2026-06-10T10:00:00+00:00",
            text=text,
            sender_label="Tester",
        ),
    )
