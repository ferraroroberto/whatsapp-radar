"""task-os auto-export: a scan sends actionable messages to the Inbox on its own.

Fully offline: ``src.task_os.client.export_message`` is replaced by a recorder,
so task-os is never dialled. Scans run in ``process`` mode over the sanitized
fixture chats with the keyword stub classifier.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from src.analysis.classifier import StubClassifier
from src.analysis.pipeline import scan, scan_outcome_to_dict
from src.config import Config, TaskOsConfig
from src.config import task_os as task_os_config
from src.db import store
from src.models import StoredMessage, TaskExportContext
from src.task_os import auto_export, client
from tests.helpers import chat_id_by_source, review_config

READY = TaskOsConfig(enabled=True, token="secret", auto_export=True)


def _config(tmp_path: Path, task_os: TaskOsConfig) -> Config:
    return dataclasses.replace(review_config(tmp_path), task_os=task_os)


def _monitor_class_chat(conn: sqlite3.Connection) -> int:
    chat_id = chat_id_by_source(conn, "chat-class-4a")
    store.set_chat_status(conn, chat_id, "monitored")
    return chat_id


def _record(monkeypatch: pytest.MonkeyPatch) -> list[TaskExportContext]:
    sent: list[TaskExportContext] = []

    def fake_export(config: TaskOsConfig, ctx: TaskExportContext) -> dict[str, Any]:
        sent.append(ctx)
        return {"id": len(sent)}

    monkeypatch.setattr(client, "export_message", fake_export)
    return sent


def _exported(conn: sqlite3.Connection) -> dict[str, str | None]:
    rows = conn.execute("SELECT source_message_id, task_exported_at FROM messages").fetchall()
    return {r["source_message_id"]: r["task_exported_at"] for r in rows}


def _scan(conn: sqlite3.Connection, config: Config, mode: str = "process") -> Any:
    return scan(conn, config, mode=mode, classifier=StubClassifier(), deliver=False)  # type: ignore[arg-type]


def test_actionable_evidence_is_exported_once(
    ingested_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _monitor_class_chat(ingested_conn)
    sent = _record(monkeypatch)

    outcome = _scan(ingested_conn, _config(tmp_path, READY))

    assert outcome.actionable == 1
    assert [ctx.source_message_id for ctx in sent] == ["c4a-0002"]
    assert outcome.tasks_exported == 1
    assert scan_outcome_to_dict(outcome)["funnel"]["tasks_exported"] == 1
    exported = _exported(ingested_conn)
    assert exported["c4a-0002"] is not None
    assert exported["c4a-0001"] is None

    # The same verdict again (a retry, or a manual reprocess) posts nothing new.
    chat_id = chat_id_by_source(ingested_conn, "chat-class-4a")
    delta = store.messages_for_chat(ingested_conn, chat_id)
    again = auto_export.export_actionable(ingested_conn, READY, delta, ["c4a-0002"])
    assert again == 0
    assert len(sent) == 1


@pytest.mark.parametrize(
    "task_os",
    [
        TaskOsConfig(enabled=True, token="secret", auto_export=False),
        TaskOsConfig(enabled=False, token="secret", auto_export=True),
        TaskOsConfig(enabled=True, token="", auto_export=True),
    ],
    ids=["opt-in-off", "export-disabled", "no-token"],
)
def test_nothing_is_sent_unless_opted_in_and_ready(
    ingested_conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_os: TaskOsConfig,
) -> None:
    _monitor_class_chat(ingested_conn)
    sent = _record(monkeypatch)

    outcome = _scan(ingested_conn, _config(tmp_path, task_os))

    assert outcome.actionable == 1
    assert sent == []
    assert outcome.tasks_exported == 0


def test_dry_run_never_exports(
    ingested_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _monitor_class_chat(ingested_conn)
    sent = _record(monkeypatch)

    outcome = _scan(ingested_conn, _config(tmp_path, READY), mode="dry_run")

    assert outcome.actionable == 1
    assert sent == []


def test_failed_export_is_left_for_a_tap_and_does_not_hold_the_cursor(
    ingested_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_id = _monitor_class_chat(ingested_conn)

    def down(config: TaskOsConfig, ctx: TaskExportContext) -> Any:
        raise client.TaskOsError("task-os unreachable", status=503)

    monkeypatch.setattr(client, "export_message", down)

    outcome = _scan(ingested_conn, _config(tmp_path, READY))

    assert outcome.actionable == 1
    assert outcome.tasks_exported == 0
    assert _exported(ingested_conn)["c4a-0002"] is None
    assert outcome.source_funnels["whatsapp"].cursors_advanced == 1
    assert store.messages_since_cursor(ingested_conn, chat_id) == []


def _msg(mid: int, source_id: str, text: str | None) -> StoredMessage:
    return StoredMessage(
        id=mid, chat_id=1, source_message_id=source_id,
        message_timestamp="2026-06-01T08:00:00+00:00", text=text,
        sender_label=None, message_type="text",
    )


def test_pick_messages_prefers_evidence_then_newest_text() -> None:
    delta = [_msg(1, "a", "first"), _msg(2, "b", "second"), _msg(3, "c", None)]

    assert [m.id for m in auto_export.pick_messages(delta, ["b", "zzz"])] == [2]
    assert [m.id for m in auto_export.pick_messages(delta, ["c"])] == [2]
    assert [m.id for m in auto_export.pick_messages(delta, [])] == [2]
    assert auto_export.pick_messages([_msg(4, "d", "  ")], []) == []


def test_auto_export_parses_from_config_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WR_TASK_OS_AUTO_EXPORT", raising=False)
    assert task_os_config.parse({}).auto_export is False
    assert task_os_config.parse({"auto_export": True}).auto_export is True
    monkeypatch.setenv("WR_TASK_OS_AUTO_EXPORT", "0")
    assert task_os_config.parse({"auto_export": True}).auto_export is False
