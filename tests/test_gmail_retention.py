"""Gmail sender-level retention (#166): what pruning removes and what it must never touch.

These tests are the offline counterpart of the live-DB safety proof: they assert
that :func:`store.prune_gmail_unmonitored` removes ONLY unmonitored-sender Gmail
messages older than the window, keeps monitored senders' history byte-for-byte,
never touches WhatsApp, is idempotent, and drops empty discovered senders.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from src.connector.base import ConnectorStatus
from src.connector.factory import ConnectorBinding
from src.db import store
from src.db.sync import sync_sources
from src.models import ChatRecord, MessageRecord

NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _add_chat(conn: sqlite3.Connection, source: str, source_chat_id: str, status: str) -> int:
    chat_id = store.upsert_chat(
        conn,
        ChatRecord(
            source_chat_id=source_chat_id,
            display_name=source_chat_id,
            chat_type="email" if source == "gmail" else "group",
            source=source,
        ),
    )
    store.set_chat_status(conn, chat_id, status)
    return chat_id


def _add_msg(conn: sqlite3.Connection, chat_id: int, mid: str, days_ago: int) -> None:
    store.insert_message(
        conn,
        chat_id,
        MessageRecord(
            source_message_id=mid,
            message_timestamp=_iso(days_ago),
            text=f"message {mid}",
            sender_label="Sender",
        ),
    )


def _count(conn: sqlite3.Connection, chat_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?", (chat_id,)
        ).fetchone()["n"]
    )


def test_prunes_only_unmonitored_gmail_old_messages(conn: sqlite3.Connection) -> None:
    monitored = _add_chat(conn, "gmail", "sender:keep@example.com", "monitored")
    unmonitored = _add_chat(conn, "gmail", "sender:news@example.com", "discovered")
    whatsapp = _add_chat(conn, "whatsapp", "wa-1", "discovered")

    _add_msg(conn, monitored, "m-old", 90)
    _add_msg(conn, monitored, "m-new", 5)
    _add_msg(conn, unmonitored, "u-old", 90)
    _add_msg(conn, unmonitored, "u-new", 5)
    _add_msg(conn, whatsapp, "w-old", 90)
    _add_msg(conn, whatsapp, "w-new", 5)

    outcome = store.prune_gmail_unmonitored(conn, retention_days=30, now=NOW)

    # Only the unmonitored Gmail sender's old message is gone.
    assert outcome.messages_pruned == 1
    assert _count(conn, monitored) == 2  # monitored history intact
    assert _count(conn, unmonitored) == 1  # only recent kept
    assert _count(conn, whatsapp) == 2  # WhatsApp untouched
    remaining = {
        row["source_message_id"]
        for row in conn.execute("SELECT source_message_id FROM messages").fetchall()
    }
    assert "u-old" not in remaining
    assert {"m-old", "m-new", "u-new", "w-old", "w-new"} <= remaining


def test_whatsapp_is_never_touched_even_when_all_old(conn: sqlite3.Connection) -> None:
    whatsapp = _add_chat(conn, "whatsapp", "wa-1", "discovered")
    _add_msg(conn, whatsapp, "w1", 400)
    _add_msg(conn, whatsapp, "w2", 365)

    outcome = store.prune_gmail_unmonitored(conn, retention_days=30, now=NOW)

    assert outcome.is_noop
    assert _count(conn, whatsapp) == 2


def test_is_idempotent(conn: sqlite3.Connection) -> None:
    unmonitored = _add_chat(conn, "gmail", "sender:news@example.com", "discovered")
    _add_msg(conn, unmonitored, "u-old", 90)
    _add_msg(conn, unmonitored, "u-new", 5)

    first = store.prune_gmail_unmonitored(conn, retention_days=30, now=NOW)
    second = store.prune_gmail_unmonitored(conn, retention_days=30, now=NOW)

    assert first.messages_pruned == 1
    assert second.is_noop


class _EmptyGmailConnector:
    """A live-but-quiet Gmail source: lists nothing, so a sync only prunes."""

    def connect(self) -> ConnectorStatus:
        return ConnectorStatus("gmail", True, "ok")

    def status(self) -> ConnectorStatus:
        return self.connect()

    def list_chats(self) -> list[ChatRecord]:
        return []

    def fetch_messages(self, source_chat_id: str) -> list[MessageRecord]:
        return []

    def canonical_source_id(self, source_chat_id: str) -> str | None:
        return source_chat_id

    def stop(self) -> None:
        return None


def test_sync_prunes_unmonitored_gmail_and_logs_detail(conn: sqlite3.Connection) -> None:
    unmonitored = _add_chat(conn, "gmail", "sender:news@example.com", "discovered")
    monitored = _add_chat(conn, "gmail", "sender:keep@example.com", "monitored")
    # Absolute-ancient vs a message inside any plausible window, so the assertion is
    # independent of the wall clock the sync's prune reads.
    store.insert_message(
        conn,
        unmonitored,
        MessageRecord(
            source_message_id="ancient",
            message_timestamp="2000-01-01T00:00:00+00:00",
            text="ancient",
        ),
    )
    store.insert_message(
        conn,
        monitored,
        MessageRecord(
            source_message_id="mon-ancient",
            message_timestamp="2000-01-01T00:00:00+00:00",
            text="monitored ancient",
        ),
    )
    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    store.insert_message(
        conn,
        unmonitored,
        MessageRecord(source_message_id="recent", message_timestamp=recent, text="recent"),
    )

    sync_sources(
        conn,
        [ConnectorBinding("gmail", _EmptyGmailConnector())],
        operation="scan",
        gmail_retention_days=30,
    )

    remaining = {
        row["source_message_id"]
        for row in conn.execute("SELECT source_message_id FROM messages").fetchall()
    }
    # Only the unmonitored ancient message is gone; monitored history is untouched.
    assert remaining == {"mon-ancient", "recent"}
    detail = store.recent_syncs(conn)[0]["detail"]
    assert "retention pruned" in detail


def test_reprocess_retention_days_zero_skips_prune(conn: sqlite3.Connection) -> None:
    unmonitored = _add_chat(conn, "gmail", "sender:news@example.com", "discovered")
    store.insert_message(
        conn,
        unmonitored,
        MessageRecord(
            source_message_id="ancient",
            message_timestamp="2000-01-01T00:00:00+00:00",
            text="ancient",
        ),
    )

    sync_sources(
        conn,
        [ConnectorBinding("gmail", _EmptyGmailConnector())],
        operation="reprocess",
        gmail_retention_days=0,
    )

    # 0 disables retention (the rebuild path), so nothing is pruned.
    assert _count(conn, unmonitored) == 1


def test_empty_discovered_sender_row_is_removed_monitored_kept(conn: sqlite3.Connection) -> None:
    monitored_empty_old = _add_chat(conn, "gmail", "sender:keep@example.com", "monitored")
    discovered_stale = _add_chat(conn, "gmail", "sender:gone@example.com", "discovered")
    _add_msg(conn, monitored_empty_old, "m-old", 400)
    _add_msg(conn, discovered_stale, "d-old", 400)

    outcome = store.prune_gmail_unmonitored(conn, retention_days=30, now=NOW)

    assert outcome.messages_pruned == 1  # only the discovered sender's message
    assert outcome.senders_removed == 1  # its now-empty row dropped
    # The monitored sender keeps its row AND its old history.
    assert store.get_chat(conn, monitored_empty_old) is not None
    assert _count(conn, monitored_empty_old) == 1
    # The discovered sender's row is gone.
    assert store.get_chat(conn, discovered_stale) is None
