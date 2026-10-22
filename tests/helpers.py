"""Shared test helpers (importable; fixtures live in conftest)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.analysis.classifier import StubClassifier
from src.analysis.pipeline import ScanOutcome, scan
from src.config import Config, HubConfig, TelegramConfig
from src.connector.base import ConnectorStatus
from src.connector.fixture import FixtureConnector
from src.db import store
from src.models import ChatRecord, MessageRecord


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


class OfflineConnector:
    """Reports offline and refuses reads: the #29 liveness gate must catch it."""

    def connect(self) -> ConnectorStatus:
        return ConnectorStatus(name="linked_device", connected=False, detail="heartbeat stale")

    def status(self) -> ConnectorStatus:
        return self.connect()

    def list_chats(self) -> list[ChatRecord]:
        raise AssertionError("an offline source must never be read")

    def fetch_messages(self, source_chat_id: str) -> list[MessageRecord]:
        raise AssertionError("an offline source must never be read")

    def canonical_source_id(self, source_chat_id: str) -> str | None:
        return source_chat_id

    def stop(self) -> None:
        return None


def review_config(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "unused.sqlite3",
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="claude_sonnet"),
        notifier="none",
        telegram=TelegramConfig(bot_token="t", chat_id="c"),
        linked_device_dir=tmp_path / "ld",
    )


def review(conn: sqlite3.Connection, tmp_path: Path, classifier: object = None) -> ScanOutcome:
    # ``process`` (#394): the ``wr review`` verb expressed as a scan mode that
    # skips sync and reuses scan's traced loop.
    return scan(
        conn, review_config(tmp_path), mode="process",
        classifier=classifier or StubClassifier(), deliver=False,
    )
