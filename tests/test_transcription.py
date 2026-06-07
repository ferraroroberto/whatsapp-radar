"""Transcription module: hub client and pending-message runner (offline)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config import Config, HubConfig, TelegramConfig, TranscriptionConfig
from src.db import store
from src.models import ChatRecord, MessageRecord
from src.transcription.hub import TranscriptionError, transcribe_file
from src.transcription.runner import transcribe_pending


def _config(tmp_path: Path, *, enabled: bool = True) -> Config:
    media_root = tmp_path / "linked_device"
    media_root.mkdir()
    return Config(
        db_path=tmp_path / "test.sqlite3",
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="m"),
        transcription=TranscriptionConfig(
            enabled=enabled,
            window_days=7,
            hub_base_url="http://127.0.0.1:8090",
            model="whisper-1",
        ),
        notifier="none",
        telegram=TelegramConfig(bot_token="", chat_id=""),
        linked_device_dir=media_root,
    )


def _voice_pending(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    msg_id: str = "V1",
    ts: str = "2026-06-10T10:00:00+00:00",
) -> tuple[int, Path]:
    chat_id = store.upsert_chat(
        conn,
        ChatRecord(source_chat_id="g@g.us", display_name="G", chat_type="group"),
    )
    rel = f"media/{msg_id}.ogg"
    audio = tmp_path / "linked_device" / rel
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"fake-ogg")
    store.insert_message(
        conn,
        chat_id,
        MessageRecord(
            source_message_id=msg_id,
            message_timestamp=ts,
            text="[voice note]",
            message_type="voice",
            raw={"media_path": rel, "placeholder_text": "[voice note]"},
        ),
    )
    row = conn.execute(
        "SELECT id FROM messages WHERE source_message_id = ?", (msg_id,)
    ).fetchone()
    return int(row["id"]), audio


def test_transcribe_pending_updates_text_and_deletes_audio(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    row_id, audio = _voice_pending(conn, tmp_path)
    with patch(
        "src.transcription.runner.transcribe_file",
        return_value="Parent-teacher meeting moved to Friday 3pm",
    ):
        outcome = transcribe_pending(conn, config)
    assert outcome.transcribed == 1
    assert outcome.failed == 0
    row = conn.execute(
        "SELECT text, transcription_status, raw_json FROM messages WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row["text"] == "Parent-teacher meeting moved to Friday 3pm"
    assert row["transcription_status"] == "done"
    raw = json.loads(row["raw_json"])
    assert raw["placeholder_text"] == "[voice note]"
    assert not audio.exists()


def test_transcribe_pending_marks_failed_on_hub_error(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    row_id, audio = _voice_pending(conn, tmp_path)
    with patch(
        "src.transcription.runner.transcribe_file",
        side_effect=TranscriptionError("offline"),
    ):
        outcome = transcribe_pending(conn, config)
    assert outcome.transcribed == 0
    assert outcome.failed == 1
    row = conn.execute(
        "SELECT transcription_status FROM messages WHERE id = ?", (row_id,)
    ).fetchone()
    assert row["transcription_status"] == "failed"
    assert audio.exists()


def test_skip_old_voice_notes_outside_window(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    _voice_pending(conn, tmp_path, msg_id="OLD", ts="2020-01-01T10:00:00+00:00")
    outcome = transcribe_pending(conn, config)
    assert outcome.skipped_old == 1
    row = conn.execute(
        "SELECT transcription_status FROM messages WHERE source_message_id = 'OLD'"
    ).fetchone()
    assert row["transcription_status"] == "skipped_old"


def test_transcribe_disabled_is_noop(conn: sqlite3.Connection, tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=False)
    _voice_pending(conn, tmp_path)
    outcome = transcribe_pending(conn, config)
    assert outcome.transcribed == 0
    row = conn.execute(
        "SELECT transcription_status FROM messages WHERE source_message_id = 'V1'"
    ).fetchone()
    assert row["transcription_status"] == "pending"


def test_transcribe_file_parses_hub_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"text": " hello world "}).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    tx = TranscriptionConfig(True, 7, "http://127.0.0.1:8090", "whisper-1")
    path = Path(__file__).parent / "_fake.ogg"
    path.write_bytes(b"x")
    try:
        assert transcribe_file(path, tx) == "hello world"
    finally:
        path.unlink(missing_ok=True)
