"""Reprocess (src/db/reprocess.py): full rebuild that preserves operator state.

The fixture connector has no JID aliasing, so ``canonical_source_id`` is identity
and a rebuilt chat keeps its source id — which is exactly what lets monitored /
ignored / alias state re-attach. The test asserts state survives, the DB is
backed up, and run history is discarded.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.connector.fixture import FixtureConnector
from src.db import store
from src.db.reprocess import reprocess, reprocess_outcome_to_dict
from src.db.sync import resync


def _seed(conn: sqlite3.Connection) -> tuple[int, int]:
    resync(conn, FixtureConnector())
    mon = store.chat_id_for_source(conn, "chat-class-4a")
    ign = store.chat_id_for_source(conn, "chat-building")
    assert mon is not None and ign is not None
    store.set_chat_status(conn, mon, "monitored")
    store.set_chat_alias(conn, mon, "4A")
    store.set_chat_status(conn, ign, "ignored")
    return mon, ign


def test_reprocess_preserves_status_and_alias(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed(conn)
    db_path = tmp_path / "test.sqlite3"  # matches the conftest conn db name

    outcome = reprocess(conn, FixtureConnector(), db_path)

    assert outcome.monitored_preserved == 1
    assert outcome.ignored_preserved == 1
    assert outcome.aliases_preserved == 1
    assert outcome.unmapped == []

    # Re-keyed chats carry the operator state forward.
    mon = store.chat_id_for_source(conn, "chat-class-4a")
    ign = store.chat_id_for_source(conn, "chat-building")
    assert mon is not None and ign is not None
    assert store.get_chat(conn, mon)["status"] == "monitored"
    assert store.get_chat(conn, mon)["alias"] == "4A"
    assert store.get_chat(conn, ign)["status"] == "ignored"


def test_reprocess_backs_up_and_resets_history(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed(conn)
    # A prior run that should NOT survive the rebuild.
    run_id = store.start_run(conn, mode="dry_run")
    store.finish_run(conn, run_id, "completed", 0)
    assert store.count_runs(conn) == 1

    db_path = tmp_path / "test.sqlite3"
    outcome = reprocess(conn, FixtureConnector(), db_path)

    assert Path(outcome.backup_path).is_file()
    assert store.count_runs(conn) == 0  # history reset
    assert outcome.chats_after == 3


def test_reprocess_rebaselines_monitored(conn: sqlite3.Connection, tmp_path: Path) -> None:
    mon, _ = _seed(conn)
    db_path = tmp_path / "test.sqlite3"
    reprocess(conn, FixtureConnector(), db_path)

    mon2 = store.chat_id_for_source(conn, "chat-class-4a")
    assert mon2 is not None
    # A baselined cursor means the first review skips the existing backlog.
    state = conn.execute(
        "SELECT last_processed_message_id FROM chat_review_state WHERE chat_id = ?", (mon2,)
    ).fetchone()
    assert state is not None
    assert state["last_processed_message_id"] is not None


def test_reprocess_outcome_to_dict_shape(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed(conn)
    outcome = reprocess(conn, FixtureConnector(), tmp_path / "test.sqlite3")
    payload = reprocess_outcome_to_dict(outcome)
    assert payload["kind"] == "reprocess"
    assert payload["ok"] is True
    assert "backup_path" in payload
