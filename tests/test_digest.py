"""Consolidated digest behaviour, including the no-notification path."""

from __future__ import annotations

import sqlite3

from tests.helpers import chat_id_by_source
from whatsapp_radar.analysis.classifier import StubClassifier
from whatsapp_radar.analysis.review import review_monitored_chats
from whatsapp_radar.db import store
from whatsapp_radar.report.digest import build_digest


def _monitor(conn: sqlite3.Connection, source_chat_id: str) -> None:
    store.set_chat_status(conn, chat_id_by_source(conn, source_chat_id), "monitored")


def test_digest_consolidates_actionable_across_chats(ingested_conn: sqlite3.Connection) -> None:
    _monitor(ingested_conn, "chat-class-4a")  # has an actionable "permission slip" message
    _monitor(ingested_conn, "chat-school-parents")  # has an "urgent pay" message
    outcome = review_monitored_chats(ingested_conn, StubClassifier())

    digest = build_digest(ingested_conn, outcome.run_id)
    assert digest.has_actionable_items
    chats = {item.chat for item in digest.items}
    assert chats == {"Class 4A Group", "School Parents Group"}
    # The urgent one is high priority via the stub's keyword rules.
    priorities = {item.chat: item.priority for item in digest.items}
    assert priorities["School Parents Group"] == "high"


def test_noise_only_chat_produces_no_notification(ingested_conn: sqlite3.Connection) -> None:
    _monitor(ingested_conn, "chat-building")  # only small talk
    outcome = review_monitored_chats(ingested_conn, StubClassifier())

    digest = build_digest(ingested_conn, outcome.run_id)
    assert not digest.has_actionable_items

    # No notification row should exist for a non-actionable run.
    rows = ingested_conn.execute(
        "SELECT COUNT(*) AS n FROM notifications WHERE run_id = ?", (outcome.run_id,)
    ).fetchone()["n"]
    assert rows == 0


def test_second_review_no_new_messages_no_actionable(ingested_conn: sqlite3.Connection) -> None:
    _monitor(ingested_conn, "chat-class-4a")
    classifier = StubClassifier()
    review_monitored_chats(ingested_conn, classifier)  # first run consumes backlog

    outcome = review_monitored_chats(ingested_conn, classifier)
    digest = build_digest(ingested_conn, outcome.run_id)
    assert not digest.has_actionable_items
