"""Shared pytest fixtures: an in-temp SQLite store ingested from the fixture connector."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import ingest_all
from whatsapp_radar.connector.fixture import FixtureConnector
from whatsapp_radar.db import store


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = store.connect(tmp_path / "test.sqlite3")
    yield connection
    connection.close()


@pytest.fixture
def ingested_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    ingest_all(conn, FixtureConnector())
    return conn
