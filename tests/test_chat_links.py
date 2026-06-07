"""Parent↔child chat links (#25): manual merge of one person across numbers.

All offline against the fixture connector + sanitized data. Covers the store
link rules, family-aware review (one analysis per family, per-member cursors,
backlog included), the link/unlink + merged-history API, and link survival
across a full reprocess rebuild.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.webapp.server import create_app
from src.analysis.classifier import StubClassifier
from src.analysis.review import review_monitored_chats
from src.connector.fixture import FixtureConnector
from src.db import store
from src.db.reprocess import reprocess
from src.db.sync import resync
from src.models import ChatRecord, MessageRecord
from src.webapp_config import WebappConfig
from tests.helpers import chat_id_by_source

LOOPBACK = ("127.0.0.1", 5555)


def _msg_count(conn: sqlite3.Connection, chat_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?", (chat_id,)
        ).fetchone()["n"]
    )


def _has_cursor(conn: sqlite3.Connection, chat_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM chat_review_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        is not None
    )


# --- store: link rules + family membership ---------------------------------

def test_link_and_unlink_roundtrip(ingested_conn: sqlite3.Connection) -> None:
    parent = chat_id_by_source(ingested_conn, "chat-class-4a")
    child = chat_id_by_source(ingested_conn, "chat-school-parents")

    store.link_chats(ingested_conn, child, parent)
    assert store.get_chat(ingested_conn, child)["parent_chat_id"] == parent
    assert store.family_member_ids(ingested_conn, parent) == sorted([parent, child])
    assert store.child_count(ingested_conn, parent) == 1
    assert [r["id"] for r in store.child_chats(ingested_conn, parent)] == [child]

    assert store.unlink_chat(ingested_conn, child) is True
    assert store.get_chat(ingested_conn, child)["parent_chat_id"] is None
    # Unlinking an already-unlinked chat is a no-op.
    assert store.unlink_chat(ingested_conn, child) is False
    assert store.family_member_ids(ingested_conn, parent) == [parent]


def test_link_rejects_self_chain_and_parent_with_children(
    ingested_conn: sqlite3.Connection,
) -> None:
    a = chat_id_by_source(ingested_conn, "chat-class-4a")
    b = chat_id_by_source(ingested_conn, "chat-school-parents")
    c = chat_id_by_source(ingested_conn, "chat-building")

    with pytest.raises(store.LinkError):
        store.link_chats(ingested_conn, a, a)  # self-link

    store.link_chats(ingested_conn, b, a)  # b is now a child of a
    with pytest.raises(store.LinkError):
        store.link_chats(ingested_conn, c, b)  # can't link under a child (chain)
    with pytest.raises(store.LinkError):
        store.link_chats(ingested_conn, a, c)  # a has a child, can't become a child


# --- family-aware review ----------------------------------------------------

def test_family_review_is_one_subject(ingested_conn: sqlite3.Connection) -> None:
    parent = chat_id_by_source(ingested_conn, "chat-class-4a")
    child = chat_id_by_source(ingested_conn, "chat-school-parents")
    total = _msg_count(ingested_conn, parent) + _msg_count(ingested_conn, child)

    store.link_chats(ingested_conn, child, parent)
    store.set_chat_status(ingested_conn, parent, "monitored")

    outcome = review_monitored_chats(ingested_conn, StubClassifier())

    # The family is reviewed once, over the merged backlog of both members.
    assert outcome.chats_with_delta == 1
    assert outcome.messages_processed == total

    # Exactly one analysis item, attributed to the head — the digest sees the
    # family once (no double-count).
    items = ingested_conn.execute(
        "SELECT chat_id FROM analysis_items WHERE run_id = ?", (outcome.run_id,)
    ).fetchall()
    assert [r["chat_id"] for r in items] == [parent]

    # Every member's own cursor advanced (each keyed on its own ingestion id).
    assert _has_cursor(ingested_conn, parent)
    assert _has_cursor(ingested_conn, child)

    # Second pass is a no-op for the whole family.
    assert review_monitored_chats(ingested_conn, StubClassifier()).messages_processed == 0


def test_overview_folds_family_stats_into_parent(tmp_path: Path) -> None:
    """A parent row represents the merged family: newest time, preview, and the
    summed count come from itself + its children, so a child's newer message
    floats the parent to the top of the list (#25)."""
    db = tmp_path / "overview.sqlite3"
    parent, child = _seed_family_db(db)  # parent msg 10:00, child msg 10:30
    conn = store.connect(db)
    try:
        store.link_chats(conn, child, parent)
        rows = {int(r["id"]): r for r in store.chats_overview(conn)}
        head = rows[parent]
        # Family-aggregated, not the parent's own 10:00 / single message.
        assert head["last_message_at"] == "2026-06-01T10:30:00+00:00"
        assert head["last_message_text"] == "from new number"
        assert head["message_count"] == 2
    finally:
        conn.close()


def test_linked_child_not_reviewed_standalone(ingested_conn: sqlite3.Connection) -> None:
    parent = chat_id_by_source(ingested_conn, "chat-class-4a")
    child = chat_id_by_source(ingested_conn, "chat-school-parents")
    # The child was independently monitored before linking; linking subsumes it.
    store.set_chat_status(ingested_conn, child, "monitored")
    store.set_chat_status(ingested_conn, parent, "monitored")
    store.link_chats(ingested_conn, child, parent)

    # monitored_chats yields heads only — the child is folded, not its own subject.
    heads = [int(r["id"]) for r in store.monitored_chats(ingested_conn)]
    assert child not in heads and parent in heads

    outcome = review_monitored_chats(ingested_conn, StubClassifier())
    assert outcome.chats_with_delta == 1


# --- API: link / unlink / merged history ------------------------------------

def _app_with_db(db: Path) -> Any:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    return app


def _seed_family_db(db: Path) -> tuple[int, int]:
    """A parent + child, each with one message at interleaving send-times."""
    conn = store.connect(db)
    parent = store.upsert_chat(
        conn, ChatRecord(source_chat_id="p", display_name="School Office", chat_type="dm")
    )
    child = store.upsert_chat(
        conn, ChatRecord(source_chat_id="c", display_name="+44987", chat_type="dm")
    )
    store.insert_message(
        conn,
        parent,
        MessageRecord(
            source_message_id="p1",
            message_timestamp="2026-06-01T10:00:00+00:00",
            text="from old number",
            sender_label="Office",
        ),
    )
    store.insert_message(
        conn,
        child,
        MessageRecord(
            source_message_id="c1",
            message_timestamp="2026-06-01T10:30:00+00:00",
            text="from new number",
            sender_label="Office",
        ),
    )
    conn.close()
    return parent, child


def test_link_endpoint_and_validation(tmp_path: Path) -> None:
    db = tmp_path / "link.sqlite3"
    parent, child = _seed_family_db(db)

    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        ok = client.post(f"/api/chats/{child}/link", json={"parent_id": parent})
        assert ok.status_code == 200 and ok.json() == {"id": child, "parent_id": parent}

        # The child now carries parent_chat_id in the listing.
        listed = {c["id"]: c for c in client.get("/api/chats").json()["chats"]}
        assert listed[child]["parent_chat_id"] == parent
        assert listed[parent]["parent_chat_id"] is None

        # Self-link → 400; unknown chat / unknown parent → 404.
        def _link(cid: int, pid: int) -> int:
            return client.post(f"/api/chats/{cid}/link", json={"parent_id": pid}).status_code

        assert _link(parent, parent) == 400
        assert _link(99999, parent) == 404
        assert _link(parent, 99999) == 404

        # Unlink restores independence.
        un = client.post(f"/api/chats/{child}/unlink")
        assert un.status_code == 200 and un.json() == {"id": child, "unlinked": True}
        again = {c["id"]: c for c in client.get("/api/chats").json()["chats"]}
        assert again[child]["parent_chat_id"] is None


def test_parent_history_merges_family(tmp_path: Path) -> None:
    db = tmp_path / "merge.sqlite3"
    parent, child = _seed_family_db(db)

    with TestClient(_app_with_db(db), client=LOOPBACK) as client:
        client.post(f"/api/chats/{child}/link", json={"parent_id": parent})
        body = client.get(f"/api/chats/{parent}/history?limit=100").json()

    # Both members' messages, time-ordered, each tagged with its origin chat.
    assert [m["text"] for m in body["messages"]] == ["from old number", "from new number"]
    origins = [m["origin"] for m in body["messages"]]
    assert origins == ["School Office", "+44987"]


# --- reprocess durability ---------------------------------------------------

def test_reprocess_preserves_links(conn: sqlite3.Connection, tmp_path: Path) -> None:
    resync(conn, FixtureConnector())
    parent = store.chat_id_for_source(conn, "chat-class-4a")
    child = store.chat_id_for_source(conn, "chat-school-parents")
    assert parent is not None and child is not None
    store.link_chats(conn, child, parent)

    outcome = reprocess(conn, FixtureConnector(), tmp_path / "test.sqlite3")
    assert outcome.links_preserved == 1

    parent2 = store.chat_id_for_source(conn, "chat-class-4a")
    child2 = store.chat_id_for_source(conn, "chat-school-parents")
    assert parent2 is not None and child2 is not None
    assert store.get_chat(conn, child2)["parent_chat_id"] == parent2
