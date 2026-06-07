"""Resync (src/db/sync.py): incremental, idempotent upsert from the connector.

Asserted against the deterministic fixture connector so the deltas are known
exactly: a first resync ingests everything; a second over the same buffer is a
no-op; operator state (status/alias) is never touched.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.connector.base import ConnectorStatus
from src.connector.fixture import FixtureConnector
from src.connector.preflight import ConnectorOffline
from src.db import store
from src.db.sync import resync, resync_outcome_to_dict
from src.models import ChatRecord, MessageRecord


def test_resync_ingests_then_is_noop(conn: sqlite3.Connection) -> None:
    first = resync(conn, FixtureConnector())
    assert first.chats_added == 3
    assert first.chats_updated == 0
    assert first.messages_added > 0
    assert not first.is_noop

    second = resync(conn, FixtureConnector())
    assert (second.chats_added, second.chats_updated, second.messages_added) == (0, 0, 0)
    assert second.is_noop


def test_resync_preserves_operator_state(conn: sqlite3.Connection) -> None:
    resync(conn, FixtureConnector())
    chat_id = store.chat_id_for_source(conn, "chat-class-4a")
    assert chat_id is not None
    store.set_chat_status(conn, chat_id, "monitored")
    store.set_chat_alias(conn, chat_id, "My Alias")

    resync(conn, FixtureConnector())

    row = store.get_chat(conn, chat_id)
    assert row is not None
    assert row["status"] == "monitored"
    assert row["alias"] == "My Alias"


def test_resync_reports_new_messages_only(conn: sqlite3.Connection) -> None:
    resync(conn, FixtureConnector())
    chat_id = store.chat_id_for_source(conn, "chat-building")
    assert chat_id is not None
    # Drop one message so the next resync re-adds exactly that one.
    conn.execute(
        "DELETE FROM messages WHERE id = (SELECT MIN(id) FROM messages WHERE chat_id = ?)",
        (chat_id,),
    )
    conn.commit()

    again = resync(conn, FixtureConnector())
    assert again.messages_added == 1
    assert again.chats_added == 0


class _OfflineConnector:
    """Reports offline and refuses reads — the #29 liveness gate must catch it."""

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


def test_resync_aborts_when_source_offline(conn: sqlite3.Connection) -> None:
    with pytest.raises(ConnectorOffline):
        resync(conn, _OfflineConnector())
    # Nothing was written from the dead source.
    assert store.count_chats(conn) == 0


def test_resync_outcome_to_dict_shape(conn: sqlite3.Connection) -> None:
    payload = resync_outcome_to_dict(resync(conn, FixtureConnector()))
    assert payload["kind"] == "resync"
    assert payload["ok"] is True
    assert set(payload) >= {"chats_added", "chats_updated", "messages_added", "noop"}
