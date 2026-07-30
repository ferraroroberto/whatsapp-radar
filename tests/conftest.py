"""Shared pytest fixtures: an in-temp SQLite store ingested from the fixture connector."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.webapp import runs as webapp_runs
from src.connector.fixture import FixtureConnector
from src.db import store
from tests.helpers import ingest_all


@pytest.fixture(autouse=True)
def isolated_runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test's run records out of the real ``webapp/runs/``.

    Autouse and repo-wide because since #233 the CLI writes a run record of its
    own: any test calling ``cli.main(["traffic-check", ...])`` would otherwise
    litter the developer's actual run history — and then be read back by the
    next Execution-tab poll as if it were a real run. Tests that need to inspect
    the directory can take this fixture (or patch ``RUNS_DIR`` themselves, which
    still wins, both being function-scoped).
    """
    target = tmp_path / "webapp-runs"
    monkeypatch.setattr(webapp_runs, "RUNS_DIR", target)
    return target


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = store.connect(tmp_path / "test.sqlite3")
    yield connection
    connection.close()


@pytest.fixture
def ingested_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    ingest_all(conn, FixtureConnector())
    return conn
