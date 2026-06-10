"""Consolidated digest behaviour, including the no-notification path."""

from __future__ import annotations

import sqlite3
from datetime import date

from src.analysis.classifier import StubClassifier
from src.analysis.review import review_monitored_chats
from src.db import store
from src.report.digest import DigestItem, build_digest, render_item
from tests.helpers import chat_id_by_source


def _item(**kw: object) -> DigestItem:
    base: dict[str, object] = {
        "chat": "Class 4A Group",
        "priority": "high",
        "summary": "Bring long trousers — school trip",
        "suggested_next_action": "Pack them",
        "deadline": None,
        "confidence": 0.9,
        "evidence_message_ids": ["c4a-0002"],
    }
    base.update(kw)
    return DigestItem(**base)  # type: ignore[arg-type]


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


# --- resolved-date rendering (#71) -----------------------------------------

def test_render_flags_resolved_date_relative_to_today() -> None:
    today = date(2026, 6, 9)
    assert "⏰ 2026-06-09 (TODAY)" in render_item(_item(deadline_date="2026-06-09"), today=today)
    assert "⏰ 2026-06-10 (tomorrow)" in render_item(
        _item(deadline_date="2026-06-10"), today=today
    )
    assert "⏰ 2026-06-08 (OVERDUE)" in render_item(
        _item(deadline_date="2026-06-08"), today=today
    )
    assert "⏰ 2026-06-12 (in 3 days)" in render_item(
        _item(deadline_date="2026-06-12"), today=today
    )


def test_stale_tomorrow_renders_as_today_not_future() -> None:
    # The 2026-06-09 miss at the render layer: a message sent on D-1 said
    # "tomorrow"; the model resolved that to D (the scan day). The digest must
    # surface it as TODAY, never as a comfortable future day.
    rendered = render_item(
        _item(deadline="tomorrow", deadline_date="2026-06-09"),
        today=date(2026, 6, 9),
    )
    assert "(TODAY)" in rendered
    assert "tomorrow" not in rendered  # the raw relative word is not re-shown


def test_render_falls_back_to_free_text_deadline() -> None:
    # No resolved date — behaviour is unchanged from before #71.
    assert "⏰ this evening" in render_item(_item(deadline="this evening"), today=date(2026, 6, 9))


def test_render_keeps_unparseable_resolved_date() -> None:
    out = render_item(_item(deadline_date="next Friday"), today=date(2026, 6, 9))
    assert "⏰ next Friday" in out
