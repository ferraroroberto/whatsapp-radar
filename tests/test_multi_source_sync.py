"""Multi-source fan-out, consolidation, and failure isolation (#58)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from src.analysis.classifier import ClassificationOutcome
from src.analysis.pipeline import scan, scan_outcome_to_dict
from src.config import Config, HubConfig, TelegramConfig
from src.connector.base import ConnectorStatus
from src.connector.factory import ConnectorBinding
from src.connector.fixture import FixtureConnector
from src.db import store
from src.db.sync import sync_sources
from src.models import ChatRecord, MessageRecord, StoredMessage


def _fixture(path: Path, chat_id: str, message_id: str, text: str) -> FixtureConnector:
    path.write_text(
        json.dumps(
            {
                "chats": [
                    {
                        "source_chat_id": chat_id,
                        "display_name": f"{chat_id} channel",
                        "chat_type": "group",
                        "messages": [
                            {
                                "source_message_id": message_id,
                                "message_timestamp": "2026-07-10T08:00:00+00:00",
                                "text": text,
                                "sender_label": "Example Sender",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return FixtureConnector(path)


def _config(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "test.sqlite3",
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="stub"),
        notifier="none",
        telegram=TelegramConfig("", ""),
        linked_device_dir=tmp_path / "linked",
        sources=("whatsapp", "gmail"),
    )


class _AlwaysActionable:
    def classify_traced(
        self,
        chat_display_name: str,
        delta: list[StoredMessage],
        prior_context: str | None,
        *,
        source: str = "whatsapp",
    ) -> ClassificationOutcome:
        return ClassificationOutcome(
            raw_output=json.dumps(
                {
                    "action_required": True,
                    "priority": "high",
                    "summary": f"Act on {chat_display_name}",
                    "suggested_next_action": "Review it",
                    "deadline": None,
                    "confidence": 0.9,
                    "evidence_message_ids": [delta[0].source_message_id],
                }
            ),
            llm_called=True,
        )


class _OfflineConnector:
    def connect(self) -> ConnectorStatus:
        return ConnectorStatus("gmail", False, "quota unavailable")

    def status(self) -> ConnectorStatus:
        return self.connect()

    def list_chats(self) -> list[ChatRecord]:
        raise AssertionError("offline source must not be read")

    def fetch_messages(self, source_chat_id: str) -> list[MessageRecord]:
        raise AssertionError("offline source must not be read")

    def canonical_source_id(self, source_chat_id: str) -> str | None:
        return source_chat_id

    def stop(self) -> None:
        return None


def test_sync_fans_out_and_tags_each_source(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    bindings = [
        ConnectorBinding(
            "whatsapp",
            _fixture(tmp_path / "wa.json", "shared", "wa-1", "deadline tomorrow"),
        ),
        ConnectorBinding(
            "gmail",
            _fixture(tmp_path / "gm.json", "shared", "gm-1", "deadline Friday"),
        ),
    ]

    outcome = sync_sources(conn, bindings, operation="ingest")

    assert outcome.successful_sources == {"whatsapp", "gmail"}
    assert outcome.delta.chats_added == 2
    rows = conn.execute(
        "SELECT source, source_chat_id FROM chats ORDER BY source"
    ).fetchall()
    assert [(row["source"], row["source_chat_id"]) for row in rows] == [
        ("gmail", "shared"),
        ("whatsapp", "shared"),
    ]
    logs = store.recent_syncs(conn)
    assert {row["connector_source"] for row in logs} == {"whatsapp", "gmail"}
    assert {row["status"] for row in logs} == {"success"}


def test_scan_consolidates_two_sources_into_one_delivery(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    bindings = [
        ConnectorBinding(
            "whatsapp",
            _fixture(tmp_path / "wa.json", "wa-chat", "wa-1", "deadline tomorrow"),
        ),
        ConnectorBinding(
            "gmail",
            _fixture(tmp_path / "gm.json", "gm-chat", "gm-1", "deadline Friday"),
        ),
    ]
    for source, source_chat_id in (("whatsapp", "wa-chat"), ("gmail", "gm-chat")):
        chat_id = store.upsert_chat(
            conn,
            ChatRecord(
                source=source,
                source_chat_id=source_chat_id,
                display_name=source_chat_id,
            ),
        )
        store.set_chat_status(conn, chat_id, "monitored")

    calls: list[int] = []

    def fake_deliver(
        _conn: sqlite3.Connection, _config: Config, run_id: int, _digest: Any
    ) -> tuple[str, str]:
        calls.append(run_id)
        return "sent", ""

    monkeypatch.setattr("src.analysis.pipeline.deliver_digest", fake_deliver)
    outcome = scan(
        conn,
        _config(tmp_path),
        connectors=bindings,
        classifier=_AlwaysActionable(),
    )

    assert outcome.actionable == 2
    assert outcome.digest is not None and len(outcome.digest.items) == 2
    assert len(calls) == 1
    payload = scan_outcome_to_dict(outcome)
    assert set(payload["sources"]) == {"whatsapp", "gmail"}
    assert payload["sources"]["gmail"]["sync_status"] == "success"
    assert payload["sources"]["gmail"]["messages_checked"] == 1
    assert payload["sources"]["gmail"]["llm_calls"] == 1
    assert payload["sources"]["gmail"]["cursors_advanced"] == 1
    persisted = conn.execute(
        "SELECT source_funnel_json FROM review_runs WHERE id = ?", (outcome.run_id,)
    ).fetchone()
    assert json.loads(persisted["source_funnel_json"])["gmail"]["actionable"] == 1


def test_failed_source_is_logged_and_its_cursor_does_not_advance(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    gmail_id = store.upsert_chat(
        conn,
        ChatRecord(
            source="gmail",
            source_chat_id="school",
            display_name="School Mail",
            chat_type="email",
        ),
    )
    store.set_chat_status(conn, gmail_id, "monitored")
    store.insert_message(
        conn,
        gmail_id,
        MessageRecord(
            source_message_id="cached-1",
            message_timestamp="2026-07-10T08:00:00+00:00",
            text="deadline tomorrow",
        ),
    )
    bindings = [
        ConnectorBinding(
            "whatsapp",
            _fixture(tmp_path / "wa.json", "wa-chat", "wa-1", "ordinary update"),
        ),
        ConnectorBinding("gmail", _OfflineConnector()),
    ]

    outcome = scan(
        conn,
        _config(tmp_path),
        connectors=bindings,
        classifier=_AlwaysActionable(),
    )

    assert outcome.source_errors and outcome.source_errors[0][0] == "gmail"
    payload = scan_outcome_to_dict(outcome)
    assert payload["ok"] is False
    assert payload["sources"]["gmail"]["sync_status"] == "failed"
    assert payload["sources"]["gmail"]["cursors_advanced"] == 0
    assert conn.execute(
        "SELECT 1 FROM chat_review_state WHERE chat_id = ?",
        (gmail_id,),
    ).fetchone() is None
    failed = store.recent_syncs(conn)[0]
    assert failed["connector_source"] == "gmail"
    assert failed["status"] == "failed"


def test_sync_sources_streams_progress_without_content(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    bindings = [
        ConnectorBinding(
            "whatsapp",
            _fixture(tmp_path / "wa.json", "shared", "wa-1", "deadline tomorrow"),
        ),
    ]
    lines: list[str] = []

    sync_sources(conn, bindings, operation="ingest", progress=lines.append)

    assert lines == [
        "• whatsapp: syncing…",
        "✓ whatsapp: 1 chat(s) · +1 new chat(s) · +1 message(s)",
    ]
    # Breadcrumbs must never leak message content or sender identities (#180).
    assert not any("deadline" in line or "@" in line for line in lines)
