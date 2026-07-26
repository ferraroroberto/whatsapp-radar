"""Non-routine ack items: DB layer (Step 5/5 of #206, #219)."""

from __future__ import annotations

import sqlite3

from src.db import store
from src.models import ChatRecord


def _chat(conn: sqlite3.Connection) -> int:
    return store.upsert_chat(
        conn, ChatRecord(source_chat_id="sender:school@example.test", display_name="School Updates")
    )


def test_insert_and_list_pending(conn: sqlite3.Connection) -> None:
    chat_id = _chat(conn)
    run_id = store.start_run(conn, kind="scan")

    item_id = store.insert_ack_item(
        conn, run_id, chat_id,
        child="Sam", task_category="permission_slip", summary="Bring the signed form",
    )

    pending = store.pending_ack_items(conn)
    assert len(pending) == 1
    assert pending[0]["id"] == item_id
    assert pending[0]["status"] == "pending"
    assert pending[0]["display_name"] == "School Updates"
    assert pending[0]["child"] == "Sam"
    assert pending[0]["calendar_event_id"] is None
    assert pending[0]["acknowledged_at"] is None


def test_acknowledge_removes_from_pending(conn: sqlite3.Connection) -> None:
    chat_id = _chat(conn)
    run_id = store.start_run(conn, kind="scan")
    item_id = store.insert_ack_item(
        conn, run_id, chat_id, child="Sam", task_category="permission_slip", summary=None
    )

    store.acknowledge_item(conn, item_id)

    assert store.pending_ack_items(conn) == []
    row = store.get_ack_item(conn, item_id)
    assert row is not None
    assert row["status"] == "acknowledged"
    assert row["acknowledged_at"] is not None


def test_acknowledge_is_idempotent(conn: sqlite3.Connection) -> None:
    chat_id = _chat(conn)
    run_id = store.start_run(conn, kind="scan")
    item_id = store.insert_ack_item(
        conn, run_id, chat_id, child="Sam", task_category="permission_slip", summary=None
    )

    store.acknowledge_item(conn, item_id)
    first_row = store.get_ack_item(conn, item_id)
    assert first_row is not None
    first_ack_at = first_row["acknowledged_at"]

    store.acknowledge_item(conn, item_id)  # repeat tap
    second_row = store.get_ack_item(conn, item_id)
    assert second_row is not None
    assert second_row["status"] == "acknowledged"
    assert second_row["acknowledged_at"] == first_ack_at  # unchanged, not re-stamped


def test_get_ack_item_missing_returns_none(conn: sqlite3.Connection) -> None:
    assert store.get_ack_item(conn, 9999) is None


def test_pending_items_are_newest_first(conn: sqlite3.Connection) -> None:
    chat_id = _chat(conn)
    run_id = store.start_run(conn, kind="scan")
    first_id = store.insert_ack_item(
        conn, run_id, chat_id, child="Sam", task_category="permission_slip", summary=None
    )
    second_id = store.insert_ack_item(
        conn, run_id, chat_id, child="Mia", task_category="supply", summary=None
    )

    pending = store.pending_ack_items(conn)
    assert [row["id"] for row in pending] == [second_id, first_id]
