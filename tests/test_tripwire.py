"""Stage-1-only discovery tripwire: bounded reads and status exclusions."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from src.analysis.tripwire import scan_tripwire
from src.config import TripwireConfig
from src.db import store
from src.models import ChatRecord, MessageRecord


def _message(
    conn: sqlite3.Connection,
    chat_id: int,
    source_id: str,
    text: str,
    sent_at: datetime,
) -> None:
    store.insert_message(
        conn,
        chat_id,
        MessageRecord(
            source_message_id=source_id,
            message_timestamp=sent_at.isoformat(timespec="seconds"),
            text=text,
            sender_label="Fixture",
        ),
    )


def test_tripwire_is_recent_capped_and_discovered_only(conn: sqlite3.Connection) -> None:
    now = datetime(2026, 7, 19, 12, tzinfo=UTC)
    discovered = store.upsert_chat(
        conn, ChatRecord(source_chat_id="discovered", display_name="Worth watching")
    )
    noisy = store.upsert_chat(
        conn, ChatRecord(source_chat_id="noisy", display_name="Noisy chat")
    )
    monitored = store.upsert_chat(
        conn, ChatRecord(source_chat_id="monitored", display_name="Already monitored")
    )
    ignored = store.upsert_chat(
        conn, ChatRecord(source_chat_id="ignored", display_name="Explicitly ignored")
    )
    store.set_chat_status(conn, monitored, "monitored")
    store.set_chat_status(conn, ignored, "ignored")

    _message(conn, discovered, "d-old", "urgent but stale", now - timedelta(days=8))
    _message(conn, discovered, "d-new", "urgent pickup deadline", now - timedelta(hours=1))
    _message(conn, monitored, "m-new", "urgent payment", now - timedelta(minutes=10))
    _message(conn, ignored, "i-new", "urgent payment", now - timedelta(minutes=5))
    for index in range(4):
        _message(
            conn,
            noisy,
            f"n-{index}",
            "ordinary chatter",
            now - timedelta(minutes=20 + index),
        )

    result = scan_tripwire(
        conn,
        TripwireConfig(window_days=7, max_messages=3, max_messages_per_chat=2),
        now=now,
    )

    assert result.scanned_messages == 3
    assert result.truncated is True
    assert [hit.chat_id for hit in result.hits] == [discovered]
    assert set(result.hits[0].roots) >= {"urgent", "deadline"}
    assert result.hits[0].matched_messages == 1


def test_tripwire_uses_source_specific_gmail_buckets(conn: sqlite3.Connection) -> None:
    now = datetime(2026, 7, 19, 12, tzinfo=UTC)
    chat_id = store.upsert_chat(
        conn,
        ChatRecord(
            source_chat_id="sender:school@example.com",
            display_name="School mail",
            chat_type="email",
            source="gmail",
        ),
    )
    _message(conn, chat_id, "mail-1", "Invoice payment due tomorrow", now)

    result = scan_tripwire(conn, TripwireConfig(), now=now)

    assert len(result.hits) == 1
    assert result.hits[0].source == "gmail"
    assert set(result.hits[0].buckets) >= {"deadline", "payment"}
