"""Routine-prep -> calendar reminder creation: eligibility, event shape, idempotency (#218)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from src.analysis.contract import AnalysisResult
from src.analysis.reminders import build_reminder_event, create_reminder_event, is_eligible
from src.config import FamilyConfig
from src.db import store
from src.models import ChatRecord

_BASE_RESULT = AnalysisResult(
    action_required=True,
    priority="high",
    summary="Bring the permission slip",
    suggested_next_action="Sign and return it",
    deadline="Friday",
    confidence=0.9,
    evidence_message_ids=["m1"],
    deadline_date="2026-09-11",
    child="Sam",
    task_category="permission_slip",
    prep_complexity="routine",
)


def _result(**overrides: Any) -> AnalysisResult:
    return replace(_BASE_RESULT, **overrides)


def test_is_eligible_requires_routine_child_and_deadline() -> None:
    assert is_eligible(_result()) is True
    assert is_eligible(_result(prep_complexity="non_routine")) is False
    assert is_eligible(_result(child=None)) is False
    assert is_eligible(_result(deadline_date=None)) is False
    assert is_eligible(_result(action_required=False)) is False


def test_build_reminder_event_uses_deadline_date_and_configured_time() -> None:
    family = FamilyConfig(reminder_calendar_id="family@example.test", reminder_time="07:30")
    event = build_reminder_event(family, _result(), "School Updates")

    assert event["summary"] == "Sam: permission_slip"
    assert "Bring the permission slip" in event["description"]
    assert "Sign and return it" in event["description"]
    assert "School Updates" in event["description"]
    start = event["start"]["dateTime"]
    end = event["end"]["dateTime"]
    assert start.startswith("2026-09-11T07:30:00")
    assert end.startswith("2026-09-11T07:45:00")  # 15-minute reminder block


def test_build_reminder_event_falls_back_to_generic_title_with_no_task_category() -> None:
    family = FamilyConfig(reminder_calendar_id="family@example.test")
    event = build_reminder_event(family, _result(task_category=None), "School Updates")
    assert event["summary"] == "Sam: school prep"


class _FakeWriteClient:
    def __init__(self) -> None:
        self.insert_calls: list[dict[str, Any]] = []
        self._next_id = 0

    def insert_event(self, *, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
        self.insert_calls.append({"calendar_id": calendar_id, "event": event})
        self._next_id += 1
        return {"id": f"evt-{self._next_id}", **event}

    def delete_event(self, *, calendar_id: str, event_id: str) -> None:
        raise AssertionError("not exercised in these tests")

    def close(self) -> None:
        return None


def _conn(tmp_path: Path) -> sqlite3.Connection:
    return store.connect(tmp_path / "test.sqlite3")


def _chat(conn: sqlite3.Connection) -> int:
    return store.upsert_chat(
        conn, ChatRecord(source_chat_id="sender:school@example.test", display_name="School Updates")
    )


def _seed_item(conn: sqlite3.Connection, chat_id: int, result: AnalysisResult) -> int:
    run_id = store.start_run(conn, kind="scan")
    return store.insert_analysis_item(
        conn,
        run_id,
        chat_id,
        action_required=result.action_required,
        priority=result.priority,
        summary=result.summary,
        suggested_next_action=result.suggested_next_action,
        deadline=result.deadline,
        deadline_date=result.deadline_date,
        confidence=result.confidence,
        evidence_message_ids_json=json.dumps(result.evidence_message_ids),
        child=result.child,
        task_category=result.task_category,
        prep_complexity=result.prep_complexity,
    )


def test_create_reminder_event_calls_insert_and_persists_id(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        chat_id = _chat(conn)
        result = _result()
        item_id = _seed_item(conn, chat_id=chat_id, result=result)
        client = _FakeWriteClient()

        event_id = create_reminder_event(
            conn,
            client,
            calendar_id="family@example.test",
            family=FamilyConfig(reminder_calendar_id="family@example.test"),
            chat_id=chat_id,
            item_id=item_id,
            chat_display_name="School Updates",
            result=result,
        )

        assert event_id == "evt-1"
        assert len(client.insert_calls) == 1
        assert client.insert_calls[0]["calendar_id"] == "family@example.test"
        row = conn.execute(
            "SELECT calendar_event_id FROM analysis_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row["calendar_event_id"] == "evt-1"
    finally:
        conn.close()


def test_create_reminder_event_is_idempotent_for_same_evidence(tmp_path: Path) -> None:
    """Reprocessing/replaying the same underlying item must never mint a second event."""
    conn = _conn(tmp_path)
    try:
        chat_id = _chat(conn)
        result = _result()
        client = _FakeWriteClient()
        family = FamilyConfig(reminder_calendar_id="family@example.test")

        first_item_id = _seed_item(conn, chat_id=chat_id, result=result)
        first_event_id = create_reminder_event(
            conn, client, calendar_id="family@example.test", family=family,
            chat_id=chat_id, item_id=first_item_id, chat_display_name="School Updates",
            result=result,
        )

        # Simulate the same item being reclassified in a later run (a reprocess
        # or a retried delta) — a *new* analysis_items row, same evidence.
        second_item_id = _seed_item(conn, chat_id=chat_id, result=result)
        second_event_id = create_reminder_event(
            conn, client, calendar_id="family@example.test", family=family,
            chat_id=chat_id, item_id=second_item_id, chat_display_name="School Updates",
            result=result,
        )

        assert first_event_id == second_event_id == "evt-1"
        assert len(client.insert_calls) == 1  # only ever inserted once
        second_row = conn.execute(
            "SELECT calendar_event_id FROM analysis_items WHERE id = ?", (second_item_id,)
        ).fetchone()
        assert second_row["calendar_event_id"] == "evt-1"
    finally:
        conn.close()


def test_create_reminder_event_never_raises_on_insert_failure(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        chat_id = _chat(conn)
        result = _result()
        item_id = _seed_item(conn, chat_id=chat_id, result=result)

        class _FailingClient:
            def insert_event(self, *, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError("quota exceeded")

            def delete_event(self, *, calendar_id: str, event_id: str) -> None:
                raise AssertionError("not exercised")

            def close(self) -> None:
                return None

        event_id = create_reminder_event(
            conn,
            _FailingClient(),
            calendar_id="family@example.test",
            family=FamilyConfig(reminder_calendar_id="family@example.test"),
            chat_id=chat_id,
            item_id=item_id,
            chat_display_name="School Updates",
            result=result,
        )

        assert event_id is None
        row = conn.execute(
            "SELECT calendar_event_id FROM analysis_items WHERE id = ?", (item_id,)
        ).fetchone()
        assert row["calendar_event_id"] is None
    finally:
        conn.close()
