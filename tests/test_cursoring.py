"""Cursor and delta semantics — the core behaviour the spike proves."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.analysis.classifier import ClassificationOutcome, ClassifierUnavailable, StubClassifier
from src.analysis.contract import ContractError
from src.analysis.pipeline import scan
from src.analysis.review import CLASSIFIER_OFFLINE_STATUS
from src.config import Config, HubConfig, TelegramConfig
from src.db import store
from src.models import StoredMessage
from tests.helpers import append_message, chat_id_by_source


def _config(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "unused.sqlite3",
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="claude_sonnet"),
        notifier="none",
        telegram=TelegramConfig(bot_token="t", chat_id="c"),
        linked_device_dir=tmp_path / "ld",
    )


def _monitor(conn: sqlite3.Connection, source_chat_id: str) -> int:
    chat_id = chat_id_by_source(conn, source_chat_id)
    store.set_chat_status(conn, chat_id, "monitored")
    return chat_id


def _review(conn: sqlite3.Connection, tmp_path: Path, classifier: object):
    # ``process`` (#394): the ``wr review`` verb expressed as a scan mode that
    # skips sync and reuses scan's traced loop. ``deliver=False`` mirrors the
    # verb's own ``--dry-run`` — cursors still advance, only the digest is held.
    return scan(conn, _config(tmp_path), mode="process", classifier=classifier, deliver=False)


def test_first_review_processes_all_then_second_is_noop(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _monitor(ingested_conn, "chat-class-4a")
    classifier = StubClassifier()

    first = _review(ingested_conn, tmp_path, classifier)
    assert first.chats_with_delta == 1
    assert first.messages_processed == 3
    assert first.source_funnels["whatsapp"].messages_checked == 3
    assert first.source_funnels["whatsapp"].cursors_advanced == 1
    run = store.review_run(ingested_conn, first.run_id)
    assert run is not None
    persisted = json.loads(run["source_funnel_json"])
    assert persisted["whatsapp"]["monitored_channels"] == 1
    assert persisted["whatsapp"]["messages_checked"] == 3

    second = _review(ingested_conn, tmp_path, classifier)
    assert second.chats_with_delta == 0
    assert second.messages_processed == 0


def test_new_message_processes_only_the_delta(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _monitor(ingested_conn, "chat-class-4a")
    classifier = StubClassifier()
    _review(ingested_conn, tmp_path, classifier)  # consume initial backlog

    append_message(ingested_conn, "chat-class-4a", "c4a-0004", "please sign the new form")
    third = _review(ingested_conn, tmp_path, classifier)
    assert third.chats_with_delta == 1
    assert third.messages_processed == 1  # only the one new message


def test_backfilled_out_of_order_message_is_not_skipped(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A resync backfilling an older-send-time message is still picked up (#37).

    The cursor keys on ingestion id, not send-time, so a message ingested *after*
    a scan but bearing a ``message_timestamp`` earlier than the cursor's high-water
    send-time lands in the next delta rather than being filtered out forever.
    """
    chat_id = _monitor(ingested_conn, "chat-class-4a")
    classifier = StubClassifier()
    _review(ingested_conn, tmp_path, classifier)  # advances cursor past backlog
    assert _review(ingested_conn, tmp_path, classifier).messages_processed == 0

    # Backfill: ingested now (new id) but with a send-time *older* than the cursor's
    # high-water send-time (2026-06-01T08:06 from c4a-0003).
    append_message(
        ingested_conn,
        "chat-class-4a",
        "c4a-backfill",
        "please sign and return the form",
        timestamp="2026-06-01T08:04:00+00:00",
    )

    after = _review(ingested_conn, tmp_path, classifier)
    assert after.chats_with_delta == 1
    assert after.messages_processed == 1  # the backfilled message, not skipped

    # And it is not reprocessed on the next run.
    assert _review(ingested_conn, tmp_path, classifier).messages_processed == 0

    # The cursor advanced to the backfill's ingestion id (the new high-water mark).
    state = ingested_conn.execute(
        "SELECT last_processed_message_id FROM chat_review_state WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    backfill_id = ingested_conn.execute(
        "SELECT id FROM messages WHERE source_message_id = 'c4a-backfill'"
    ).fetchone()[0]
    assert state["last_processed_message_id"] == backfill_id


def test_only_monitored_chats_are_reviewed(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _monitor(ingested_conn, "chat-class-4a")  # building + school-parents left unmonitored
    outcome = _review(ingested_conn, tmp_path, StubClassifier())
    assert outcome.chats_with_delta == 1


def test_cursor_not_advanced_on_contract_error(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    chat_id = _monitor(ingested_conn, "chat-class-4a")

    class BadClassifier:
        def classify_traced(
            self,
            chat_display_name: str,
            delta: list[StoredMessage],
            prior_context: str | None,
            *,
            source: str = "whatsapp",
        ) -> ClassificationOutcome:
            return ClassificationOutcome(raw_output="{ not valid json")

    outcome = _review(ingested_conn, tmp_path, BadClassifier())
    assert outcome.errors and outcome.errors[0][0] == chat_id
    assert outcome.messages_processed == 0

    # Cursor untouched -> a good classifier reprocesses the same backlog next run.
    state = ingested_conn.execute(
        "SELECT last_processed_message_id FROM chat_review_state WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    assert state is None

    recovered = _review(ingested_conn, tmp_path, StubClassifier())
    assert recovered.messages_processed == 3

    # The raised error type is ContractError under the hood.
    from src.analysis.contract import parse_analysis

    try:
        parse_analysis("{ not valid json")
    except ContractError:
        pass
    else:
        raise AssertionError("expected ContractError")


def test_review_aborts_and_finalizes_run_when_classifier_is_unreachable(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A dead hub must fail the run loudly, not strand it at 'running' (#298)."""
    chat_id = _monitor(ingested_conn, "chat-class-4a")
    _monitor(ingested_conn, "chat-school-parents")
    calls = 0

    class UnreachableClassifier:
        def classify_traced(
            self,
            chat_display_name: str,
            delta: list[StoredMessage],
            prior_context: str | None,
            *,
            source: str = "whatsapp",
        ) -> ClassificationOutcome:
            nonlocal calls
            calls += 1
            raise ClassifierUnavailable("hub unreachable at http://127.0.0.1:8000: boom")

    outcome = _review(ingested_conn, tmp_path, UnreachableClassifier())

    assert calls == 1  # stopped once, not once per monitored chat
    assert outcome.notification_status == CLASSIFIER_OFFLINE_STATUS
    assert outcome.errors and outcome.errors[0][0] == chat_id
    run = store.review_run(ingested_conn, outcome.run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["notification_status"] == CLASSIFIER_OFFLINE_STATUS

    # Cursors held: a recovered hub reprocesses the whole backlog next run.
    assert ingested_conn.execute(
        "SELECT 1 FROM chat_review_state WHERE chat_id = ?", (chat_id,)
    ).fetchone() is None
    assert ingested_conn.execute(
        "SELECT COUNT(*) AS n FROM chat_review_state"
    ).fetchone()["n"] == 0
    recovered = _review(ingested_conn, tmp_path, StubClassifier())
    assert recovered.chats_with_delta == 2
    assert recovered.messages_processed == 5  # both backlogs, nothing lost
    # Both backlogs are actionable under the stub, and delivery was withheld.
    assert recovered.notification_status == "dry_run"
