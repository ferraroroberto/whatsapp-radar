"""Cursor and delta semantics — the core behaviour the spike proves."""

from __future__ import annotations

import json
import sqlite3

from src.analysis.classifier import StubClassifier
from src.analysis.contract import ContractError
from src.analysis.review import review_monitored_chats
from src.db import store
from src.models import StoredMessage
from tests.helpers import append_message, chat_id_by_source


def _monitor(conn: sqlite3.Connection, source_chat_id: str) -> int:
    chat_id = chat_id_by_source(conn, source_chat_id)
    store.set_chat_status(conn, chat_id, "monitored")
    return chat_id


def test_first_review_processes_all_then_second_is_noop(ingested_conn: sqlite3.Connection) -> None:
    _monitor(ingested_conn, "chat-class-4a")
    classifier = StubClassifier()

    first = review_monitored_chats(ingested_conn, classifier)
    assert first.chats_with_delta == 1
    assert first.messages_processed == 3
    assert first.source_funnels["whatsapp"].messages_checked == 3
    assert first.source_funnels["whatsapp"].cursors_advanced == 1
    run = store.review_run(ingested_conn, first.run_id)
    assert run is not None
    persisted = json.loads(run["source_funnel_json"])
    assert persisted["whatsapp"]["monitored_channels"] == 1
    assert persisted["whatsapp"]["messages_checked"] == 3

    second = review_monitored_chats(ingested_conn, classifier)
    assert second.chats_with_delta == 0
    assert second.messages_processed == 0


def test_new_message_processes_only_the_delta(ingested_conn: sqlite3.Connection) -> None:
    _monitor(ingested_conn, "chat-class-4a")
    classifier = StubClassifier()
    review_monitored_chats(ingested_conn, classifier)  # consume initial backlog

    append_message(ingested_conn, "chat-class-4a", "c4a-0004", "please sign the new form")
    third = review_monitored_chats(ingested_conn, classifier)
    assert third.chats_with_delta == 1
    assert third.messages_processed == 1  # only the one new message


def test_backfilled_out_of_order_message_is_not_skipped(
    ingested_conn: sqlite3.Connection,
) -> None:
    """A resync backfilling an older-send-time message is still picked up (#37).

    The cursor keys on ingestion id, not send-time, so a message ingested *after*
    a scan but bearing a ``message_timestamp`` earlier than the cursor's high-water
    send-time lands in the next delta rather than being filtered out forever.
    """
    chat_id = _monitor(ingested_conn, "chat-class-4a")
    classifier = StubClassifier()
    review_monitored_chats(ingested_conn, classifier)  # advances cursor past backlog
    assert review_monitored_chats(ingested_conn, classifier).messages_processed == 0

    # Backfill: ingested now (new id) but with a send-time *older* than the cursor's
    # high-water send-time (2026-06-01T08:06 from c4a-0003).
    append_message(
        ingested_conn,
        "chat-class-4a",
        "c4a-backfill",
        "please sign and return the form",
        timestamp="2026-06-01T08:04:00+00:00",
    )

    after = review_monitored_chats(ingested_conn, classifier)
    assert after.chats_with_delta == 1
    assert after.messages_processed == 1  # the backfilled message, not skipped

    # And it is not reprocessed on the next run.
    assert review_monitored_chats(ingested_conn, classifier).messages_processed == 0

    # The cursor advanced to the backfill's ingestion id (the new high-water mark).
    state = ingested_conn.execute(
        "SELECT last_processed_message_id FROM chat_review_state WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    backfill_id = ingested_conn.execute(
        "SELECT id FROM messages WHERE source_message_id = 'c4a-backfill'"
    ).fetchone()[0]
    assert state["last_processed_message_id"] == backfill_id


def test_only_monitored_chats_are_reviewed(ingested_conn: sqlite3.Connection) -> None:
    _monitor(ingested_conn, "chat-class-4a")  # building + school-parents left unmonitored
    outcome = review_monitored_chats(ingested_conn, StubClassifier())
    assert outcome.chats_with_delta == 1


def test_cursor_not_advanced_on_contract_error(ingested_conn: sqlite3.Connection) -> None:
    chat_id = _monitor(ingested_conn, "chat-class-4a")

    class BadClassifier:
        def classify(
            self,
            chat_display_name: str,
            delta: list[StoredMessage],
            prior_context: str | None,
            *,
            source: str = "whatsapp",
        ) -> str:
            return "{ not valid json"

    outcome = review_monitored_chats(ingested_conn, BadClassifier())
    assert outcome.errors and outcome.errors[0][0] == chat_id
    assert outcome.messages_processed == 0

    # Cursor untouched -> a good classifier reprocesses the same backlog next run.
    state = ingested_conn.execute(
        "SELECT last_processed_message_id FROM chat_review_state WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    assert state is None

    recovered = review_monitored_chats(ingested_conn, StubClassifier())
    assert recovered.messages_processed == 3

    # The raised error type is ContractError under the hood.
    from src.analysis.contract import parse_analysis

    try:
        parse_analysis("{ not valid json")
    except ContractError:
        pass
    else:
        raise AssertionError("expected ContractError")
