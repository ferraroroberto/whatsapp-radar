"""Fixture connector + ingestion: deterministic, no credentials, idempotent."""

from __future__ import annotations

import sqlite3

from src.connector.fixture import FixtureConnector
from tests.helpers import ingest_all


def test_fixture_connector_is_read_only() -> None:
    connector = FixtureConnector()
    # The read-only guarantee: no write-side methods exist on the connector.
    for forbidden in ("send", "send_message", "react", "mark_read", "delete"):
        assert not hasattr(connector, forbidden)


def test_ingest_is_deterministic_and_complete(ingested_conn: sqlite3.Connection) -> None:
    chats = ingested_conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()["n"]
    messages = ingested_conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    assert chats == 3
    assert messages == 7  # 3 + 2 + 2 from the fixture


def test_ingest_is_idempotent(conn: sqlite3.Connection) -> None:
    ingest_all(conn, FixtureConnector())
    ingest_all(conn, FixtureConnector())
    messages = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    assert messages == 7  # re-ingesting the same fixture creates no duplicates
