"""Pipeline integration: transcription before analysis and cursor guard."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from src.analysis.classifier import ClassificationOutcome, StubClassifier
from src.analysis.pipeline import scan
from src.config import Config, HubConfig, TelegramConfig, TranscriptionConfig
from src.connector.base import ConnectorStatus
from src.db import store
from src.models import ChatRecord, MessageRecord
from src.transcription.runner import transcribe_pending


class _EmptyLiveConnector:
    """Online connector that syncs nothing — messages already in SQLite."""

    def connect(self) -> ConnectorStatus:
        return ConnectorStatus(name="test", connected=True, detail="ok")

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(name="test", connected=True, detail="ok")

    def list_chats(self) -> list[ChatRecord]:
        return []

    def fetch_messages(self, source_chat_id: str) -> list[MessageRecord]:
        return []

    def canonical_source_id(self, source_chat_id: str) -> str | None:
        return source_chat_id

    def stop(self) -> None:
        return None


_ACTIONABLE_JSON = json.dumps(
    {
        "action_required": True,
        "priority": "high",
        "summary": "Meeting moved",
        "suggested_next_action": "Note the new time",
        "deadline": "Friday",
        "confidence": 0.9,
        "evidence_message_ids": ["V1"],
    }
)


def _config(tmp_path: Path, *, transcription: bool = True) -> Config:
    media_root = tmp_path / "linked_device"
    media_root.mkdir(parents=True, exist_ok=True)
    return Config(
        db_path=tmp_path / "scan.sqlite3",
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="m"),
        transcription=TranscriptionConfig(
            enabled=transcription,
            window_days=7,
            hub_base_url="http://127.0.0.1:8090",
            model="whisper-1",
        ),
        notifier="none",
        telegram=TelegramConfig(bot_token="", chat_id=""),
        linked_device_dir=media_root,
    )


class _ActionableClassifier(StubClassifier):
    def classify_traced(self, chat_name, delta, prior_context):  # type: ignore[no-untyped-def]
        return ClassificationOutcome(
            llm_called=True,
            raw_output=_ACTIONABLE_JSON,
            raw_response=_ACTIONABLE_JSON,
            system_prompt="s",
            user_prompt="u",
        )


def test_pending_voice_blocks_cursor_at_untranscribed_message(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    chat_id = store.upsert_chat(
        conn,
        ChatRecord(
            source_chat_id="block@g.us",
            display_name="Block Group",
            chat_type="group",
        ),
    )
    store.set_chat_status(conn, chat_id, "monitored")
    store.insert_message(
        conn,
        chat_id,
        MessageRecord(
            source_message_id="T1",
            message_timestamp="2026-06-10T09:00:00+00:00",
            text="noise only",
            message_type="text",
        ),
    )
    store.insert_message(
        conn,
        chat_id,
        MessageRecord(
            source_message_id="V1",
            message_timestamp="2026-06-10T10:00:00+00:00",
            text="[voice note]",
            message_type="voice",
            raw={"media_path": "media/V1.ogg"},
        ),
    )
    cfg = _config(tmp_path, transcription=False)
    scan(
        conn,
        cfg,
        mode="live",
        connector=_EmptyLiveConnector(),
        classifier=_ActionableClassifier(),
    )
    state = conn.execute(
        "SELECT last_processed_message_id FROM chat_review_state WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    text_row = conn.execute(
        "SELECT id FROM messages WHERE source_message_id = 'T1'"
    ).fetchone()
    voice_row = conn.execute(
        "SELECT id FROM messages WHERE source_message_id = 'V1'"
    ).fetchone()
    assert state is not None
    assert int(state["last_processed_message_id"]) == int(text_row["id"])
    assert int(state["last_processed_message_id"]) != int(voice_row["id"])


def test_transcribed_voice_passes_stage1_and_is_actionable(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    chat_id = store.upsert_chat(
        conn,
        ChatRecord(
            source_chat_id="voice@g.us",
            display_name="Voice Group",
            chat_type="group",
        ),
    )
    store.set_chat_status(conn, chat_id, "monitored")
    rel = "media/V1.ogg"
    audio = tmp_path / "linked_device" / rel
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"fake-ogg")
    store.insert_message(
        conn,
        chat_id,
        MessageRecord(
            source_message_id="V1",
            message_timestamp="2026-06-10T10:00:00+00:00",
            text="[voice note]",
            message_type="voice",
            raw={"media_path": rel, "placeholder_text": "[voice note]"},
        ),
    )
    cfg = _config(tmp_path)
    with patch(
        "src.transcription.runner.transcribe_file",
        return_value="Parent-teacher meeting moved to Friday 3pm",
    ):
        tx = transcribe_pending(conn, cfg)
    assert tx.transcribed == 1
    outcome = scan(conn, cfg, mode="dry_run", classifier=_ActionableClassifier())
    assert outcome.stage1_passed == 1
    assert outcome.actionable == 1
    trace = conn.execute(
        "SELECT input_text FROM analysis_trace WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    assert trace is not None
    assert "🎤" in trace["input_text"]
    assert "meeting" in trace["input_text"]
